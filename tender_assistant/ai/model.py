import re
import time
import httpx
from abc import ABC, abstractmethod

from tender_assistant.core.config import settings

# SDK провайдеров импортируются лениво: в установке достаточно пакета
# только того провайдера, который указан в AI_PROVIDER.


class AIModel(ABC):
    #: True у провайдеров, реально экономящих на повторной отправке
    #: большого общего префикса (например, Anthropic prompt caching).
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


# ── Service LLM base (для облачных моделей с retry-логикой) ──────────────────

_OVERLOAD_MESSAGE = (
    "Сервис модели перегружен или недоступен. "
    "Попыток исчерпаны. Попробуйте позже."
)


class ServiceLLMModel(AIModel, ABC):
    """Base class for service-based LLM models (Gemini, Anthropic, Qwen).

    Implements automatic retry logic for service errors:
    - 503 / UNAVAILABLE / overloaded — up to 3 retries with 5s delay
    - Empty response (ValueError) — up to 3 retries with 1s delay

    Subclass must implement _call_api() for a single API call without retry.
    """

    retries: int = 3
    retry_delay: int = 5
    empty_response_retries: int = 3
    empty_response_delay: int = 1

    @abstractmethod
    def _call_api(self, query: str) -> str:
        """Make one API call. Return text or raise an exception."""

    def response(self, query: str) -> str:
        for attempt in range(1, self.retries + 1):
            try:
                return self._call_api(query)
            except ValueError as exc:
                # Empty/invalid response — retry with short timeout
                if self._is_empty_response(exc) and attempt < self.empty_response_retries:
                    print(
                        f"[WARN] {self.__class__.__name__} не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.empty_response_delay)
                    continue

                if self._is_empty_response(exc):
                    print(
                        f"[ERROR] {self.__class__.__name__} не вернул валидный текст "
                        f"после {self.empty_response_retries} попыток",
                        flush=True,
                    )
                    return _OVERLOAD_MESSAGE

                # Other ValueError — fail immediately
                raise RuntimeError(f"Ошибка {self.__class__.__name__}: {exc}") from exc

            except Exception as exc:
                if self._is_overload(exc) and attempt < self.retries:
                    print(
                        f"[WARN] {self.__class__.__name__} перегружен, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек. Ошибка: {exc}",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue

                if self._is_overload(exc):
                    print(
                        f"[ERROR] {self.__class__.__name__} недоступен после {self.retries} попыток",
                        flush=True,
                    )
                    return _OVERLOAD_MESSAGE

                raise RuntimeError(f"Ошибка {self.__class__.__name__}: {exc}") from exc

        return _OVERLOAD_MESSAGE

    @staticmethod
    def _is_overload(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "503" in text
            or "unavailable" in text
            or "overloaded" in text
            or "429" in text
            or "rate limit" in text
        )

    # Наши raise ValueError(...) согласуют глагол по роду подлежащего
    # ("Gemini не вернула текст", "Qwen не вернул текст"), поэтому проверка
    # на точную фразу "не вернул текст" пропускала половину случаев —
    # ловим оба окончания одним паттерном.
    _EMPTY_RESPONSE_RE = re.compile(r"не верн\w* текст")

    @classmethod
    def _is_empty_response(cls, exc: Exception) -> bool:
        text = str(exc).lower()
        return bool(cls._EMPTY_RESPONSE_RE.search(text)) or "no response" in text


# ── Gemini (cloud) ────────────────────────────────────────────────────────────

class GeminiModel(ServiceLLMModel):
    def __init__(self):
        from google import genai

        self._client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value()
        )
        self._config = genai.types.GenerateContentConfig(
            temperature=settings.ai_temperature,
            top_p=0.95,
            top_k=64,
            max_output_tokens=4096,
        )

    def _call_api(self, query: str) -> str:
        result = self._client.models.generate_content(
            model=settings.model_name,
            contents=query,
            config=self._config,
        )
        if not result or not result.text:
            raise ValueError("Gemini не вернула текст")

        finish = (
            getattr(result.candidates[0], "finish_reason", "unknown")
            if result.candidates
            else "unknown"
        )
        tokens_out = (
            getattr(result.usage_metadata, "candidates_token_count", "?")
            if result.usage_metadata
            else "?"
        )
        print(
            f"[INFO] Gemini finish_reason={finish}, output_tokens={tokens_out}, chars={len(result.text)}",
            flush=True,
        )
        return result.text.strip()


# ── Anthropic Claude (cloud) ──────────────────────────────────────────────────

class AnthropicModel(ServiceLLMModel):
    #: Реальное prompt caching через cache_control — см. response_with_cache.
    supports_prompt_caching = True

    def __init__(self):
        import anthropic

        self._sdk = anthropic
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )

    def _call_api(self, query: str, cache_prefix: str = "") -> str:
        # temperature не передаём: для новых моделей Claude (Sonnet/Opus 5)
        # этот параметр deprecated и API отвечает 400 invalid_request_error.
        #
        # thinking явно отключаем: для этих же моделей extended thinking
        # включено по умолчанию и тратит на рассуждения весь max_tokens,
        # так что на сам ответ не остаётся ни одного токена (stop_reason
        # оказывается "max_tokens" при пустом текстовом блоке). Наши задачи —
        # извлечение и классификация текста, а не многошаговое рассуждение,
        # поэтому thinking здесь не нужен.
        #
        # cache_prefix — общий для многих вызовов кусок промпта (например,
        # материалы для заполнения десятков полей заявки): выносим его
        # отдельным блоком с cache_control, чтобы платить полную цену только
        # за первый вызов, а за последующие в течение ~5 минут — по тарифу
        # чтения кэша (на порядок дешевле). Без cache_prefix ведём себя как
        # раньше — один текстовый блок без разбивки.
        content = (
            [
                {"type": "text", "text": cache_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": query},
            ]
            if cache_prefix else query
        )
        message = self._client.messages.create(
            model=settings.anthropic_model_name,
            max_tokens=8192,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": content}],
        )
        # message.content — список блоков; при включённом extended thinking
        # первым идёт ThinkingBlock без атрибута text, текст — в TextBlock.
        text = "".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise ValueError("Anthropic не вернула текст")
        return text

    def response(self, query: str) -> str:
        return self._retrying_call(query, cache_prefix="")

    def response_with_cache(self, cache_prefix: str, query: str) -> str:
        return self._retrying_call(query, cache_prefix=cache_prefix)

    def _retrying_call(self, query: str, cache_prefix: str) -> str:
        """Общий retry-цикл response()/response_with_cache().

        Отдельный override нужен из-за специфичного для Anthropic заголовка
        retry-after при 429 — базовый ServiceLLMModel.response() его не читает.
        """
        for attempt in range(1, self.retries + 1):
            try:
                return self._call_api(query, cache_prefix)
            except self._sdk.RateLimitError as e:
                if attempt < self.retries:
                    wait = self.retry_delay
                    try:
                        retry_after = e.response.headers.get("retry-after")
                        if retry_after:
                            wait = int(float(retry_after)) + 5
                    except Exception:
                        pass
                    print(
                        f"[WARN] {self.__class__.__name__} 429 rate limit, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {wait} сек",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Ошибка Anthropic API: {e}") from e
            except self._sdk.APIStatusError as e:
                if e.status_code in (500, 503) and attempt < self.retries:
                    print(
                        f"[WARN] {self.__class__.__name__} {e.status_code}, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка Anthropic API: {e}") from e
            except ValueError as e:
                if self._is_empty_response(e) and attempt < self.empty_response_retries:
                    print(
                        f"[WARN] {self.__class__.__name__} не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.empty_response_delay)
                    continue
                raise RuntimeError(f"Ошибка Anthropic API: {e}") from e
        return _OVERLOAD_MESSAGE



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
                    print(
                        f"[WARN] Qwen не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.empty_response_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: {exc}") from exc

            except httpx.TimeoutException as exc:
                if attempt < self.retries:
                    print(
                        f"[WARN] Qwen таймаут (ReadTimeout), "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: таймаут после {self.retries} попыток") from exc

            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code in (503, 504) and attempt < self.retries):
                    print(
                        f"[WARN] Qwen {exc.response.status_code}, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка Qwen API: {exc}") from exc

            except Exception as exc:
                if self._is_overload(exc) and attempt < self.retries:
                    print(
                        f"[WARN] Qwen перегружен, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек. Ошибка: {exc}",
                        flush=True,
                    )
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
                "max_tokens": settings.qwen_max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["text"]
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

    # Наши raise ValueError(...) согласуют глагол по роду подлежащего
    # ("Gemini не вернула текст", "Qwen не вернул текст"), поэтому проверка
    # на точную фразу "не вернул текст" пропускала половину случаев —
    # ловим оба окончания одним паттерном.
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
                    print(
                        f"[WARN] VSK AI не вернул текст, "
                        f"попытка {attempt}/{self.empty_response_retries}, "
                        f"повтор через {self.empty_response_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.empty_response_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: {exc}") from exc

            except httpx.TimeoutException as exc:
                if attempt < self.retries:
                    print(
                        f"[WARN] VSK AI таймаут (ReadTimeout), "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: таймаут после {self.retries} попыток") from exc

            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code in (503, 504) and attempt < self.retries):
                    print(
                        f"[WARN] VSK AI {exc.response.status_code}, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек",
                        flush=True,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(f"Ошибка VSK AI API: {exc}") from exc

            except Exception as exc:
                if self._is_overload(exc) and attempt < self.retries:
                    print(
                        f"[WARN] VSK AI перегружен, "
                        f"попытка {attempt}/{self.retries}, "
                        f"повтор через {self.retry_delay} сек. Ошибка: {exc}",
                        flush=True,
                    )
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
                "thinking_token_budget": settings.vsk_thinking_token_budget,
                "max_tokens": settings.vsk_max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
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

    # Наши raise ValueError(...) согласуют глагол по роду подлежащего
    # ("Gemini не вернула текст", "Qwen не вернул текст"), поэтому проверка
    # на точную фразу "не вернул текст" пропускала половину случаев —
    # ловим оба окончания одним паттерном.
    _EMPTY_RESPONSE_RE = re.compile(r"не верн\w* текст")

    @classmethod
    def _is_empty_response(cls, exc: Exception) -> bool:
        text = str(exc).lower()
        return bool(cls._EMPTY_RESPONSE_RE.search(text)) or "no response" in text

# ── Ollama (local Docker or remote GPU server) ────────────────────────────────

class OllamaModel(AIModel):
    """Connect to any Ollama instance via HTTP.

    Local Docker:      LLM_BASE_URL=http://ollama:11434
    Remote GPU server: LLM_BASE_URL=http://<server-ip>:11434
    """

    _ENDPOINT = "/api/chat"
    _TIMEOUT = 900.0  # large models on CPU can take 10+ min for long prompts

    def __init__(self, base_url: str, model_name: str, temperature: float, num_ctx: int):
        self._url = base_url.rstrip("/") + self._ENDPOINT
        self._model_name = model_name
        self._temperature = temperature
        self._num_ctx = num_ctx

    def response(self, query: str) -> str:
        payload = {
            "model": self._model_name,
            "messages": [{"role": "user", "content": query}],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._num_ctx,
            },
        }

        try:
            resp = httpx.post(self._url, json=payload, timeout=self._TIMEOUT)
            resp.raise_for_status()
            return resp.json()["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Ollama вернула ошибку {e.response.status_code}: {e.response.text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Ошибка подключения к Ollama: {e}") from e


# ── Factory ───────────────────────────────────────────────────────────────────

class ModelFactory:
    """Select and instantiate the right AIModel based on AI_PROVIDER.

    AI_PROVIDER=ollama     → OllamaModel  (local Docker or remote GPU server)
    AI_PROVIDER=gemini     → GeminiModel  (Google Gemini API)
    AI_PROVIDER=anthropic  → AnthropicModel (Anthropic Claude API)
    AI_PROVIDER=qwen       → QwenModel (Qwen via OpenAI-compatible API)
    AI_PROVIDER=vsk        → VskAIModel (OpenAI-compatible chat API)
    """

    _PROVIDERS = ("ollama", "gemini", "anthropic", "qwen", "vsk")

    @staticmethod
    def create() -> AIModel:
        provider = settings.ai_provider.strip().lower()

        if provider == "ollama":
            return OllamaModel(
                base_url=settings.llm_base_url,
                model_name=settings.llm_model_name,
                temperature=settings.ai_temperature,
                num_ctx=settings.llm_num_ctx,
            )
        if provider == "gemini":
            return GeminiModel()
        if provider == "anthropic":
            return AnthropicModel()
        if provider == "qwen":
            return QwenModel()
        if provider == "vsk":
            return VskAIModel()

        raise ValueError(
            f"Неизвестный AI_PROVIDER='{provider}'. "
            f"Допустимые значения: {ModelFactory._PROVIDERS}"
        )
