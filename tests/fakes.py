"""Фиктивные LLM для тестов.

Настоящую модель в тестах дёргать нельзя: она платная, медленная и
недетерминированная. Здесь два подхода к подмене:

* ``ScriptedModel`` — отвечает по маркерам разделов промпта. Так тест
  проверяет и то, что нужный шаблон вообще был применён.
* ``SequenceModel`` — отдаёт заранее заданные ответы по порядку, когда
  содержимое промпта для теста не важно.
"""

import json
from typing import Callable, Dict, List, Optional, Union

from tender_assistant.ai.model import AIModel

Reply = Union[str, Callable[[str], str]]


def as_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def fenced(payload) -> str:
    """Ответ в ```json-обрамлении — так отвечает большинство моделей."""
    return f"```json\n{as_json(payload)}\n```"


class ScriptedModel(AIModel):
    """Отвечает в зависимости от того, какой маркер найден в промпте.

    Маркеры проверяются в порядке объявления ``replies``, поэтому более
    специфичные нужно ставить раньше.
    """

    def __init__(self, replies: Dict[str, Reply], strict: bool = True):
        self.replies = replies
        self.strict = strict
        self.calls: List[str] = []

    def response(self, query: str) -> str:
        self.calls.append(query)

        for marker, reply in self.replies.items():
            if marker in query:
                return reply(query) if callable(reply) else reply

        if self.strict:
            raise AssertionError(
                f"Промпт не опознан ни по одному маркеру:\n{query[:400]}"
            )
        return ""

    # ── помощники для проверок в тестах ──────────────────────────────────────

    def calls_with(self, marker: str) -> List[str]:
        return [c for c in self.calls if marker in c]

    def call_count(self, marker: str) -> int:
        return len(self.calls_with(marker))

    def last_call(self, marker: Optional[str] = None) -> str:
        calls = self.calls_with(marker) if marker else self.calls
        assert calls, f"Не было вызовов с маркером {marker!r}"
        return calls[-1]


class CachingScriptedModel(ScriptedModel):
    """ScriptedModel с поддержкой prompt caching — как AnthropicModel.

    Подбор ответа работает так же, как у ScriptedModel (маркер ищется по
    склеенным cache_prefix + query), но пары запоминаются отдельно — тесты
    могут проверить, что общий контекст ушёл именно в кэшируемый блок,
    а не заново с каждым запросом.
    """

    supports_prompt_caching = True

    def __init__(self, replies: Dict[str, Reply], strict: bool = True):
        super().__init__(replies, strict)
        self.cache_calls: List[tuple] = []

    def response_with_cache(self, cache_prefix: str, query: str) -> str:
        self.cache_calls.append((cache_prefix, query))
        return self.response(cache_prefix + query)


class SequenceModel(AIModel):
    """Отдаёт заготовленные ответы по одному на вызов."""

    def __init__(self, replies: List[str]):
        self._replies = list(replies)
        self.calls: List[str] = []

    def response(self, query: str) -> str:
        self.calls.append(query)
        if not self._replies:
            raise AssertionError("Ответы у SequenceModel закончились")
        return self._replies.pop(0)


class BrokenModel(AIModel):
    """Всегда возвращает мусор — проверяет устойчивость постпроцессоров."""

    def __init__(self, text: str = "Извините, не могу помочь."):
        self.text = text
        self.calls: List[str] = []

    def response(self, query: str) -> str:
        self.calls.append(query)
        return self.text


# ── Маркеры разделов промптов (см. core/config.py) ────────────────────────────

class Marker:
    HEADERS = "### ЗАГОЛОВКИ"
    SECTION = "### ТЕКСТ РАЗДЕЛА"
    NORMATIVE = "### ПРЕДВАРИТЕЛЬНЫЙ ПЕРЕЧЕНЬ"
    FILE_MATCH = "### ТРЕБУЕМЫЙ ДОКУМЕНТ"
    FORM_FIELDS = "### ШАБЛОН"
    FIELD_VALUE = "### ЗАПРАШИВАЕМОЕ ПОЛЕ"
    INLINE_BLANKS = "### ПРОПУСКИ ДЛЯ ЗАПОЛНЕНИЯ"


def value_between(query: str, marker: str) -> str:
    """Первая непустая строка сразу после маркера раздела промпта.

    Не весь текст до следующего "###": шаблон может содержать пояснительную
    фразу после самого значения (метки, задачи и т. п.), и это не должно
    ломать тесты при мелких правках текста промпта.
    """
    after = query.split(marker)[1]
    for line in after.splitlines():
        line = line.strip()
        if line:
            return line
    return ""
