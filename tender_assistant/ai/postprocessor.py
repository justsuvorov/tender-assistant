import json
import re
from abc import ABC
from typing import Any, List


class PostProcessor(ABC):
    """Интерфейс подготовки ответа LLM к дальнейшей обработке."""

    def report(self, raw_text: str) -> Any:
        raise NotImplementedError


# ── Разбор JSON из ответа модели ──────────────────────────────────────────────

class JsonExtractor:
    """Достаёт JSON-объект из ответа LLM.

    Модели любят обрамлять ответ ```json ... ```, добавлять преамбулу
    («Вот результат:») и рассуждения в <think>. Извлекаем первый
    сбалансированный объект или массив.
    """

    _FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    _THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

    def extract(self, raw_text: str) -> Any:
        if not raw_text:
            return None

        text = self._THINK.sub("", raw_text).strip()

        fenced = self._FENCE.search(text)
        if fenced:
            parsed = self._loads(fenced.group(1))
            if parsed is not None:
                return parsed

        parsed = self._loads(text)
        if parsed is not None:
            return parsed

        for candidate in self._balanced_candidates(text):
            parsed = self._loads(candidate)
            if parsed is not None:
                return parsed

        return None

    @staticmethod
    def _loads(text: str) -> Any:
        try:
            return json.loads(text.strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _balanced_candidates(text: str):
        """Выдаёт сбалансированные фрагменты {...} и [...] по убыванию длины."""
        pairs = {"{": "}", "[": "]"}
        results = []

        for start, char in enumerate(text):
            closing = pairs.get(char)
            if closing is None:
                continue

            depth = 0
            in_string = False
            escaped = False

            for pos in range(start, len(text)):
                current = text[pos]

                if in_string:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        in_string = False
                    continue

                if current == '"':
                    in_string = True
                elif current == char:
                    depth += 1
                elif current == closing:
                    depth -= 1
                    if depth == 0:
                        results.append(text[start:pos + 1])
                        break

        results.sort(key=len, reverse=True)
        return results


class TextFallback:
    """Запасной разбор: превращает нумерованный/маркированный текст в список строк."""

    _BULLET = re.compile(r"^\s*(?:[-*•–]|\d+[.)]|[а-яa-z][.)])\s+", re.IGNORECASE)

    def lines(self, raw_text: str) -> List[str]:
        if not raw_text:
            return []

        result = []
        for line in raw_text.splitlines():
            cleaned = self._BULLET.sub("", line).strip(" \t|")
            cleaned = cleaned.strip()
            if len(cleaned) < 3:
                continue
            if cleaned.startswith("#") or set(cleaned) <= set("-|: "):
                continue
            result.append(cleaned)
        return result


# ── Конкретные постпроцессоры ─────────────────────────────────────────────────

class SectionsMatcherResponse(PostProcessor):
    """Ответ на поиск заголовка раздела со списком документов → list[str]."""

    def __init__(self):
        self._json = JsonExtractor()
        self._fallback = TextFallback()

    def report(self, raw_text: str) -> List[str]:
        data = self._json.extract(raw_text)

        if isinstance(data, dict):
            headers = data.get("headers") or data.get("header") or []
            if isinstance(headers, str):
                headers = [headers]
            return [str(h).strip() for h in headers if str(h).strip()]

        if isinstance(data, list):
            return [str(h).strip() for h in data if str(h).strip()]

        return self._fallback.lines(raw_text)


class DocumentListResponse(PostProcessor):
    """Ответ с перечнем документов → list[dict(name, mandatory, note)]."""

    def __init__(self):
        self._json = JsonExtractor()
        self._fallback = TextFallback()

    def report(self, raw_text: str) -> List[dict]:
        data = self._json.extract(raw_text)

        if isinstance(data, dict):
            if data.get("is_document_list") is False:
                return []
            items = data.get("documents") or data.get("items") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        if items:
            return self._normalise(items)

        return [{"name": name, "mandatory": True, "note": ""}
                for name in self._fallback.lines(raw_text)]

    @staticmethod
    def _normalise(items: list) -> List[dict]:
        result = []
        for item in items:
            if isinstance(item, str):
                name, mandatory, note = item, True, ""
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("document") or "").strip()
                mandatory = bool(item.get("mandatory", True))
                note = str(item.get("note") or "").strip()
            else:
                continue

            if name:
                result.append({"name": name, "mandatory": mandatory, "note": note})
        return result


