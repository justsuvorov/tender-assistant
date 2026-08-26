"""Запись заполненной заявки в шаблон с цветовой разметкой."""

import pytest
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from openpyxl import Workbook, load_workbook

from tender_assistant.core.pydantic_models import (
    DocumentListResult,
    FieldStatus,
    FilledField,
    RequiredDocument,
)
from tender_assistant.reports.style import GREEN, RED, YELLOW
from tender_assistant.reports.writers import (
    DocumentListWriter,
    ExcelApplicationWriter,
    ReportWriter,
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

    def test_fields_sharing_a_merged_cell_are_not_overwritten(self, tmp_path):
        """Регрессия из живого прогона на форме Росатова: колонка «Предложение
        участника» там объединена по вертикали на 10 строк подряд — 9 разных
        полей физически указывают на одну и ту же ячейку. Раньше «нашёл —
        сразу написал» приводило к тому, что каждое следующее поле молча
        затирало ответ предыдущего: 9 из 10 значений терялись, а в ячейке
        оставалось только последнее записанное."""
        doc = Document()
        table = doc.add_table(rows=3, cols=2)
        table.style = "Table Grid"
        for i, label in enumerate(["Поле А", "Поле Б", "Поле В"]):
            table.rows[i].cells[0].text = label
        # Объединяем колонку значений во всех трёх строках по вертикали —
        # именно так устроена реальная форма закупочной документации.
        table.cell(0, 1).merge(table.cell(2, 1))

        template = tmp_path / "merged.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [
                field("Поле А", "значение А", FieldStatus.FOUND),
                field("Поле Б", "значение Б", FieldStatus.CHECK),
                field("Поле В", "значение В", FieldStatus.FOUND),
            ],
            tmp_path / "out.docx", template,
        )

        merged_cell_text = Document(str(out)).tables[0].cell(0, 1).text
        assert "значение А" in merged_cell_text
        assert "значение Б" in merged_cell_text
        assert "значение В" in merged_cell_text

    def test_merged_cell_lines_keep_their_own_status_colour(self, tmp_path):
        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text = "Поле А"
        table.rows[1].cells[0].text = "Поле Б"
        table.cell(0, 1).merge(table.cell(1, 1))

        template = tmp_path / "merged.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [
                field("Поле А", "зелёное", FieldStatus.FOUND),
                field("Поле Б", "красное", FieldStatus.MISSING),
            ],
            tmp_path / "out.docx", template,
        )

        cell = Document(str(out)).tables[0].cell(0, 1)
        highlights = {
            p.runs[-1].font.highlight_color: p.text for p in cell.paragraphs if p.runs
        }
        assert highlights[WD_COLOR_INDEX.BRIGHT_GREEN] == "Поле А: зелёное"
        assert highlights[WD_COLOR_INDEX.RED] == "Поле Б: НЕ НАЙДЕНО"

    def test_single_field_in_a_merged_cell_behaves_as_before(self, tmp_path):
        """Ветка «одно поле — одна ячейка» не должна была измениться."""
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text = "Единственное поле"
        template = tmp_path / "single.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [field("Единственное поле", "значение", FieldStatus.FOUND)],
            tmp_path / "out.docx", template,
        )

        cell = Document(str(out)).tables[0].rows[0].cells[1]
        assert cell.text == "значение"
        assert cell.paragraphs[0].runs[0].font.highlight_color == WD_COLOR_INDEX.BRIGHT_GREEN

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


def _inline_field(anchor: str, value: str, status=FieldStatus.FOUND) -> FilledField:
    return FilledField(label=f"пропуск {anchor}", anchor=anchor, kind="inline",
                       value=value, status=status)


BLANK_TEXT = (
    "___ (наименование участника закупки) зарегистрирован в "
    "___ (наименование государства) в установленном порядке."
)


