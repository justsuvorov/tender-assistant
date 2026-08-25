from pathlib import Path

from tender_assistant.ai.context_builder import ContextBuilder, NormativeIndex

from tender_assistant.core.parsers import DataParser


class NormativeBaseLoader:
    """Load insurance normative documents from a file or a directory.

    Supported plain-text formats (.md, .txt) are read directly.
    Structured formats (.xlsx, .docx, .pdf) are parsed via DataParser
    so the same markdown conversion pipeline is reused.
    """

    _PLAIN_TEXT = {".md", ".txt"}

    def load(self, path: str) -> str:
        if not path:
            return ""

        p = Path(path)
        if not p.exists():
            return ""

        if p.is_file():
            return self._read_file(p)

        if p.is_dir():
            return self._read_directory(p)

        return ""

    def _read_file(self, path: Path) -> str:
        if path.suffix.lower() in self._PLAIN_TEXT:
            return path.read_text(encoding="utf-8").strip()

        try:
            return DataParser(str(path)).origin_data()
        except ValueError:
            return ""

    def _read_directory(self, directory: Path) -> str:

        supported = set(DataParser._SUPPORTED) | self._PLAIN_TEXT
        parts = []

        for file in sorted(directory.iterdir()):
            if file.is_file() and file.suffix.lower() in supported:
                content = self._read_file(file)
                if content:
                    parts.append(f"### {file.stem}\n\n{content}")

        return "\n\n---\n\n".join(parts)


class PromptEngine:
    """Assemble the final LLM prompt from its parts.

    The template must contain the following placeholders:
        {role}           — system role / persona
        {normative_base} — loaded insurance normative documents
        {examples}       — few-shot examples (may be empty)
        {source_text}    — client request converted to markdown

    If ``num_ctx > 0`` the engine first tries to fit the full normative base.
    When it doesn't fit, it splits the base into sections and retrieves only
    the most relevant ones (keyword overlap with source_text).
    """

    def __init__(self, role: str, template: str, normative_base: str, num_ctx: int):
        self._role = role
        self._template = template
        norm_text = NormativeBaseLoader().load(normative_base)
        self._norm_index = NormativeIndex(norm_text)
        self._context_builder = ContextBuilder(num_ctx, self._norm_index)
        print(
            f"[INFO] Нормативная база: {self._norm_index.section_count} разделов, "
            f"контекст {num_ctx} токенов",
            flush=True,
        )

    def build(self, source_text: str, examples: list[str]) -> str:
        examples_block = ""
        if examples:
            examples_block = "\n\n".join(
                f"### Пример {i + 1}\n{ex}" for i, ex in enumerate(examples)
            )

        return self._context_builder.build(
            template=self._template,
            role=self._role,
            examples=examples_block,
            source_text=source_text,
        )