class NormativeFilterResponse(PostProcessor):
    """Ответ проверки перечня по нормативной базе → dict(documents, excluded)."""

    def __init__(self):
        self._json = JsonExtractor()
        self._documents = DocumentListResponse()

    def report(self, raw_text: str) -> dict:
        data = self._json.extract(raw_text)

        if not isinstance(data, dict):
            return {"documents": self._documents.report(raw_text), "excluded": []}

        return {
            "documents": DocumentListResponse._normalise(data.get("documents") or []),
            "excluded": DocumentListResponse._normalise(data.get("excluded") or []),
        }


class TitleMatcherPostProcessor(PostProcessor):
    """Ответ подбора файла под документ → dict(files, confidence, note)."""

    _CONFIDENCE = {"high", "medium", "low"}

    def __init__(self):
        self._json = JsonExtractor()
        self._fallback = TextFallback()

    def report(self, raw_text: str) -> dict:
        data = self._json.extract(raw_text)

        if isinstance(data, dict):
            files = data.get("files") or data.get("file") or []
            if isinstance(files, str):
                files = [files]
            confidence = str(data.get("confidence") or "low").strip().lower()
            return {
                "files": [str(f).strip() for f in files if str(f).strip()],
                "confidence": confidence if confidence in self._CONFIDENCE else "low",
                "note": str(data.get("note") or "").strip(),
            }

        if isinstance(data, list):
            return {"files": [str(f).strip() for f in data if str(f).strip()],
                    "confidence": "low", "note": ""}

        return {"files": self._fallback.lines(raw_text), "confidence": "low", "note": ""}


class FormFieldsResponse(PostProcessor):
    """Ответ с перечнем полей шаблона → list[dict(label, anchor, kind)]."""

    def __init__(self):
        self._json = JsonExtractor()
        self._fallback = TextFallback()

    def report(self, raw_text: str) -> List[dict]:
        data = self._json.extract(raw_text)

        if isinstance(data, dict):
            items = data.get("fields") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        result = []
        for item in items:
            if isinstance(item, str):
                label, anchor, kind = item, item, "line"
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("name") or "").strip()
                anchor = str(item.get("anchor") or label).strip()
                kind = str(item.get("kind") or "line").strip()
            else:
                continue
            if label:
                result.append({"label": label, "anchor": anchor, "kind": kind})

        if result:
            return result

        return [{"label": line, "anchor": line, "kind": "line"}
                for line in self._fallback.lines(raw_text)]


class TenderRowPostProcessor(PostProcessor):
    """Ответ на заполнение одного поля → dict(value, status, source, note)."""

    _STATUSES = {"found", "check", "missing"}

    def __init__(self):
        self._json = JsonExtractor()

    def report(self, raw_text: str) -> dict:
        data = self._json.extract(raw_text)

        if not isinstance(data, dict):
            value = (raw_text or "").strip()
            return {
                "value": value,
                "status": "check" if value else "missing",
                "source": "",
                "note": "Ответ модели не в формате JSON — требуется проверка",
            }

        value = str(data.get("value") or "").strip()
        status = str(data.get("status") or "").strip().lower()

        if status not in self._STATUSES:
            status = "check" if value else "missing"
        if not value and status != "missing":
            status = "missing"

        return {
            "value": value,
            "status": status,
            "source": str(data.get("source") or "").strip(),
            "note": str(data.get("note") or "").strip(),
        }
