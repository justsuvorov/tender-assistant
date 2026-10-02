"""Модели vsk и qwen: retry-логика, разбор ответа, фабрика.

Настоящие HTTP-вызовы не делаются — клиент httpx подменяется.
"""

import pytest

from tender_assistant.ai import model as model_module
from tender_assistant.ai.model import (
    AIModel,
    LoggedModel,
    ModelFactory,
    QwenModel,
    VskAIModel,
)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry-логика реально спит между попытками — в тестах это не нужно."""
    monkeypatch.setattr(model_module.time, "sleep", lambda seconds: None)


class TestEmptyResponseDetection:
    """Ловим обе формы глагола: окончание зависит от названия модели."""

    @pytest.mark.parametrize("cls", [VskAIModel, QwenModel])
    @pytest.mark.parametrize("message", [
        "Qwen не вернул текст",
        "VSK AI не вернул текст",
        "Модель не вернула текст",
    ])
    def test_matches_both_grammatical_genders(self, cls, message):
        assert cls._is_empty_response(ValueError(message))

    @pytest.mark.parametrize("cls", [VskAIModel, QwenModel])
    def test_unrelated_error_does_not_match(self, cls):
        assert not cls._is_empty_response(ValueError("connection reset"))


class TestPromptCachingDefault:
    """AIModel.response_with_cache без переопределения — просто конкатенация.

    У vsk и qwen нет отдельного кэшируемого блока в API, поэтому кэшируемый
    префикс — обычный текст в начале запроса.
    """

    def test_call_receives_the_concatenated_string(self):
        captured = {}

        class RecordingModel(AIModel):
            def response(self, query: str) -> str:
                captured["query"] = query
                return "ответ"

        assert RecordingModel().response_with_cache("ПРЕФИКС ", "ЗАПРОС") == "ответ"
        assert captured["query"] == "ПРЕФИКС ЗАПРОС"

    @pytest.mark.parametrize("cls", [VskAIModel, QwenModel])
    def test_self_hosted_models_do_not_cache(self, cls):
        assert cls.supports_prompt_caching is False


class TestModelFactory:
    @pytest.mark.parametrize("provider, cls", [("vsk", VskAIModel), ("qwen", QwenModel)])
    def test_creates_the_provider_model(self, monkeypatch, provider, cls):
        monkeypatch.setattr(model_module.settings, "ai_provider", f" {provider.upper()} ")
        created = ModelFactory.create()

        assert isinstance(created, LoggedModel)
        assert isinstance(created.wrapped, cls)
        assert created.provider == provider

    @pytest.mark.parametrize("provider", ["gemini", "anthropic", "ollama", ""])
    def test_paid_and_unknown_providers_are_rejected(self, monkeypatch, provider):
        """В этой ветке только модели на своих серверах."""
        monkeypatch.setattr(model_module.settings, "ai_provider", provider)
        with pytest.raises(ValueError, match="vsk"):
            ModelFactory.create()


class TestSelfHostedPayload:
    """VSK и Qwen: temperature уходит в запрос, content=null — пустой ответ."""

    class _Response:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    def _model(self, cls, monkeypatch, data, sent):
        from tender_assistant.ai import model as model_module

        model = cls()
        monkeypatch.setattr(model_module.time, "sleep", lambda s: None)

        def post(url, **kwargs):
            sent.append(kwargs["json"])
            return self._Response(data)

        monkeypatch.setattr(model._client, "post", post)
        return model

    def test_vsk_sends_temperature(self, monkeypatch):
        from tender_assistant.ai.model import VskAIModel
        from tender_assistant.core.config import settings

        sent = []
        data = {"choices": [{"message": {"content": "ok"}}]}
        assert self._model(VskAIModel, monkeypatch, data, sent).response("q") == "ok"
        assert sent[0]["temperature"] == settings.ai_temperature

    def test_qwen_sends_temperature(self, monkeypatch):
        from tender_assistant.ai.model import QwenModel
        from tender_assistant.core.config import settings

        sent = []
        data = {"choices": [{"text": "ok"}]}
        assert self._model(QwenModel, monkeypatch, data, sent).response("q") == "ok"
        assert sent[0]["temperature"] == settings.ai_temperature

    def test_vsk_null_content_is_retried_as_empty(self, monkeypatch):
        """Раньше падало TypeError: expected string, got NoneType."""
        import pytest
        from tender_assistant.ai.model import VskAIModel

        sent = []
        data = {"choices": [{"message": {"content": None}}]}
        model = self._model(VskAIModel, monkeypatch, data, sent)

        with pytest.raises(RuntimeError, match="не вернул текст"):
            model.response("q")
        assert len(sent) == model.empty_response_retries
