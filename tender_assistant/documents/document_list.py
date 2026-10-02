import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import List, Optional, Tuple

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
from tender_assistant.reports.report_export import BaseReport, DocumentListReport, md_cell
from tender_assistant.reports.writers import DocumentListWriter, ReportWriter

logger = logging.getLogger(__name__)

# Сколько символов начала раздела показывать модели рядом с заголовком.
_PREVIEW_CHARS = 200

# Строки с типовыми названиями документов: по их числу раздел признаётся
# перечнем документов независимо от выбора модели.
_DOCUMENT_WORDS = re.compile(
    r"выписк|устав|копи[яиюей]|справк|свидетельств|лицензи|декларац|доверенност|"
    r"бухгалтерск|баланс|сертификат|анкет|егрюл|егрип|решени\w* об одобрении",
    re.IGNORECASE,
)
_MIN_DOCUMENT_LINES = 4
_MAX_EXTRA_SECTIONS = 3
_MAX_SECTION_SHARE = 0.5

# Ссылка на другой подраздел вместо документа: «Документы, указанные
# в подразделе 2.1 настоящей документации».
_REFERENCE = re.compile(
    r"(?:указанн|перечисленн|предусмотренн|приведенн|приведённ)\w*\s+в\s+"
    r"(?:(?:под)?раздел\w*|пункт\w*|п\.)\s*(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_DOTTED_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s")
