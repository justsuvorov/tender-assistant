"""Разбор ответов LLM.

Модели своевольничают с форматом, поэтому постпроцессоры обязаны переживать
обрамление, преамбулы, рассуждения и полностью нерелевантный текст.
"""

import pytest

from tender_assistant.ai.postprocessor import (
    DocumentListResponse,
    FormFieldsResponse,
    InlineBlanksResponse,
    JsonExtractor,
    NormativeFilterResponse,
    SectionsMatcherResponse,
    TenderRowPostProcessor,
    TextFallback,
    TitleMatcherPostProcessor,
)


class TestJsonExtractor:
    @pytest.fixture
    def extractor(self):
        return JsonExtractor()

    def test_plain_json(self, extractor):
        assert extractor.extract('{"a": 1}') == {"a": 1}

    def test_fenced_json(self, extractor):
        assert extractor.extract('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fence_without_language(self, extractor):
        assert extractor.extract('```\n{"a": 1}\n```') == {"a": 1}

    def test_preamble_and_epilogue(self, extractor):
        raw = 'Вот результат:\n{"a": [1, 2]}\nНадеюсь, это помогло!'
        assert extractor.extract(raw) == {"a": [1, 2]}

    def test_thinking_block_is_dropped(self, extractor):
        raw = '<think>рассуждения с {фигурными} скобками</think>\n{"a": "б"}'
        assert extractor.extract(raw) == {"a": "б"}

    def test_braces_inside_strings_do_not_break_parsing(self, extractor):
        raw = '{"text": "строка со } скобкой", "n": 1}'
        assert extractor.extract(raw) == {"text": "строка со } скобкой", "n": 1}

    def test_escaped_quote_inside_string(self, extractor):
        raw = r'{"text": "он сказал \"да\"", "n": 1}'
        assert extractor.extract(raw) == {"text": 'он сказал "да"', "n": 1}

    def test_top_level_array(self, extractor):
        assert extractor.extract('["а", "б"]') == ["а", "б"]

    def test_longest_candidate_wins(self, extractor):
        """Из вложенных кандидатов выбирается самый полный объект."""
        raw = 'мусор {"outer": {"inner": 1}} мусор'
        assert extractor.extract(raw) == {"outer": {"inner": 1}}

    @pytest.mark.parametrize("raw", ["", None, "совсем не json", "{битый: json"])
    def test_unparseable_returns_none(self, extractor, raw):
        assert extractor.extract(raw) is None


class TestTextFallback:
    @pytest.fixture
    def fallback(self):
        return TextFallback()

    @pytest.mark.parametrize("raw", [
        "1. Устав\n2. Выписка ЕГРЮЛ",
        "- Устав\n- Выписка ЕГРЮЛ",
        "• Устав\n• Выписка ЕГРЮЛ",
        "а) Устав\nб) Выписка ЕГРЮЛ",
    ])
    def test_bullet_styles(self, fallback, raw):
        assert fallback.lines(raw) == ["Устав", "Выписка ЕГРЮЛ"]

    def test_headings_and_separators_are_skipped(self, fallback):
        raw = "# Заголовок\n| --- |\nУстав\n\n:\n"
        assert fallback.lines(raw) == ["Устав"]

    def test_empty_input(self, fallback):
        assert fallback.lines("") == []


class TestSectionsMatcherResponse:
    @pytest.fixture
    def processor(self):
        return SectionsMatcherResponse()

    def test_list_of_headers(self, processor):
        raw = '{"headers": ["Состав заявки", "Приложения"], "confidence": "high"}'
        assert processor.report(raw) == ["Состав заявки", "Приложения"]

    def test_single_header_as_string(self, processor):
        assert processor.report('{"headers": "Один заголовок"}') == ["Один заголовок"]

    def test_bare_array(self, processor):
        assert processor.report('["Состав заявки"]') == ["Состав заявки"]

    def test_no_suitable_header(self, processor):
        assert processor.report('{"headers": [], "confidence": "low"}') == []

    def test_falls_back_to_text(self, processor):
        assert processor.report("1. Состав заявки\n2. Приложения") == [
            "Состав заявки", "Приложения"
        ]

    def test_empty_response(self, processor):
        assert processor.report("") == []


class TestDocumentListResponse:
    @pytest.fixture
    def processor(self):
        return DocumentListResponse()

    def test_full_answer(self, processor):
        raw = """{"is_document_list": true, "documents": [
            {"name": "Устав", "mandatory": true, "note": "действующая редакция"}
        ]}"""
        assert processor.report(raw) == [
            {"name": "Устав", "mandatory": True, "note": "действующая редакция"}
        ]

    def test_section_without_documents(self, processor):
        """Модель подтвердила, что перечня документов в разделе нет."""
        assert processor.report('{"is_document_list": false, "documents": []}') == []

    def test_documents_as_plain_strings(self, processor):
        assert processor.report('{"documents": ["Устав"]}') == [
            {"name": "Устав", "mandatory": True, "note": ""}
        ]

    def test_missing_fields_get_defaults(self, processor):
        assert processor.report('{"documents": [{"name": "Устав"}]}') == [
            {"name": "Устав", "mandatory": True, "note": ""}
        ]

    def test_entries_without_name_are_dropped(self, processor):
        raw = '{"documents": [{"note": "без названия"}, {"name": "Устав"}]}'
        assert [d["name"] for d in processor.report(raw)] == ["Устав"]

    def test_falls_back_to_text(self, processor):
        docs = processor.report("- Устав\n- Выписка ЕГРЮЛ")
        assert [d["name"] for d in docs] == ["Устав", "Выписка ЕГРЮЛ"]

    def test_explicitly_empty_list_is_respected(self, processor):
        """Разобранный JSON авторитетен: пустой список — это ответ, а не сбой.
        Иначе текстовый откат «найдёт» документы в самом тексте JSON."""
        assert processor.report('{"documents": []}') == []


class TestNormativeFilterResponse:
    @pytest.fixture
    def processor(self):
        return NormativeFilterResponse()

    def test_splits_kept_and_excluded(self, processor):
        raw = """{"documents": [{"name": "Устав"}],
                  "excluded": [{"name": "Паспорт ИП", "note": "мы юрлицо"}]}"""
        result = processor.report(raw)
        assert [d["name"] for d in result["documents"]] == ["Устав"]
        assert result["excluded"][0]["note"] == "мы юрлицо"

    def test_missing_excluded_key(self, processor):
        result = processor.report('{"documents": [{"name": "Устав"}]}')
        assert result["excluded"] == []

    def test_non_json_falls_back_to_document_list(self, processor):
        result = processor.report("- Устав")
        assert [d["name"] for d in result["documents"]] == ["Устав"]
        assert result["excluded"] == []


class TestTitleMatcherPostProcessor:
    @pytest.fixture
    def processor(self):
        return TitleMatcherPostProcessor()

    def test_full_answer(self, processor):
        raw = '{"files": ["Устав.docx"], "confidence": "high", "note": "точное"}'
        assert processor.report(raw) == {
            "files": ["Устав.docx"], "confidence": "high", "note": "точное"
        }

    def test_single_file_as_string(self, processor):
        assert processor.report('{"files": "a.pdf"}')["files"] == ["a.pdf"]

    def test_unknown_confidence_is_downgraded(self, processor):
        """Уверенность вне словаря нельзя трактовать как высокую."""
        assert processor.report('{"files": ["a.pdf"], "confidence": "ВЫСОКАЯ"}') == {
            "files": ["a.pdf"], "confidence": "low", "note": ""
        }

    def test_nothing_found(self, processor):
        result = processor.report('{"files": [], "confidence": "low", "note": "нет"}')
        assert result["files"] == []


class TestFormFieldsResponse:
    @pytest.fixture
    def processor(self):
        return FormFieldsResponse()

    def test_full_answer(self, processor):
        raw = '{"fields": [{"label": "ИНН", "anchor": "ячейка B2", "kind": "table_row"}]}'
        assert processor.report(raw) == [
            {"label": "ИНН", "anchor": "ячейка B2", "kind": "table_row"}
        ]

    def test_anchor_defaults_to_label(self, processor):
        assert processor.report('{"fields": [{"label": "ИНН"}]}') == [
            {"label": "ИНН", "anchor": "ИНН", "kind": "line"}
        ]

    def test_fields_as_strings(self, processor):
        assert processor.report('{"fields": ["ИНН"]}')[0]["label"] == "ИНН"

    def test_falls_back_to_text(self, processor):
        assert processor.report("1. ИНН\n2. Адрес")[0]["label"] == "ИНН"

    def test_explicitly_empty_list_is_respected(self, processor):
        """Шаблон без полей для заполнения — валидный ответ."""
        assert processor.report('{"fields": []}') == []


class TestTenderRowPostProcessor:
    @pytest.fixture
    def processor(self):
        return TenderRowPostProcessor()

    def test_full_answer(self, processor):
        raw = """{"value": "7701234567", "status": "found",
                  "source": "company.md", "note": ""}"""
        assert processor.report(raw) == {
            "value": "7701234567", "status": "found",
            "source": "company.md", "note": "",
        }

    def test_non_json_answer_requires_check(self, processor):
        """Сырой текст мог быть чем угодно — в документ он идёт под жёлтым."""
        result = processor.report("ИНН организации 7701234567")
        assert result["value"] == "ИНН организации 7701234567"
        assert result["status"] == "check"
        assert result["note"]

    def test_empty_non_json_answer_is_missing(self, processor):
        assert processor.report("")["status"] == "missing"

    def test_unknown_status_with_value_requires_check(self, processor):
        assert processor.report('{"value": "X", "status": "ok"}')["status"] == "check"

    def test_found_without_value_is_missing(self, processor):
        """Модель не может одновременно «найти» значение и не вернуть его."""
        assert processor.report('{"value": "", "status": "found"}')["status"] == "missing"


class TestInlineBlanksResponse:
    """Пакетный ответ по инлайн-пропускам: {"1": {...}, "2": {...}}."""

    @pytest.fixture
    def processor(self):
        return InlineBlanksResponse()

    def test_full_answer(self, processor):
        raw = """{"1": {"value": "САО «ВСК»", "status": "found",
                         "source": "company.md", "note": ""},
                  "2": {"value": "Россия", "status": "found",
                        "source": "", "note": ""}}"""
        result = processor.report(raw)

        assert result["1"]["value"] == "САО «ВСК»"
        assert result["2"]["value"] == "Россия"

    def test_each_item_normalised_like_a_single_field(self, processor):
        """Та же нормализация статусов, что у TenderRowPostProcessor —
        не два независимых, слегка расходящихся набора правил."""
        raw = '{"1": {"value": "X", "status": "ok"}}'
        assert processor.report(raw)["1"]["status"] == "check"

    def test_found_without_value_is_missing(self, processor):
        raw = '{"1": {"value": "", "status": "found"}}'
        assert processor.report(raw)["1"]["status"] == "missing"

    def test_non_dict_items_are_skipped(self, processor):
        raw = '{"1": "не объект", "2": {"value": "X", "status": "found"}}'
        result = processor.report(raw)
        assert "1" not in result
        assert result["2"]["value"] == "X"

    def test_not_a_json_object_yields_empty_dict(self, processor):
        assert processor.report("совсем не json") == {}
        assert processor.report("[1, 2, 3]") == {}

    def test_empty_response(self, processor):
        assert processor.report("") == {}

    def test_fenced_json(self, processor):
        raw = '```json\n{"1": {"value": "X", "status": "found"}}\n```'
        assert processor.report(raw)["1"]["value"] == "X"
