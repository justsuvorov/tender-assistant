"""Чтение документов и разбор их структуры."""

import pytest
from docx import Document
from openpyxl import Workbook

from tender_assistant.core.parsers import (
    DataParser,
    MarkdownOutline,
    MarkdownTableBuilder,
    Section,
)


class TestMarkdownTableBuilder:
    def test_normalise_pads_short_rows(self):
        rows = MarkdownTableBuilder.normalise([["а", "б", "в"], ["г"]])
        assert rows == [["а", "б", "в"], ["г", "", ""]]

    def test_normalise_stringifies_and_strips(self):
        rows = MarkdownTableBuilder.normalise([[1, None, " x \n y "]])
        assert rows == [["1", "", "x   y"]]

    def test_from_rows_builds_header_and_separator(self):
        md = MarkdownTableBuilder.from_rows([["Поле", "Значение"], ["ИНН", "770"]])
        lines = md.splitlines()
        assert lines[0].startswith("| Поле")
        assert set(lines[1]) <= set("| -")
        assert "ИНН" in lines[2]

    def test_empty_input(self):
        assert MarkdownTableBuilder.from_rows([]) == ""
        assert MarkdownTableBuilder.normalise([]) == []


class TestDataParser:
    def test_reads_word_headings_as_markdown(self, requirements_docx):
        markdown = DataParser(str(requirements_docx)).origin_data()
        assert "# Конкурсная документация" in markdown
        assert "## 3. Состав заявки участника" in markdown
        assert "Выписка из ЕГРЮЛ" in markdown

    def test_reads_word_tables(self, template_docx):
        markdown = DataParser(str(template_docx)).origin_data()
        assert "| Полное наименование участника" in markdown

    def test_reads_excel_sheets(self, template_xlsx):
        markdown = DataParser(str(template_xlsx)).origin_data()
        assert "## Лист:" in markdown
        assert "ИНН" in markdown

    def test_unsupported_format_raises(self, tmp_path):
        path = tmp_path / "notes.rtf"
        path.write_text("stub", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported file format"):
            DataParser(str(path))

    def test_missing_file_raises_a_clear_error(self, tmp_path):
        """Иначе наружу летит PackageNotFoundError из python-docx."""
        with pytest.raises(FileNotFoundError, match="Файл не найден"):
            DataParser(str(tmp_path / "нет-такого.docx"))

    def test_directory_instead_of_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="не является файлом"):
            DataParser(str(tmp_path))

    @pytest.mark.parametrize("path", ["", None])
    def test_empty_path_raises(self, path):
        with pytest.raises(FileNotFoundError, match="Не указан путь"):
            DataParser(path)

    def test_excel_skips_empty_sheets(self, tmp_path):
        workbook = Workbook()
        workbook.active["A1"] = "Данные"
        workbook.create_sheet("Пустой")
        path = tmp_path / "book.xlsx"
        workbook.save(path)

        assert "Пустой" not in DataParser(str(path)).origin_data()

    def test_blank_lines_are_collapsed(self, tmp_path):
        doc = Document()
        doc.add_paragraph("Первый")
        for _ in range(5):
            doc.add_paragraph("")
        doc.add_paragraph("Второй")
        path = tmp_path / "sparse.docx"
        doc.save(path)

        assert "\n\n\n" not in DataParser(str(path)).origin_data()


class TestMarkdownOutlineWithHeadings:
    @pytest.fixture
    def outline(self):
        return MarkdownOutline(
            "# Раздел А\nтекст А\n\n## Раздел Б\nтекст Б\n\n### Раздел В\nтекст В"
        )

    def test_titles_and_levels(self, outline):
        assert outline.titles() == ["Раздел А", "Раздел Б", "Раздел В"]
        assert [s.level for s in outline.sections] == [1, 2, 3]

    def test_body_belongs_to_its_heading(self, outline):
        assert outline.find("Раздел Б").body == "текст Б"

    def test_last_section_body_is_captured(self, outline):
        assert outline.find("Раздел В").body == "текст В"

    def test_section_text_includes_heading(self, outline):
        assert outline.find("Раздел Б").text == "Раздел Б\n\nтекст Б"

    def test_as_list_is_numbered(self, outline):
        assert outline.as_list().splitlines()[1] == "2. Раздел Б"

    def test_text_before_first_heading_is_ignored(self):
        outline = MarkdownOutline("преамбула\n# Раздел\nтело")
        assert outline.titles() == ["Раздел"]
        assert outline.find("Раздел").body == "тело"

    def test_trailing_hashes_are_stripped(self):
        assert MarkdownOutline("## Раздел ##\nтело").titles() == ["Раздел"]


