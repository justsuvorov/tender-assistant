import logging
import re
import time
import httpx
from abc import ABC, abstractmethod

from tender_assistant.ai.context_builder import TokenEstimator
from tender_assistant.core.config import settings

logger = logging.getLogger(__name__)

# Провайдеры — только модели на своих серверах с OpenAI-совместимым API:
# vsk (/v1/chat/completions) и qwen (/v1/completions). Платные облачные API
# в этой ветке не используются.


class AIModel(ABC):
    #: True у провайдеров с явным кэшированием общего префикса промпта
    #: (кэшируемый блок передаётся отдельно). У vsk и qwen такого API нет.
    #: Влияет на то, обрезает ли вызывающий код контекст под конкретный
    #: запрос (self-hosted, окно небольшое) или отправляет целиком,
    #: полагаясь на кэш (см. TenderAIQuery в application/application.py).
    supports_prompt_caching: bool = False

    @abstractmethod
    def response(self, query: str) -> str:
        pass

    def response_with_cache(self, cache_prefix: str, query: str) -> str:
        """Запрос с явно выделенным кэшируемым префиксом.

        По умолчанию — просто конкатенация и обычный response(): провайдер
        не поддерживает кэширование, разбивать промпт на блоки незачем.
        Переопределяется там, где ``supports_prompt_caching = True``.
        """
        return self.response(cache_prefix + query)


# ── Журналирование вызовов ────────────────────────────────────────────────────

# Сколько символов промпта/ответа писать в журнал на уровне DEBUG. Целиком
# не пишем: материалы заявки — десятки тысяч символов на каждый вызов.
_DEBUG_PREVIEW_CHARS = 3000


def _preview(text: str) -> str:
    if len(text) <= _DEBUG_PREVIEW_CHARS:
        return text
    return f"{text[:_DEBUG_PREVIEW_CHARS]}\n… [ещё {len(text) - _DEBUG_PREVIEW_CHARS} симв.]"


class LoggedModel(AIModel):
    """Декоратор модели: пишет в журнал каждый вызов.

    На INFO — номер вызова, размер запроса (и кэшируемой части), время и
    размер ответа; на DEBUG — начало промпта и ответа. Сбой вызова пишется
    с временем до сбоя и пробрасывается дальше без изменений.

    Отдельный класс, а не код в каждой модели: провайдеров несколько, а по
    журналу нужно одинаково видеть, сколько вызовов было и сколько они шли.
    """

    def __init__(self, model: AIModel, provider: str):
        self._model = model
        self.provider = provider
        self.supports_prompt_caching = model.supports_prompt_caching
        self._calls = 0

    @property
    def wrapped(self) -> AIModel:
        return self._model

    def response(self, query: str) -> str:
        return self._logged(lambda: self._model.response(query), query, cache_prefix="")

    def response_with_cache(self, cache_prefix: str, query: str) -> str:
        return self._logged(
            lambda: self._model.response_with_cache(cache_prefix, query),
            query,
            cache_prefix=cache_prefix,
        )

    def _logged(self, call, query: str, cache_prefix: str) -> str:
        self._calls += 1
        number = self._calls
        size = len(cache_prefix) + len(query)
        cached = f", из них кэшируемая часть {len(cache_prefix)}" if cache_prefix else ""
        logger.info(
            "LLM #%d → %s: запрос %d симв. (~%d ток.)%s",
            number, self.provider, size, TokenEstimator.tokens(cache_prefix + query), cached,
        )
        logger.debug("LLM #%d промпт:\n%s", number, _preview(cache_prefix + query))

        started = time.perf_counter()
        try:
            text = call()
        except Exception as exc:
            logger.error(
                "LLM #%d ✗ %s: сбой через %.1f с — %s: %s",
                number, self.provider, time.perf_counter() - started,
                type(exc).__name__, exc,
            )
            raise

        logger.info(
            "LLM #%d ← %s: ответ %d симв. за %.1f с",
            number, self.provider, len(text or ""), time.perf_counter() - started,
        )
        logger.debug("LLM #%d ответ:\n%s", number, _preview(text or ""))
        return text


