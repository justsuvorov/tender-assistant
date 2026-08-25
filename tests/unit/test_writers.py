"""Запись заполненной заявки в шаблон с цветовой разметкой."""

import pytest
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from openpyxl import Workbook, load_workbook

from tender_assistant.core.pydantic_models import FieldStatus, FilledField
from tender_assistant.reports.style import GREEN, RED, YELLOW
from tender_assistant.reports.writers import (
    ExcelApplicationWriter,
    TenderReportWriter,
    WordApplicationWriter,
)


def field(label, value, status=FieldStatus.FOUND, note=""):
    return FilledField(label=label, value=value, status=status, note=note)


FIELDS = [
    field("Полное наименование участника", "ООО «Ромашка»", FieldStatus.FOUND),
    field("ИНН", "7701234567", FieldStatus.CHECK),
    field("Контактный телефон", "", FieldStatus.MISSING),
]


def cell_text(path):
    return {r.cells[0].text: r.cells[1].text
            for r in Document(str(path)).tables[0].rows}


class TestWordApplicationWriter:
    @pytest.fixture
    def template(self, tmp_path):
        doc = Document()
        table = doc.add_table(rows=3, cols=2)
        table.style = "Table Grid"
        for i, label in enumerate(
            ["Полное наименование участника", "ИНН", "Контактный телефон"]
        ):
            table.rows[i].cells[0].text = label
        path = tmp_path / "form.docx"
        doc.save(path)
        return path

    def test_values_land_in_the_second_column(self, template, tmp_path):
        out = WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", template)
        cells = cell_text(out)

        assert cells["Полное наименование участника"] == "ООО «Ромашка»"
        assert cells["ИНН"] == "7701234567"

    def test_missing_value_is_marked_in_the_document(self, template, tmp_path):
        """Пустая ячейка выглядит как «забыли заполнить» — нужна явная метка."""
        out = WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", template)
        assert cell_text(out)["Контактный телефон"] == "НЕ НАЙДЕНО"

    @pytest.mark.parametrize("row, expected", [
        (0, WD_COLOR_INDEX.BRIGHT_GREEN),
        (1, WD_COLOR_INDEX.YELLOW),
        (2, WD_COLOR_INDEX.RED),
    ])
    def test_highlight_matches_status(self, template, tmp_path, row, expected):
        out = WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", template)
        cell = Document(str(out)).tables[0].rows[row].cells[1]
        assert cell.paragraphs[0].runs[0].font.highlight_color == expected

    @pytest.mark.parametrize("row, colour", [(0, GREEN), (1, YELLOW), (2, RED)])
    def test_cell_shading_matches_status(self, template, tmp_path, row, colour):
        out = WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", template)
        cell = Document(str(out)).tables[0].rows[row].cells[1]
        assert colour in cell._tc.xml

    def test_placeholder_in_paragraph_is_replaced(self, tmp_path):
        doc = Document()
        doc.add_paragraph("Руководитель организации: ______________")
        template = tmp_path / "p.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [field("Руководитель организации", "Иванов И.И.")],
            tmp_path / "out.docx", template,
        )
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)

        assert "Руководитель организации: Иванов И.И." in text
        assert "____" not in text

    @pytest.mark.parametrize("placeholder", ["______", "<указать>", "[значение]", "...."])
    def test_placeholder_variants(self, tmp_path, placeholder):
        doc = Document()
        doc.add_paragraph(f"Должность: {placeholder}")
        template = tmp_path / "p.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [field("Должность", "Директор")], tmp_path / "out.docx", template
        )
        assert "Должность: Директор" in Document(str(out)).paragraphs[0].text

    def test_only_the_value_is_highlighted_in_a_paragraph(self, tmp_path):
        doc = Document()
        doc.add_paragraph("Руководитель организации: ______")
        template = tmp_path / "p.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [field("Руководитель организации", "Иванов И.И.")],
            tmp_path / "out.docx", template,
        )
        runs = Document(str(out)).paragraphs[0].runs
        highlighted = [r.text for r in runs if r.font.highlight_color is not None]

        assert highlighted == ["Иванов И.И."]

    def test_paragraph_without_placeholder_gets_value_appended(self, tmp_path):
        doc = Document()
        doc.add_paragraph("Контактное лицо")
        template = tmp_path / "p.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [field("Контактное лицо", "Петров П.П.")], tmp_path / "out.docx", template
        )
        assert "Контактное лицо Петров П.П." in Document(str(out)).paragraphs[0].text

    def test_unmatched_fields_are_appended_not_lost(self, template, tmp_path):
        out = WordApplicationWriter().write(
            FIELDS + [field("Поля нет в шаблоне", "Значение", FieldStatus.CHECK,
                            note="проверить")],
            tmp_path / "out.docx", template,
        )
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)

        assert "Поля, не размещённые в шаблоне" in text
        assert "Поля нет в шаблоне: Значение" in text
        assert "проверить" in text

    def test_label_is_matched_loosely(self, template, tmp_path):
        """Модель возвращает наименование без нумерации и с сокращениями."""
        out = WordApplicationWriter().write(
            [field("наименование участника", "ООО «Ромашка»")],
            tmp_path / "out.docx", template,
        )
        assert cell_text(out)["Полное наименование участника"] == "ООО «Ромашка»"

    def test_source_template_is_untouched(self, template, tmp_path):
        before = template.read_bytes()
        WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", template)
        assert template.read_bytes() == before

    def test_source_path_is_required(self, tmp_path):
        with pytest.raises(ValueError, match="исходному файлу"):
            WordApplicationWriter().write(FIELDS, tmp_path / "out.docx", None)

    def test_output_directory_is_created(self, template, tmp_path):
        out = WordApplicationWriter().write(
            FIELDS, tmp_path / "новая" / "папка" / "out.docx", template
        )
        assert out.exists()