class TestWordApplicationWriterInlineBlanks:
    """Несколько разных по смыслу пропусков в одном абзаце ячейки — как
    в реальной форме 223-ФЗ, п. 15.1.

    Сам текст требования НЕ правится: ответы пишутся в колонку справа
    («Предложение участника»), человек переносит их в текст сам.
    """

    @pytest.fixture
    def multi_blank_template(self, tmp_path):
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].paragraphs[0].add_run(BLANK_TEXT)
        table.rows[0].cells[1].text = "«Да» / «Нет»"
        path = tmp_path / "multi_blank.docx"
        doc.save(path)
        return path

    def test_answers_land_in_the_cell_to_the_right(self, multi_blank_template, tmp_path):
        fields = [_inline_field("1", "САО «ВСК»"), _inline_field("2", "Россия")]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        answer_cell = Document(str(out)).tables[0].rows[0].cells[1].text
        assert "САО «ВСК»" in answer_cell
        assert "Россия" in answer_cell

    def test_requirement_text_is_left_untouched(self, multi_blank_template, tmp_path):
        """Главное свойство подхода: формулировку требования не трогаем,
        пропуски остаются на месте — правка не должна быть незаметной."""
        fields = [_inline_field("1", "САО «ВСК»"), _inline_field("2", "Россия")]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        assert Document(str(out)).tables[0].rows[0].cells[0].text == BLANK_TEXT

    def test_each_answer_is_labelled(self, multi_blank_template, tmp_path):
        """Ответов в ячейке несколько — без метки непонятно, какой куда."""
        fields = [_inline_field("1", "САО «ВСК»"), _inline_field("2", "Россия")]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        answer_cell = Document(str(out)).tables[0].rows[0].cells[1].text
        assert "пропуск 1: САО «ВСК»" in answer_cell
        assert "пропуск 2: Россия" in answer_cell

    def test_template_hint_in_the_answer_cell_survives(
        self, multi_blank_template, tmp_path
    ):
        """Подсказка шаблона («Да» / «Нет») нужна человеку при проверке —
        ответы дописываются под ней, а не вместо неё."""
        fields = [_inline_field("1", "X"), _inline_field("2", "Y")]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        assert "«Да» / «Нет»" in Document(str(out)).tables[0].rows[0].cells[1].text

    def test_each_answer_keeps_its_own_status_colour(
        self, multi_blank_template, tmp_path
    ):
        fields = [
            _inline_field("1", "САО «ВСК»", FieldStatus.FOUND),
            _inline_field("2", "", FieldStatus.MISSING),
        ]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        cell = Document(str(out)).tables[0].rows[0].cells[1]
        highlighted = {
            run.text: run.font.highlight_color
            for paragraph in cell.paragraphs
            for run in paragraph.runs
            if run.font.highlight_color
        }
        assert highlighted["САО «ВСК»"] == WD_COLOR_INDEX.BRIGHT_GREEN
        assert highlighted["НЕ НАЙДЕНО"] == WD_COLOR_INDEX.RED

    def test_blank_in_the_last_column_has_nowhere_to_write(self, tmp_path):
        """Справа колонки нет — поле не пишется наугад, а уходит в блок
        «Поля, не размещённые в шаблоне» в конце документа."""
        doc = Document()
        table = doc.add_table(rows=1, cols=1)
        table.rows[0].cells[0].paragraphs[0].add_run(BLANK_TEXT)
        template = tmp_path / "single_col.docx"
        doc.save(template)

        out = WordApplicationWriter().write(
            [_inline_field("1", "САО «ВСК»"), _inline_field("2", "Россия")],
            tmp_path / "out.docx", template,
        )

        result = Document(str(out))
        assert result.tables[0].rows[0].cells[0].text == BLANK_TEXT
        body_text = "\n".join(p.text for p in result.paragraphs)
        assert "Поля, не размещённые в шаблоне" in body_text
        assert "САО «ВСК»" in body_text

    def test_field_with_unknown_anchor_is_left_unmatched(
        self, multi_blank_template, tmp_path
    ):
        fields = [_inline_field("1", "X"), _inline_field("2", "Y"), _inline_field("99", "Z")]
        out = WordApplicationWriter().write(
            fields, tmp_path / "out.docx", multi_blank_template
        )

        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        assert "Поля, не размещённые в шаблоне" in text
        assert "Z" in text

    def test_inline_and_table_fields_coexist(self, multi_blank_template, tmp_path):
        """Инлайн-пропуски (в ячейке таблицы) и обычные построчные поля не
        должны мешать друг другу — независимые пути записи."""
        doc = Document(str(multi_blank_template))
        doc.add_paragraph("Руководитель организации: ______________")
        doc.save(multi_blank_template)

        fields = [
            _inline_field("1", "САО «ВСК»"),
            _inline_field("2", "Россия"),
            field("Руководитель организации", "Иванов И.И."),
        ]
        out = WordApplicationWriter().write(fields, tmp_path / "out.docx", multi_blank_template)

        result = Document(str(out))
        answer_cell = result.tables[0].rows[0].cells[1].text
        body_text = "\n".join(p.text for p in result.paragraphs)

        assert "САО «ВСК»" in answer_cell and "Россия" in answer_cell
        assert "Руководитель организации: Иванов И.И." in body_text


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


