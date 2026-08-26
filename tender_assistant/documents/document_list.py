from datetime import datetime
from pathlib import Path
from typing import List, Optional

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import (
    DocumentListResponse,
    NormativeFilterResponse,
    PostProcessor,
    SectionsMatcherResponse,
)
from tender_assistant.ai.promt_builders import NormativeBaseLoader, PromptEngine
from tender_assistant.core.config import settings
from tender_assistant.core.parsers import DataParser, MarkdownOutline, Section
from tender_assistant.core.pydantic_models import DocumentListResult, RequiredDocument
from tender_assistant.reports.report_export import BaseReport, DocumentListReport
from tender_assistant.reports.writers import DocumentListWriter, ReportWriter


class DocumentSections:
    """Читает документ с требованиями и раскладывает его на разделы."""

    def __init__(self, data_parser: DataParser):
        self.data_parser = data_parser
        self._outline: Optional[MarkdownOutline] = None
        self._markdown: Optional[str] = None

    @property
    def markdown(self) -> str:
        if self._markdown is None:
            self._markdown = self.data_parser.origin_data()
        return self._markdown

    @property
    def outline(self) -> MarkdownOutline:
        if self._outline is None:
            self._outline = MarkdownOutline(self.markdown)
        return self._outline

    def sections_list(self) -> str:
        """Перечень заголовков для передачи в LLM. Пустая строка — заголовков нет."""
        return self.outline.as_list()

    def has_headings(self) -> bool:
        return self.outline.has_headings

    def section_text(self, header_name: str) -> str:
        """Текст раздела по названию заголовка.

        Если заголовок не найден, возвращается весь документ — по схеме работы
        документ без заголовков читается целиком.
        """
        section: Optional[Section] = self.outline.find(header_name)
        if section is None:
            print(
                f"[WARN] Раздел '{header_name}' не найден — читаем документ целиком",
                flush=True,
            )
            return self.markdown
        return section.text