class TestMarkdownOutlineFind:
    @pytest.fixture
    def outline(self):
        return MarkdownOutline("## 3. Состав заявки участника\nтело\n\n## 4. Оценка\n-")

    def test_exact_match(self, outline):
        assert outline.find("3. Состав заявки участника").level == 2

    def test_case_and_punctuation_insensitive(self, outline):
        assert outline.find("состав заявки участника") is not None

    def test_partial_match(self, outline):
        """Модель часто возвращает заголовок без нумерации."""
        assert outline.find("Состав заявки").title == "3. Состав заявки участника"

    def test_unknown_heading(self, outline):
        assert outline.find("Раздела нет") is None

    @pytest.mark.parametrize("title", ["", None])
    def test_empty_query(self, outline, title):
        assert outline.find(title) is None


class TestMarkdownOutlineHeuristics:
    """Документы без стилевых заголовков — сплошь и рядом в тендерах."""

    @pytest.fixture
    def outline(self):
        return MarkdownOutline(
            "Извещение о проведении конкурса.\n"
            "1. Общие положения\n"
            "Заказчик проводит конкурс.\n"
            "2.1. Состав заявки\n"
            "Устав; выписка из ЕГРЮЛ;\n"
            "ПРИЛОЖЕНИЕ N 1\n"
            "форма заявки\n"
        )

    def test_numbered_paragraphs_become_headings(self, outline):
        assert outline.titles() == [
            "1. Общие положения", "2.1. Состав заявки", "ПРИЛОЖЕНИЕ N 1"
        ]

    def test_nesting_level_from_numbering(self, outline):
        assert [s.level for s in outline.sections] == [1, 2, 1]

    def test_body_is_captured(self, outline):
        assert outline.find("2.1. Состав заявки").body == "Устав; выписка из ЕГРЮЛ;"

    def test_enumeration_items_are_not_headings(self):
        """Пункт перечня заканчивается «;» — это тело раздела, а не заголовок."""
        outline = MarkdownOutline("1. Требования\n3.1. Устав организации;\n")
        assert outline.titles() == ["1. Требования"]

    def test_sentences_are_not_headings(self):
        outline = MarkdownOutline(
            "1. Требования\n2. Заявка подаётся в срок. Конверт запечатывается.\n"
        )
        assert outline.titles() == ["1. Требования"]

    def test_table_rows_are_not_headings(self):
        outline = MarkdownOutline("1. Требования\n| 2. Ячейка таблицы |\n")
        assert outline.titles() == ["1. Требования"]

    def test_heuristics_yield_to_real_headings(self):
        """При наличии стилевых заголовков нумерованные абзацы не трогаем."""
        outline = MarkdownOutline("# Настоящий заголовок\n1. Не заголовок\nтело")
        assert outline.titles() == ["Настоящий заголовок"]

    def test_document_without_any_structure(self):
        outline = MarkdownOutline("Просто сплошной текст без разделов.")
        assert not outline.has_headings
        assert outline.titles() == []

    def test_empty_document(self):
        outline = MarkdownOutline("")
        assert not outline.has_headings
        assert outline.as_list() == ""


class TestSection:
    def test_text_without_body(self):
        assert Section("Заголовок", 1).text == "Заголовок"

    def test_repr_shows_title(self):
        assert "Заголовок" in repr(Section("Заголовок", 2))