DOCUMENT_LIST_RESULT = DocumentListResult(
    documents=[
        RequiredDocument(name="Выписка из ЕГРЮЛ", mandatory=True, note="не ранее 6 месяцев"),
        RequiredDocument(name="Устав организации", mandatory=False, note=""),
    ],
    excluded=[
        RequiredDocument(name="Паспорт ИП", mandatory=True, note="участник — юрлицо"),
    ],
    source_headers=["3. Состав заявки участника"],
)


class TestDocumentListWriter:
    def test_is_a_report_writer(self):
        """Единый интерфейс с писателями заявки — не параллельная иерархия."""
        assert isinstance(DocumentListWriter(), ReportWriter)

    def test_title_and_source_are_present(self, tmp_path):
        out = DocumentListWriter().write(
            DOCUMENT_LIST_RESULT, tmp_path / "Перечень.docx"
        )
        doc = Document(str(out))

        headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
        assert "Перечень документов для участия в закупке" in headings

        source_line = next(p for p in doc.paragraphs if p.text.startswith("Источник"))
        assert "3. Состав заявки участника" in source_line.text
        assert source_line.runs[0].italic

    def test_documents_table_is_numbered(self, tmp_path):
        out = DocumentListWriter().write(
            DOCUMENT_LIST_RESULT, tmp_path / "Перечень.docx"
        )
        table = Document(str(out)).tables[0]

        assert [c.text for c in table.rows[0].cells] == [
            "№", "Документ", "Обязательность", "Примечание"
        ]
        assert [c.text for c in table.rows[1].cells] == [
            "1", "Выписка из ЕГРЮЛ", "обязателен", "не ранее 6 месяцев"
        ]
        assert [c.text for c in table.rows[2].cells] == [
            "2", "Устав организации", "по условию", ""
        ]

    def test_excluded_section_is_a_separate_table(self, tmp_path):
        out = DocumentListWriter().write(
            DOCUMENT_LIST_RESULT, tmp_path / "Перечень.docx"
        )
        doc = Document(str(out))

        headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
        assert "Исключено при проверке по нормативной базе" in headings

        assert len(doc.tables) == 2
        excluded_table = doc.tables[1]
        assert [c.text for c in excluded_table.rows[0].cells] == [
            "Документ", "Причина исключения"
        ]
        assert [c.text for c in excluded_table.rows[1].cells] == [
            "Паспорт ИП", "участник — юрлицо"
        ]

    def test_no_excluded_section_when_nothing_was_dropped(self, tmp_path):
        result = DocumentListResult(
            documents=[RequiredDocument(name="Устав")], excluded=[]
        )
        doc = Document(str(
            DocumentListWriter().write(result, tmp_path / "Перечень.docx")
        ))

        headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
        assert "Исключено при проверке по нормативной базе" not in headings
        assert len(doc.tables) == 1

    def test_no_source_line_when_headers_absent(self, tmp_path):
        """Ветка «заголовков нет»: документ читался целиком, источника не было."""
        result = DocumentListResult(
            documents=[RequiredDocument(name="Устав")], source_headers=[]
        )
        doc = Document(str(
            DocumentListWriter().write(result, tmp_path / "Перечень.docx")
        ))

        assert not any(p.text.startswith("Источник") for p in doc.paragraphs)

    def test_empty_document_list(self, tmp_path):
        out = DocumentListWriter().write(
            DocumentListResult(), tmp_path / "Перечень.docx"
        )
        table = Document(str(out)).tables[0]
        assert len(table.rows) == 1  # только шапка

    def test_output_directory_is_created(self, tmp_path):
        out = DocumentListWriter().write(
            DOCUMENT_LIST_RESULT, tmp_path / "новая" / "папка" / "Перечень.docx"
        )
        assert out.exists()

    def test_source_path_argument_is_accepted_and_ignored(self, tmp_path):
        """Совместимость с интерфейсом ReportWriter — перечень строится с нуля."""
        out = DocumentListWriter().write(
            DOCUMENT_LIST_RESULT, tmp_path / "Перечень.docx", source_path=tmp_path
        )
        assert out.exists()