class SectionsMatcher:
    """Ищет среди заголовков тот, что относится к перечню документов."""

    def __init__(
        self,
        ai_model: AIModel,
        response_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.response_post_processor = response_post_processor or SectionsMatcherResponse()
        self.ai_model = ai_model
        self.prompt_engine = prompt_engine or PromptEngine()
        self.prompt = settings.document_list_header_searcher_template

    def result(self, text_headers: str) -> List[str]:
        if not text_headers.strip():
            return []

        query_for_model = self.prompt_engine.render(
            self.prompt,
            fit_key="structured_text",
            fit_query="перечень документов состав заявки",
            structured_text=text_headers,
        )
        model_response = self.ai_model.response(query_for_model)
        headers = self.response_post_processor.report(model_response)
        print(f"[INFO] Найдены разделы со списком документов: {headers}", flush=True)
        return headers


class ContextMatcher:
    """Читает раздел и извлекает из него перечень документов."""

    def __init__(
        self,
        ai_model: AIModel,
        response_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.response_post_processor = response_post_processor or DocumentListResponse()
        self.ai_model = ai_model
        self.prompt_engine = prompt_engine or PromptEngine()
        self.prompt = settings.document_list_searcher_template

    def result(self, section_text: str) -> List[dict]:
        if not section_text.strip():
            return []

        query_for_model = self.prompt_engine.render(
            self.prompt,
            fit_key="section_text",
            fit_query="документы приложить к заявке перечень",
            section_text=section_text,
        )
        model_response = self.ai_model.response(query_for_model)
        return self.response_post_processor.report(model_response)


class NormativeChecker:
    """Проверяет предварительный перечень документов по нормативной базе."""

    def __init__(
        self,
        ai_model: AIModel,
        normative_base_folder: Optional[str] = None,
        response_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
        base_loader: NormativeBaseLoader = None,
    ):
        self.ai_model = ai_model
        self.normative_base_folder = normative_base_folder
        self.response_post_processor = response_post_processor or NormativeFilterResponse()
        self.prompt_engine = prompt_engine or PromptEngine()
        self.base_loader = base_loader or NormativeBaseLoader()
        self.prompt = settings.document_list_normative_filter_template
        self._base_text: Optional[str] = None

    @property
    def base_text(self) -> str:
        if self._base_text is None:
            self._base_text = self.base_loader.load(self.normative_base_folder)
        return self._base_text

    def result(self, documents: List[dict]) -> dict:
        """Возвращает {'documents': [...], 'excluded': [...]}."""
        if not documents:
            return {"documents": [], "excluded": []}

        if not self.base_text.strip():
            print(
                "[INFO] Нормативная база не задана — перечень принят без фильтрации",
                flush=True,
            )
            return {"documents": documents, "excluded": []}

        listing = "\n".join(
            f"{i + 1}. {d['name']}" + (f" ({d['note']})" if d.get("note") else "")
            for i, d in enumerate(documents)
        )

        query_for_model = self.prompt_engine.render(
            self.prompt,
            fit_key="normative_base",
            fit_query=listing,
            normative_base=self.base_text,
            documents=listing,
        )
        model_response = self.ai_model.response(query_for_model)
        checked = self.response_post_processor.report(model_response)

        # Модель могла отбросить всё — это почти всегда её ошибка,
        # исходный перечень надёжнее пустого.
        if not checked.get("documents"):
            print(
                "[WARN] Проверка по нормативной базе не оставила документов — "
                "используем исходный перечень",
                flush=True,
            )
            return {"documents": documents, "excluded": []}

        return checked


class DocumentList:
    """Этап 1: получить перечень документов, необходимых для заявки."""

    def __init__(
        self,
        report: BaseReport,
        document_sections: DocumentSections,
        sections_matcher: SectionsMatcher,
        context_matcher: ContextMatcher,
        normative_checker: NormativeChecker = None,
        writer: ReportWriter = None,
        results_path: Optional[str] = None,
    ):
        self.context_matcher = context_matcher
        self.sections_matcher = sections_matcher
        self.document_sections = document_sections
        self.normative_checker = normative_checker
        self.report = report or DocumentListReport()
        self.writer = writer or DocumentListWriter()
        self.results_path = results_path or settings.results_root

    def result(self) -> DocumentListResult:
        """Читает документ, находит раздел с перечнем документов и разбирает его.

        Если заголовков в документе нет, раздел не ищется — документ читается
        целиком. Полученный перечень проверяется по нормативной базе.
        """
        headers = self._find_headers()
        raw_documents = self._collect_documents(headers)
        checked = self._check_by_normative(raw_documents)

        result = DocumentListResult(
            documents=[RequiredDocument(**d) for d in checked["documents"]],
            excluded=[RequiredDocument(**d) for d in checked["excluded"]],
            source_headers=headers,
        )

        result.report_path = self.report.result(self._report_text(result))
        result.formatted_path = str(self.writer.write(result, self._formatted_output_path()))
        print(
            f"[INFO] Итоговый перечень документов: {len(result.documents)} шт. "
            f"(отброшено {len(result.excluded)})",
            flush=True,
        )
        return result

    def _formatted_output_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Path(self.results_path) / f"Перечень документов_{stamp}.docx"

    def _find_headers(self) -> List[str]:
        if not self.document_sections.has_headings():
            print("[INFO] Заголовки не найдены — документ читается целиком", flush=True)
            return []
        return self.sections_matcher.result(self.document_sections.sections_list())

    def _collect_documents(self, headers: List[str]) -> List[dict]:
        if not headers:
            return self.context_matcher.result(self.document_sections.markdown)

        collected: List[dict] = []
        seen = set()

        for header in headers:
            section_text = self.document_sections.section_text(header)
            for document in self.context_matcher.result(section_text):
                key = document["name"].strip().lower()
                if key not in seen:
                    seen.add(key)
                    collected.append(document)

        if not collected:
            print(
                "[WARN] В найденных разделах нет перечня документов — "
                "разбираем документ целиком",
                flush=True,
            )
            return self.context_matcher.result(self.document_sections.markdown)

        return collected

    def _check_by_normative(self, documents: List[dict]) -> dict:
        if self.normative_checker is None:
            return {"documents": documents, "excluded": []}
        return self.normative_checker.result(documents)

    @staticmethod
    def _report_text(result: DocumentListResult) -> str:
        lines = ["# Перечень документов для заявки", ""]

        if result.source_headers:
            lines.append("Источник: " + "; ".join(result.source_headers))
            lines.append("")

        lines += ["| № | Документ | Обязательность | Примечание |",
                  "| --- | --- | --- | --- |"]
        for i, doc in enumerate(result.documents, start=1):
            mandatory = "обязателен" if doc.mandatory else "по условию"
            lines.append(f"| {i} | {doc.name} | {mandatory} | {doc.note} |")

        if result.excluded:
            lines += ["", "## Исключено при проверке по нормативной базе", "",
                      "| Документ | Причина |", "| --- | --- |"]
            for doc in result.excluded:
                lines.append(f"| {doc.name} | {doc.note} |")

        return "\n".join(lines)
