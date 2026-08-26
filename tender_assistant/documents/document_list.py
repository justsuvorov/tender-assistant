from dataclasses import dataclass
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


@dataclass
class ExtractedDocuments:
    """Сырой результат разбора требований: что нашли и откуда."""

    headers: List[str]
    documents: List[dict]


class DocumentListExtractor:
    """Достаёт перечень документов из требований тендера.

    Три шага схемы работы — найти заголовки, выбрать раздел про документы,
    прочитать его — вместе, потому что порознь они бессмысленны: результат
    каждого нужен только следующему. Наружу отдаётся один вызов ``extract()``.
    """

    def __init__(
        self,
        document_sections: DocumentSections,
        sections_matcher: SectionsMatcher,
        context_matcher: ContextMatcher,
    ):
        self.document_sections = document_sections
        self.sections_matcher = sections_matcher
        self.context_matcher = context_matcher

    def extract(self) -> ExtractedDocuments:
        headers = self._find_headers()
        return ExtractedDocuments(
            headers=headers, documents=self._collect_documents(headers)
        )

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


class DocumentListOutput:
    """Куда и чем сохраняется оформленный перечень документов."""

    def __init__(self, results_path: Optional[str] = None, writer: ReportWriter = None):
        self.results_path = results_path or settings.results_root
        self.writer = writer or DocumentListWriter()

    def save(self, result: DocumentListResult) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = Path(self.results_path) / f"Перечень документов_{stamp}.docx"
        return self.writer.write(result, output)


class DocumentList:
    """Этап 1: получить перечень документов, необходимых для заявки.

    Оркестратор: извлечь перечень → проверить по нормативной базе → сохранить.
    Чтение документа и запись файлов живут в DocumentListExtractor и
    DocumentListOutput.
    """

    def __init__(
        self,
        extractor: DocumentListExtractor,
        normative_checker: NormativeChecker = None,
        output: DocumentListOutput = None,
        report: BaseReport = None,
    ):
        self.extractor = extractor
        self.normative_checker = normative_checker
        self.output = output or DocumentListOutput()
        self.report = report or DocumentListReport()

    def result(self) -> DocumentListResult:
        extracted = self.extractor.extract()
        checked = self._check_by_normative(extracted.documents)

        result = DocumentListResult(
            documents=[RequiredDocument(**d) for d in checked["documents"]],
            excluded=[RequiredDocument(**d) for d in checked["excluded"]],
            source_headers=extracted.headers,
        )

        result.report_path = self.report.result(self._report_text(result))
        result.formatted_path = str(self.output.save(result))
        print(
            f"[INFO] Итоговый перечень документов: {len(result.documents)} шт. "
            f"(отброшено {len(result.excluded)})",
            flush=True,
        )
        return result

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
