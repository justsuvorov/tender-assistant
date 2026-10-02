"""Логика ServiceLLMModel и её конкретных реализаций.

Настоящие HTTP-вызовы к провайдерам здесь не делаются — только то, что
можно проверить без сети: разбор ответов SDK и совпадение сообщений об
ошибках с проверкой retry-логики. Оба теста ниже — регрессии на баги,
найденные живым прогоном через настоящий Anthropic API.
"""

import pytest

from tender_assistant.ai import model as model_module
from tender_assistant.ai.model import AIModel, AnthropicModel, ServiceLLMModel


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry-логика реально спит между попытками — в тестах это не нужно."""
    monkeypatch.setattr(model_module.time, "sleep", lambda seconds: None)


class DummyModel(ServiceLLMModel):
    """Модель, которую можно дёргать без сети — считает вызовы _call_api."""

    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0

    def _call_api(self, query: str) -> str:
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class TestEmptyResponseDetection:
    """Регрессия: raise-сообщения согласуют глагол по роду подлежащего
    ("Gemini не вернула текст", "Qwen не вернул текст"), а проверка сверяла
    только фразу с мужским окончанием — retry для Gemini/Anthropic никогда
    не срабатывал."""

    @pytest.mark.parametrize("message", [
        "Gemini не вернула текст",
        "Anthropic не вернула текст",
        "Qwen не вернул текст",
        "VSK AI не вернул текст",
    ])
    def test_matches_both_grammatical_genders(self, message):
        assert ServiceLLMModel._is_empty_response(ValueError(message))

    def test_matches_english_fallback(self):
        assert ServiceLLMModel._is_empty_response(ValueError("no response"))

    def test_unrelated_error_does_not_match(self):
        assert not ServiceLLMModel._is_empty_response(ValueError("connection reset"))

    def test_empty_response_triggers_a_retry(self):
        model = DummyModel([
            ValueError("Gemini не вернула текст"),
            "итоговый текст",
        ])
        assert model.response("запрос") == "итоговый текст"
        assert model.calls == 2

    def test_retries_are_capped(self):
        model = DummyModel([ValueError("Gemini не вернула текст")] * 5)
        model.empty_response_retries = 2
        assert model.response("запрос") != "итоговый текст"
        assert model.calls == 2


class Block:
    """Заглушка под anthropic.types.ContentBlock — только нужные поля."""

    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class TestAnthropicContentParsing:
    """Регрессия: при extended thinking message.content[0] — ThinkingBlock без
    .text, а не TextBlock. Код падал на content[0].text вместо поиска
    текстового блока среди всех вернувшихся."""

    @staticmethod
    def _text_from(blocks) -> str:
        # Та же логика, что в AnthropicModel._call_api — без сети.
        return "".join(
            block.text for block in blocks if getattr(block, "type", None) == "text"
        ).strip()

    def test_thinking_block_before_text_is_skipped(self):
        blocks = [Block("thinking", text=""), Block("text", text="ответ")]
        assert self._text_from(blocks) == "ответ"

    def test_thinking_only_yields_empty_string(self):
        """Если весь max_tokens ушёл на thinking — текста не будет вовсе,
        это и произошло в живом прогоне при max_tokens=4096."""
        blocks = [Block("thinking", text="")]
        assert self._text_from(blocks) == ""

    def test_plain_text_response(self):
        assert self._text_from([Block("text", text="ответ")]) == "ответ"


class TestPromptCachingDefault:
    """AIModel.response_with_cache без переопределения — просто конкатенация.

    Так ведут себя Gemini/Qwen/VSK/Ollama: у них нет client-side механизма
    кэширования промпта, поэтому кэшируемый префикс — обычный текст в начале
    запроса, а не отдельный блок.
    """

    def test_concatenates_prefix_and_query(self):
        model = DummyModel(["ответ"])
        assert model.response_with_cache("МАТЕРИАЛЫ...\n\n", "ПОЛЕ: ИНН") == "ответ"

    def test_call_receives_the_concatenated_string(self):
        captured = {}

        class RecordingModel(AIModel):
            def response(self, query: str) -> str:
                captured["query"] = query
                return "ответ"

        RecordingModel().response_with_cache("ПРЕФИКС ", "ЗАПРОС")
        assert captured["query"] == "ПРЕФИКС ЗАПРОС"

    def test_supports_prompt_caching_is_false_by_default(self):
        assert DummyModel([]).supports_prompt_caching is False


class TestAnthropicPromptCaching:
    """Регрессия/спецификация: живой прогон показал ~82К токенов контекста
    на каждый из 47 запросов по полям заявки — материалы отправлялись целиком
    заново на каждое поле. Anthropic поддерживает prompt caching через
    cache_control на отдельном content-блоке; это должно применяться только
    когда явно запрошено через response_with_cache.
    """

    @pytest.fixture
    def model(self):
        instance = AnthropicModel()

        class FakeMessage:
            content = [Block("text", text="ответ")]

        captured = {}

        def fake_create(**kwargs):
            captured["kwargs"] = kwargs
            return FakeMessage()

        instance._client.messages.create = fake_create
        instance._captured = captured
        return instance

    def test_supports_prompt_caching_flag(self, model):
        assert model.supports_prompt_caching is True

    def test_plain_response_sends_a_single_text_block(self, model):
        model.response("простой запрос")
        content = model._captured["kwargs"]["messages"][0]["content"]
        assert content == "простой запрос"

    def test_cached_response_splits_into_two_blocks(self, model):
        model.response_with_cache("общие материалы", "вопрос про ИНН")
        content = model._captured["kwargs"]["messages"][0]["content"]

        assert content == [
            {"type": "text", "text": "общие материалы",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "вопрос про ИНН"},
        ]

    def test_thinking_stays_disabled_on_cached_calls(self, model):
        model.response_with_cache("материалы", "вопрос")
        assert model._captured["kwargs"]["thinking"] == {"type": "disabled"}

    def test_empty_cache_prefix_behaves_like_plain_response(self, model):
        """response_with_cache("", query) не должен разбивать на блоки —
        пустой кэшируемый блок Anthropic не примет."""
        model.response_with_cache("", "вопрос")
        content = model._captured["kwargs"]["messages"][0]["content"]
        assert content == "вопрос"

    def test_retry_keeps_the_same_cache_prefix(self, model):
        """Пустой ответ на закэшированный вызов должен повторяться с тем же
        префиксом, а не терять его при retry."""
        calls = []

        def flaky_create(**kwargs):
            calls.append(kwargs["messages"][0]["content"])
            if len(calls) == 1:
                return type("M", (), {"content": [Block("thinking", text="")]})()
            return type("M", (), {"content": [Block("text", text="ответ")]})()

        model._client.messages.create = flaky_create
        result = model.response_with_cache("материалы", "вопрос")

        assert result == "ответ"
        assert len(calls) == 2
        assert calls[0] == calls[1]  # оба вызова — с одним и тем же блоком материалов



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