class TestExcelApplicationWriter:
    @pytest.fixture
    def template(self, tmp_path):
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "Полное наименование участника"
        sheet["A2"] = "ИНН"
        sheet["A3"] = "Контактный телефон"
        path = tmp_path / "form.xlsx"
        workbook.save(path)
        return path

    def test_values_land_next_to_labels(self, template, tmp_path):
        out = ExcelApplicationWriter().write(FIELDS, tmp_path / "out.xlsx", template)
        sheet = load_workbook(out).active

        assert sheet["B1"].value == "ООО «Ромашка»"
        assert sheet["B2"].value == "7701234567"
        assert sheet["B3"].value == "НЕ НАЙДЕНО"

    @pytest.mark.parametrize("cell, colour", [
        ("B1", GREEN), ("B2", YELLOW), ("B3", RED),
    ])
    def test_fill_matches_status(self, template, tmp_path, cell, colour):
        out = ExcelApplicationWriter().write(FIELDS, tmp_path / "out.xlsx", template)
        sheet = load_workbook(out).active
        assert sheet[cell].fill.fgColor.rgb.endswith(colour)

    def test_label_in_a_later_column(self, tmp_path):
        workbook = Workbook()
        workbook.active["C5"] = "ИНН"
        template = tmp_path / "form.xlsx"
        workbook.save(template)

        out = ExcelApplicationWriter().write(
            [field("ИНН", "7701234567")], tmp_path / "out.xlsx", template
        )
        assert load_workbook(out).active["D5"].value == "7701234567"

    def test_label_on_a_second_sheet(self, tmp_path):
        workbook = Workbook()
        workbook.create_sheet("Реквизиты")["A1"] = "ИНН"
        template = tmp_path / "form.xlsx"
        workbook.save(template)

        out = ExcelApplicationWriter().write(
            [field("ИНН", "7701234567")], tmp_path / "out.xlsx", template
        )
        assert load_workbook(out)["Реквизиты"]["B1"].value == "7701234567"

    def test_source_path_is_required(self, tmp_path):
        with pytest.raises(ValueError, match="исходному файлу"):
            ExcelApplicationWriter().write(FIELDS, tmp_path / "out.xlsx", None)


class TestTenderReportWriter:
    def test_docx_template_uses_word_writer(self, tmp_path):
        doc = Document()
        doc.add_paragraph("ИНН: ______")
        template = tmp_path / "form.docx"
        doc.save(template)

        out = TenderReportWriter().write(
            [field("ИНН", "7701234567")], tmp_path / "out.docx", template
        )
        assert out.suffix == ".docx"
        assert "7701234567" in Document(str(out)).paragraphs[0].text

    def test_xlsx_template_uses_excel_writer(self, tmp_path):
        workbook = Workbook()
        workbook.active["A1"] = "ИНН"
        template = tmp_path / "form.xlsx"
        workbook.save(template)

        out = TenderReportWriter().write(
            [field("ИНН", "7701234567")], tmp_path / "out.xlsx", template
        )
        assert load_workbook(out).active["B1"].value == "7701234567"

    def test_pdf_template_falls_back_to_a_standalone_docx(self, tmp_path):
        template = tmp_path / "form.pdf"
        template.write_text("stub", encoding="utf-8")

        out = TenderReportWriter().write(FIELDS, tmp_path / "out.pdf", template)

        assert out.suffix == ".docx" and out.exists()
        rows = Document(str(out)).tables[0].rows
        assert [c.text for c in rows[0].cells] == ["Поле", "Значение", "Примечание"]
        assert rows[1].cells[1].text == "ООО «Ромашка»"

    def test_standalone_output_keeps_status_colours(self, tmp_path):
        template = tmp_path / "form.pdf"
        template.write_text("stub", encoding="utf-8")

        out = TenderReportWriter().write(FIELDS, tmp_path / "out.pdf", template)

        # строка 0 — шапка, строка 2 — поле «ИНН» со статусом check
        cell = Document(str(out)).tables[0].rows[2].cells[1]
        assert cell.paragraphs[0].runs[0].font.highlight_color == WD_COLOR_INDEX.YELLOW