class QwenModel(AIModel):
    """Qwen via OpenAI-compatible API with automatic retry on service errors.

    Implements retry logic:
    - 503 / overloaded / connection errors — up to 3 retries with 5s delay
    - Empty response (ValueError) — up to 3 retries with 1s delay
    """

    retries = 3
    retry_delay = 5
    empty_response_retries = 3
    empty_response_delay = 1

    def __init__(self):
        self._api_url = settings.qwen_api_url
        self._model_name = settings.qwen_model_name
        self._client = httpx.Client(timeout=600, verify=False)  # 10 минут для больших промтов

    def response(self, query: str) -> str:
        for attempt in range(1, self.retries + 1):
            try:
                return self._call_api(query)
            except ValueError as exc:
                if self._is_empty_response(exc) and attempt < self.empty_response_retries:
                    logger.warning(f"Qwen не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек")
                    time.sleep(self.empty_response_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: {exc}") from exc

            except httpx.TimeoutException as exc:
                if attempt < self.retries:
                    logger.warning(f"Qwen таймаут (ReadTimeout), "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек")
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: таймаут после {self.retries} попыток") from exc

            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code in (503, 504) and attempt < self.retries):
                    logger.warning(f"Qwen {exc.response.status_code}, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек")
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: {exc}") from exc

            except Exception as exc:
                if self._is_overload(exc) and attempt < self.retries:
                    logger.warning(f"Qwen перегружен, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек. Ошибка: {exc}")
                    time.sleep(self.retry_delay)
                    continue

                raise RuntimeError(f"Ошибка Qwen API: {exc}") from exc

        return "Сервис модели недоступен. Попробуйте позже."

    def _call_api(self, query: str) -> str:
        resp = self._client.post(
            self._api_url,
            json={
                "model": self._model_name,
                "prompt": query,
                "temperature": settings.ai_temperature,
                "max_tokens": settings.qwen_max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # content бывает null (весь ответ ушёл в рассуждения) — это пустой
        # ответ, его повторяем, а не падаем на re.sub(None).
        text = data["choices"][0]["text"] or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text:
            raise ValueError("Qwen не вернул текст")
        return text

    @staticmethod
    def _is_overload(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "503" in text
            or "504" in text
            or "unavailable" in text
            or "overloaded" in text
            or "connection" in text
            or "timeout" in text
        )

    # Ловим и «не вернул текст», и «не вернула текст» — окончание зависит
    # от того, как в сообщении названа модель.
    _EMPTY_RESPONSE_RE = re.compile(r"не верн\w* текст")

    @classmethod
    def _is_empty_response(cls, exc: Exception) -> bool:
        text = str(exc).lower()
        return bool(cls._EMPTY_RESPONSE_RE.search(text)) or "no response" in text


# ── VSK AI (OpenAI-compatible /v1/chat/completions) ────────────────────────────

class VskAIModel(AIModel):
    """VSK AI via OpenAI-compatible chat API with automatic retry on service errors.

    Request format differs from QwenModel:
        POST {VSK_API_URL}
        Authorization: Bearer <VSK_API_KEY>
        {
            "model": "...",
            "messages": [{"role": "user", "content": "..."}],
            "thinking_token_budget": 1000,
            "max_tokens": 100000
        }

    Implements the same retry logic as QwenModel:
    - 503 / overloaded / connection errors — up to 3 retries with 5s delay
    - Empty response (ValueError) — up to 3 retries with 1s delay
    """

    retries = 5
    retry_delay = 5
    empty_response_retries = 3
    empty_response_delay = 1

    def __init__(self):
        self._api_url = settings.vsk_api_url
        self._api_key = settings.vsk_api_key.get_secret_value() if settings.vsk_api_key else ""
        self._model_name = settings.vsk_model_name
        self._client = httpx.Client(timeout=60, verify=False)

    def response(self, query: str) -> str:
        for attempt in range(1, self.retries + 1):
            try:
                return self._call_api(query)
            except ValueError as exc:
                if self._is_empty_response(exc) and attempt < self.empty_response_retries:
                    logger.warning(f"VSK AI не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек")
                    time.sleep(self.empty_response_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: {exc}") from exc

            except httpx.TimeoutException as exc:
                if attempt < self.retries:
                    logger.warning(f"VSK AI таймаут (ReadTimeout), "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек")
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: таймаут после {self.retries} попыток") from exc

            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code in (503, 504) and attempt < self.retries):
                    logger.warning(f"VSK AI {exc.response.status_code}, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек")
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: {exc}") from exc

            except Exception as exc:
                if self._is_overload(exc) and attempt < self.retries:
                    logger.warning(f"VSK AI перегружен, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек. Ошибка: {exc}")
                    time.sleep(self.retry_delay)
                    continue

                raise RuntimeError(f"Ошибка VSK AI API: {exc}") from exc

        return "Сервис модели недоступен. Попробуйте позже."

    def _call_api(self, query: str) -> str:
        resp = self._client.post(
            self._api_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model_name,
                "messages": [{"role": "user", "content": query}],
                # Без temperature сервер берёт своё значение по умолчанию, и
                # один и тот же документ даёт разный перечень от запуска к запуску.
                "temperature": settings.ai_temperature,
                "thinking_token_budget": settings.vsk_thinking_token_budget,
                "max_tokens": settings.vsk_max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # content бывает null (весь ответ ушёл в рассуждения) — это пустой
        # ответ, его повторяем, а не падаем на re.sub(None).
        text = data["choices"][0]["message"]["content"] or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text:
            raise ValueError("VSK AI не вернул текст")
        return text

    @staticmethod
    def _is_overload(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "503" in text
            or "504" in text
            or "unavailable" in text
            or "overloaded" in text
            or "connection" in text
            or "timeout" in text
        )

    # Ловим и «не вернул текст», и «не вернула текст» — окончание зависит
    # от того, как в сообщении названа модель.
    _EMPTY_RESPONSE_RE = re.compile(r"не верн\w* текст")

    @classmethod
    def _is_empty_response(cls, exc: Exception) -> bool:
        text = str(exc).lower()
        return bool(cls._EMPTY_RESPONSE_RE.search(text)) or "no response" in text


# ── Factory ───────────────────────────────────────────────────────────────────

class ModelFactory:
    """Select and instantiate the right AIModel based on AI_PROVIDER.

    AI_PROVIDER=vsk   → VskAIModel (OpenAI-compatible /v1/chat/completions)
    AI_PROVIDER=qwen  → QwenModel  (OpenAI-compatible /v1/completions)
    """

    _PROVIDERS = ("vsk", "qwen")

    @staticmethod
    def create() -> AIModel:
        """Модель выбранного провайдера, обёрнутая в журналирование вызовов."""
        provider = settings.ai_provider.strip().lower()
        model = ModelFactory._create(provider)
        logger.info("Модель: провайдер %s (%s)", provider, type(model).__name__)
        return LoggedModel(model, provider)

    @staticmethod
    def _create(provider: str) -> AIModel:
        if provider == "vsk":
            return VskAIModel()
        if provider == "qwen":
            return QwenModel()

        raise ValueError(
            f"Неизвестный AI_PROVIDER='{provider}'. "
            f"Допустимые значения: {ModelFactory._PROVIDERS}"
        )
