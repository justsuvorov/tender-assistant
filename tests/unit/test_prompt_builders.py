"""Загрузка базы знаний и сборка промпта."""

import pytest

from tender_assistant.ai.context_builder import (
    ContextBuilder,
    NormativeIndex,
    TokenEstimator,
)
from tender_assistant.ai.promt_builders import NormativeBaseLoader, PromptEngine


class TestNormativeBaseLoader:
    @pytest.fixture
    def loader(self):
        return NormativeBaseLoader()

    @pytest.mark.parametrize("path", ["", None])
    def test_empty_path(self, loader, path):
        assert loader.load(path) == ""

    def test_missing_path(self, loader, tmp_path):
        assert loader.load(str(tmp_path / "нет-такой-папки")) == ""

    def test_plain_text_file(self, loader, tmp_path):
        path = tmp_path / "rules.md"
        path.write_text("Правило первое", encoding="utf-8")
        assert loader.load(str(path)) == "Правило первое"

    def test_structured_file_goes_through_parser(self, loader, requirements_docx):
        assert "Состав заявки участника" in loader.load(str(requirements_docx))

    def test_directory_is_concatenated_with_source_labels(self, loader, tmp_path):
        (tmp_path / "а.md").write_text("Первый", encoding="utf-8")
        (tmp_path / "б.txt").write_text("Второй", encoding="utf-8")

        result = loader.load(str(tmp_path))
        assert "### Источник: а.md" in result
        assert "### Источник: б.txt" in result
        assert "Первый" in result and "Второй" in result

    def test_directory_is_walked_recursively(self, loader, tmp_path):
        nested = tmp_path / "вложенная"
        nested.mkdir()
        (nested / "в.md").write_text("Вложенный", encoding="utf-8")
        assert "Вложенный" in loader.load(str(tmp_path))

    def test_unsupported_extensions_are_skipped(self, loader, tmp_path):
        (tmp_path / "заметка.rtf").write_text("Пропустить", encoding="utf-8")
        (tmp_path / "правило.md").write_text("Взять", encoding="utf-8")

        result = loader.load(str(tmp_path))
        assert "Взять" in result and "Пропустить" not in result

    def test_office_lock_files_are_skipped(self, loader, tmp_path):
        """Word и Excel держат рядом временные файлы ~$имя — они не данные."""
        (tmp_path / "~$устав.docx").write_text("мусор", encoding="utf-8")
        (tmp_path / "правило.md").write_text("Взять", encoding="utf-8")

        assert loader.load(str(tmp_path)) == "### Источник: правило.md\n\nВзять"

    def test_broken_file_does_not_break_the_load(self, loader, tmp_path):
        """Один битый файл не должен ронять чтение всей базы знаний."""
        (tmp_path / "битый.docx").write_text("это не docx", encoding="utf-8")
        (tmp_path / "правило.md").write_text("Взять", encoding="utf-8")

        assert "Взять" in loader.load(str(tmp_path))


class TestTokenEstimator:
    def test_tokens_grow_with_length(self):
        assert TokenEstimator.tokens("а" * 250) > TokenEstimator.tokens("а" * 25)

    def test_roundtrip_is_consistent(self):
        assert TokenEstimator.chars(TokenEstimator.tokens("а" * 100)) >= 100

    def test_empty_text(self):
        assert TokenEstimator.tokens("") == 1


class TestNormativeIndex:
    def test_splits_by_markdown_headings(self):
        index = NormativeIndex("# А\nтекст\n\n# Б\nтекст")
        assert index.section_count == 2

    def test_splits_by_horizontal_rule(self):
        index = NormativeIndex("Первый\n---\nВторой")
        assert index.section_count == 2

    def test_empty_text(self):
        index = NormativeIndex("")
        assert index.section_count == 0
        assert index.relevant("что угодно", 1000) == ""

    def test_relevant_prefers_matching_section(self):
        index = NormativeIndex(
            "# Транспорт\nавтомобили грузовики перевозки маршруты\n\n"
            "# Реквизиты\nИНН КПП ОГРН расчётный счёт организации"
        )
        result = index.relevant("ИНН КПП организации", char_budget=60)
        assert "ИНН" in result
        assert "автомобили" not in result

    def test_budget_is_respected(self):
        index = NormativeIndex("\n\n".join(f"# Р{i}\n{'я' * 100}" for i in range(10)))
        assert len(index.relevant("я", char_budget=250)) <= 250

    def test_tiny_budget_still_returns_something(self):
        """Лучше обрезок самого релевантного раздела, чем пустой контекст."""
        index = NormativeIndex("# Р\n" + "я" * 500)
        assert index.relevant("я", char_budget=50) != ""