_MAX_REFERENCE_CHARS = 20000


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
        """Заголовки с началом текста каждого раздела — вход LLM для выбора раздела.

        По одному заголовку модель выбирает нестабильно: раздел с полным
        перечнем может называться не так, как ожидается, а «Состав заявки» —
        оказаться абзацем со ссылкой на другой раздел. Начало текста
        показывает, что внутри. Пустая строка — заголовков нет.
        """
        lines = []
        for i, section in enumerate(self.outline.sections, start=1):
            lines.append(f"{i}. {section.title}")
            preview = self._preview(section)
            if preview:
                lines.append(f"   — {preview}")
        return "\n".join(lines)

    def _preview(self, section: Section) -> str:
        # У раздела-«шапки» (4. → 4.1, 4.2) своё тело пустое — показываем
        # начало подразделов, иначе модель увидит только заголовок.
        if section.body:
            text = section.body
        else:
            text = self.outline.subtree_text(section)[len(section.text):]
        text = " ".join(text.split())
        if len(text) > _PREVIEW_CHARS:
            text = text[:_PREVIEW_CHARS].rstrip() + "…"
        return text

    def has_headings(self) -> bool:
        return self.outline.has_headings

    def section_text(self, section: Section) -> str:
        """Текст раздела вместе со всеми подразделами."""
        return self.outline.subtree_text(section)

    def candidate_sections(self, headers: List[str]) -> List[Section]:
        """Разделы, из которых извлекается перечень: выбор модели + страховка.

        Страховка — разделы, где много строк с типовыми названиями документов
        (выписка, устав, справка…). Она детерминирована: раздел с полным
        перечнем попадает в обработку при каждом запуске, даже если модель
        в этот раз выбрала по заголовкам другой. Разделы, вложенные в уже
        выбранные, отбрасываются — их текст и так входит в родителя.
        """
        picked: List[Section] = []
        for header in headers:
            section = self.outline.find(header)
            if section is None:
                logger.warning("Раздел «%s» из ответа модели не найден в документе", header)
            elif all(section is not p for p in picked):
                picked.append(section)

        for section in self._document_heavy_sections():
            if all(section is not p for p in picked):
                logger.info("Раздел «%s» добавлен по содержанию: в нём перечислены "
                            "документы", section.title)
                picked.append(section)

        order = {id(s): i for i, s in enumerate(self.outline.sections)}
        result: List[Section] = []
        for section in sorted(picked, key=lambda s: order[id(s)]):
            if not any(self.outline.contains(parent, section) for parent in result):
                result.append(section)
        return result

    def _document_heavy_sections(self) -> List[Section]:
        # Считаем вместе с подразделами: перечень раздела 5 часто разложен по
        # 5.1, 5.2…, а у самого «5.» тело пустое. Разделы-«корни», которые
        # охватывают почти весь документ (заголовок тома), не берём — это
        # было бы чтение документа целиком под видом раздела. Корень узнаём по
        # структуре, а не по длине: в коротком документе раздел с перечнем
        # и так занимает половину текста.
        all_sections = self.outline.sections
        limit = len(all_sections) * _MAX_SECTION_SHARE
        scored = []
        for section in all_sections:
            if len(self.outline.descendants(section)) > limit:
                continue
            text = self.outline.subtree_text(section)
            hits = sum(1 for line in text.splitlines() if _DOCUMENT_WORDS.search(line))
            if hits >= _MIN_DOCUMENT_LINES:
                scored.append((hits, len(scored), section))

        # Родитель набирает не меньше своих подразделов и идёт раньше них —
        # «5.1» и «5.2» не займут места других разделов рядом с «5.».
        chosen: List[Section] = []
        for _, _, section in sorted(scored, key=lambda item: (-item[0], item[1])):
            if any(self.outline.contains(parent, section) for parent in chosen):
                continue
            chosen.append(section)
            if len(chosen) == _MAX_EXTRA_SECTIONS:
                break
        return chosen

    def numbered_text(self, number: str) -> Tuple[str, Optional[Section]]:
        """Текст подраздела по номеру («2.1») — для ссылок «указанные в п. 2.1».

        Сначала среди заголовков; если подраздел оформлен обычным абзацем
        («2.1. В состав заявки входят…»), — по тексту документа до следующего
        пункта другого номера.
        """
        section = self.outline.find_by_number(number)
        if section is not None:
            return self.outline.subtree_text(section), section
        return self._numbered_paragraphs(number), None

    def _numbered_paragraphs(self, number: str) -> str:
        start = re.compile(
            r"^\s*(?:#+\s*)?(?:(?:под)?раздел\s+|пункт\s+|п\.\s*)?"
            + re.escape(number) + r"\.?(?:\s|$)",
            re.IGNORECASE,
        )
        lines = self.markdown.splitlines()
        for first, line in enumerate(lines):
            if start.match(line):
                break
        else:
            return ""

        collected = [lines[first]]
        size = len(lines[first])
        for line in lines[first + 1:]:
            if line.lstrip().startswith("#"):
                break
            other = _DOTTED_NUMBER.match(line)
            if other and other.group(1) != number and not other.group(1).startswith(number + "."):
                break
            collected.append(line)
            size += len(line)
            if size > _MAX_REFERENCE_CHARS:
                break
        return "\n".join(collected).strip()


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
        logger.info(f"Найдены разделы со списком документов: {headers}")
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
            logger.info("Нормативная база не задана — перечень принят без фильтрации")
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
            logger.warning("Проверка по нормативной базе не оставила документов — "
                "используем исходный перечень")
            return {"documents": documents, "excluded": []}

        for document in checked.get("excluded", []):
            logger.info(
                "Исключён по нормативной базе: «%s» — %s",
                document.get("name", "?"), document.get("note") or "причина не указана",
            )
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
        sections = self.document_sections.candidate_sections(self._find_headers())
        documents = self._collect_documents(sections)
        documents = self._resolve_references(documents, sections)
        return ExtractedDocuments(
            headers=[section.title for section in sections], documents=documents
        )

    def _find_headers(self) -> List[str]:
        if not self.document_sections.has_headings():
            logger.info("Заголовки не найдены — документ читается целиком")
            return []
        return self.sections_matcher.result(self.document_sections.sections_list())

    def _collect_documents(self, sections: List[Section]) -> List[dict]:
        if not sections:
            logger.info("Подходящие разделы не найдены — документ читается целиком")
            return self.context_matcher.result(self.document_sections.markdown)

        collected: List[dict] = []
        for section in sections:
            found = self.context_matcher.result(self.document_sections.section_text(section))
            logger.info("Раздел «%s»: документов в перечне — %d", section.title, len(found))
            _merge(collected, found)

        if not collected:
            logger.warning("В найденных разделах нет перечня документов — "
                "разбираем документ целиком")
            return self.context_matcher.result(self.document_sections.markdown)

        return collected

    def _resolve_references(
        self, documents: List[dict], sections: List[Section]
    ) -> List[dict]:
        """Пункт-ссылка («указанные в подразделе 2.1») заменяется перечнем оттуда.

        Если подраздел уже обработан в составе выбранных разделов, ссылка
        просто убирается. Если его нет в документе (например, он в другом
        томе), пункт остаётся с пометкой: выдать ссылку за документ нельзя,
        а молча потерять — тоже.
        """
        result: List[dict] = []
        resolved = {}

        for document in documents:
            match = _REFERENCE.search(f"{document['name']} {document.get('note', '')}")
            if not match:
                _merge(result, [document])
                continue

            number = match.group(1)
            if number not in resolved:
                resolved[number] = self._documents_by_reference(number, sections)
            found = resolved[number]

            if found is None:
                logger.warning("Пункт «%s» ссылается на подраздел %s, которого нет "
                               "в документе", document["name"], number)
                note = (f"Ссылка на подраздел {number}: в документе с требованиями "
                        f"его нет — проверьте другие тома документации")
                _merge(result, [dict(document, note=note)])
            else:
                _merge(result, found)
        return result

    def _documents_by_reference(
        self, number: str, sections: List[Section]
    ) -> Optional[List[dict]]:
        text, section = self.document_sections.numbered_text(number)
        if not text:
            return None
        outline = self.document_sections.outline
        if section is not None and any(
            parent is section or outline.contains(parent, section) for parent in sections
        ):
            logger.info("Подраздел %s уже входит в обработанные разделы", number)
            return []
        found = self.context_matcher.result(text)
        logger.info("Подраздел %s по ссылке: документов в перечне — %d", number, len(found))
        return found


def _merge(collected: List[dict], found: List[dict]) -> None:
    """Добавляет документы без повторов (по названию без учёта регистра)."""
    seen = {document["name"].strip().lower() for document in collected}
    for document in found:
        key = document["name"].strip().lower()
        if key not in seen:
            seen.add(key)
            collected.append(document)


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
        logger.info(f"Итоговый перечень документов: {len(result.documents)} шт. "
            f"(отброшено {len(result.excluded)})")
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
            lines.append(f"| {i} | {md_cell(doc.name)} | {mandatory} | {md_cell(doc.note)} |")

        if result.excluded:
            lines += ["", "## Исключено при проверке по нормативной базе", "",
                      "| Документ | Причина |", "| --- | --- |"]
            for doc in result.excluded:
                lines.append(f"| {md_cell(doc.name)} | {md_cell(doc.note)} |")

        return "\n".join(lines)
