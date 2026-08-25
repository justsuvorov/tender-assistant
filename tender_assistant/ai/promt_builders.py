from pathlib import Path
from typing import Optional

from tender_assistant.ai.context_builder import (
    ContextBuilder,
    NormativeIndex,
    TokenEstimator,
)
from tender_assistant.core.config import settings
from tender_assistant.core.parsers import DataParser


class NormativeBaseLoader:
    """Читает нормативную базу / базу знаний из файла или директории.

    Текстовые форматы (.md, .txt) читаются напрямую, структурированные
    (.xlsx, .docx, .pdf) — через DataParser, чтобы переиспользовать общий
    конвейер преобразования в markdown. Директории обходятся рекурсивно.
    """

    _PLAIN_TEXT = {".md", ".txt", ".csv"}

    def load(self, path: Optional[str]) -> str:
        if not path:
            return ""

        p = Path(path)
        if not p.exists():
            print(f"[WARN] Путь базы знаний не найден: {path}", flush=True)
            return ""

        if p.is_file():
            return self._read_file(p)

        if p.is_dir():
            return self._read_directory(p)

        return ""

    def _read_file(self, path: Path) -> str:
        if path.suffix.lower() in self._PLAIN_TEXT:
            try:
                return path.read_text(encoding="utf-8").strip()
            except (UnicodeDecodeError, OSError) as exc:
                print(f"[WARN] Не прочитан файл {path.name}: {exc}", flush=True)
                return ""

        try:
            return DataParser(str(path)).origin_data()
        except Exception as exc:
            print(f"[WARN] Не разобран файл {path.name}: {exc}", flush=True)
            return ""

    def _read_directory(self, directory: Path) -> str:
        supported = set(DataParser._SUPPORTED) | self._PLAIN_TEXT
        parts = []

        for file in sorted(directory.rglob("*")):
            if not file.is_file() or file.suffix.lower() not in supported:
                continue
            if file.name.startswith("~$"):  # временные файлы Word/Excel
                continue

            content = self._read_file(file)
            if content:
                parts.append(f"### Источник: {file.name}\n\n{content}")

        return "\n\n---\n\n".join(parts)


class PromptEngine:
    """Собирает финальный промпт из шаблона, роли и подставляемых значений.

    Шаблоны хранятся в настройках и содержат плейсхолдеры вида ``{role}``,
    ``{context}``, ``{section_text}``. Если у модели ограниченное контекстное
    окно, объёмное значение (база знаний) урезается до релевантных разделов —
    для этого при вызове ``render`` указывается ``fit_key``.
    """

    def __init__(self, role: Optional[str] = None, num_ctx: Optional[int] = None):
        self._role = role or settings.ai_role
        self._num_ctx = settings.context_window if num_ctx is None else num_ctx

    @property
    def role(self) -> str:
        return self._role

    def render(
        self,
        template: str,
        *,
        fit_key: Optional[str] = None,
        fit_query: str = "",
        **values,
    ) -> str:
        """Подставляет values в template.

        ``fit_key`` — имя плейсхолдера, значение которого можно урезать,
        если промпт не влезает в контекст. ``fit_query`` — текст, по которому
        отбираются релевантные разделы урезаемого значения.
        """
        values.setdefault("role", self._role)

        if fit_key is None or self._num_ctx <= 0:
            return template.format(**values)

        original = str(values.get(fit_key) or "")
        if not original:
            return template.format(**values)

        skeleton_values = dict(values, **{fit_key: ""})
        skeleton = template.format(**skeleton_values)

        budget = (
            self._num_ctx
            - ContextBuilder.ANSWER_RESERVE_TOKENS
            - TokenEstimator.tokens(skeleton)
        )

        if budget < ContextBuilder.MIN_BASE_TOKENS:
            print(
                f"[WARN] Контекста не хватает на '{fit_key}' — значение опущено",
                flush=True,
            )
            values[fit_key] = ""
        elif TokenEstimator.tokens(original) > budget:
            index = NormativeIndex(original)
            print(
                f"[INFO] '{fit_key}' не влезает в контекст: {index.section_count} "
                f"разделов, отбираем релевантные (бюджет {budget} токенов)",
                flush=True,
            )
            values[fit_key] = index.relevant(
                fit_query or skeleton, TokenEstimator.chars(budget)
            )

        return template.format(**values)

    # Совместимость с вызовом движка как функции
    __call__ = render