class TestContextBuilder:
    TEMPLATE = "{role}\n{normative_base}\n{examples}\n{source_text}"

    def test_unlimited_context_keeps_full_base(self):
        index = NormativeIndex("# Р\n" + "я" * 5000)
        builder = ContextBuilder(0, index)
        assert "я" * 5000 in builder.build(self.TEMPLATE, "роль", "", "запрос")

    def test_large_window_keeps_full_base(self):
        index = NormativeIndex("# Р\nкороткий текст")
        builder = ContextBuilder(100000, index)
        assert "короткий текст" in builder.build(self.TEMPLATE, "роль", "", "запрос")

    def test_base_is_trimmed_when_window_is_small(self):
        index = NormativeIndex("\n\n".join(f"# Р{i}\n{'я' * 400}" for i in range(20)))
        builder = ContextBuilder(5000, index)
        result = builder.build(self.TEMPLATE, "роль", "", "запрос")
        assert 0 < result.count("я") < 20 * 400

    def test_base_is_dropped_when_window_is_tiny(self):
        index = NormativeIndex("# Р\nсодержимое базы")
        builder = ContextBuilder(ContextBuilder.ANSWER_RESERVE_TOKENS + 10, index)
        assert "содержимое базы" not in builder.build(
            self.TEMPLATE, "роль", "", "запрос"
        )


class TestPromptEngine:
    TEMPLATE = "{role}\n### ДАННЫЕ\n{context}\n### ПОЛЕ\n{field_label}"

    def test_role_is_substituted_by_default(self):
        result = PromptEngine(num_ctx=0).render(
            self.TEMPLATE, context="данные", field_label="ИНН"
        )
        assert "специалист тендерного отдела" in result

    def test_role_can_be_overridden(self):
        result = PromptEngine(role="Своя роль", num_ctx=0).render(
            self.TEMPLATE, context="данные", field_label="ИНН"
        )
        assert result.startswith("Своя роль")

    def test_values_are_substituted(self):
        result = PromptEngine(num_ctx=0).render(
            self.TEMPLATE, context="данные", field_label="ИНН"
        )
        assert "данные" in result and "ИНН" in result

    def test_no_trimming_without_fit_key(self):
        engine = PromptEngine(num_ctx=100)
        result = engine.render(self.TEMPLATE, context="Z" * 5000, field_label="ИНН")
        assert result.count("Z") == 5000

    def test_no_trimming_when_window_unlimited(self):
        engine = PromptEngine(num_ctx=0)
        result = engine.render(
            self.TEMPLATE, fit_key="context", context="Z" * 5000, field_label="ИНН"
        )
        assert result.count("Z") == 5000

    def test_large_value_is_trimmed_to_relevant_sections(self):
        context = "\n\n".join(
            [f"# Р{i}\n{'посторонний текст ' * 30}" for i in range(20)]
            + ["# Реквизиты\nИНН организации 7701234567"]
        )
        engine = PromptEngine(num_ctx=5000)
        result = engine.render(
            self.TEMPLATE,
            fit_key="context",
            fit_query="ИНН организации",
            context=context,
            field_label="ИНН",
        )
        assert "7701234567" in result
        assert len(result) < len(context)

    def test_value_is_dropped_when_window_is_tiny(self):
        engine = PromptEngine(num_ctx=ContextBuilder.ANSWER_RESERVE_TOKENS + 10)
        result = engine.render(
            self.TEMPLATE, fit_key="context", context="Z" * 5000, field_label="ИНН"
        )
        assert "Z" not in result

    def test_empty_fit_value_is_harmless(self):
        engine = PromptEngine(num_ctx=100)
        assert "ИНН" in engine.render(
            self.TEMPLATE, fit_key="context", context="", field_label="ИНН"
        )

    def test_engine_is_callable(self):
        """application.py исторически вызывает движок как функцию."""
        engine = PromptEngine(num_ctx=0)
        assert engine(self.TEMPLATE, context="д", field_label="ИНН") == engine.render(
            self.TEMPLATE, context="д", field_label="ИНН"
        )

    def test_window_follows_provider(self, monkeypatch):
        """Облачным моделям промпт не режется, self-hosted — режется."""
        from tender_assistant.core.config import settings

        monkeypatch.setattr(settings, "ai_provider", "gemini")
        assert PromptEngine()._num_ctx == 0

        monkeypatch.setattr(settings, "ai_provider", "ollama")
        monkeypatch.setattr(settings, "llm_num_ctx", 8192)
        assert PromptEngine()._num_ctx == 8192

    def test_explicit_window_wins_over_provider(self, monkeypatch):
        from tender_assistant.core.config import settings

        monkeypatch.setattr(settings, "ai_provider", "ollama")
        monkeypatch.setattr(settings, "prompt_context_window", 1234)
        assert PromptEngine()._num_ctx == 1234
