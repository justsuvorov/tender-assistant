"""Промпты в настройках: значения по умолчанию и переопределение из .env."""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from tender_assistant.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def default(name: str) -> str:
    return Settings.model_fields[name].default


class TestPromptsFromEnv:
    def test_escaped_newlines_become_real_ones(self, monkeypatch):
        monkeypatch.setenv("AI_ROLE", "Первая строка\\nВторая строка")

        assert Settings(_env_file=None).ai_role == "Первая строка\nВторая строка"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_value_falls_back_to_the_default(self, monkeypatch, value):
        """docker-compose передаёт незаданную переменную пустой строкой."""
        monkeypatch.setenv("DOCUMENT_NAME_MATCHER", value)

        assert Settings(_env_file=None).document_name_matcher == default(
            "document_name_matcher"
        )

    def test_without_env_the_code_defaults_are_used(self, monkeypatch):
        for name in Settings.PROMPT_FIELDS:
            monkeypatch.delenv(name.upper(), raising=False)

        settings = Settings(_env_file=None)

        for name in Settings.PROMPT_FIELDS:
            assert getattr(settings, name) == default(name)

    def test_env_file_is_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TENDER_FORM_FIELDS_TEMPLATE", raising=False)
        env = tmp_path / ".env"
        env.write_text(
            "TENDER_FORM_FIELDS_TEMPLATE={role}\\n### ШАБЛОН\\n{template_text}\n",
            encoding="utf-8",
        )

        assert Settings(_env_file=str(env)).tender_form_fields_template == (
            "{role}\n### ШАБЛОН\n{template_text}"
        )


class TestEnvExample:
    """В .env.example лежат все промпты — и ровно те, что в коде."""

    @pytest.fixture
    def example(self):
        return dotenv_values(ROOT / ".env.example")

    def test_every_prompt_is_listed(self, example):
        missing = [n for n in Settings.PROMPT_FIELDS if not example.get(n.upper())]
        assert missing == []

    def test_listed_prompts_match_the_code(self, monkeypatch):
        for name in Settings.PROMPT_FIELDS:
            monkeypatch.delenv(name.upper(), raising=False)

        settings = Settings(_env_file=str(ROOT / ".env.example"))

        for name in Settings.PROMPT_FIELDS:
            assert getattr(settings, name) == default(name), name
