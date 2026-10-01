import logging
import time
from typing import Callable, List, Optional, TypeVar

from tender_assistant.ai.model import AIModel, ModelFactory
from tender_assistant.ai.postprocessor import (
    DocumentListResponse,
    FormFieldsResponse,
    InlineBlanksResponse,
    NormativeFilterResponse,
    SectionsMatcherResponse,
    TenderRowPostProcessor,
    TitleMatcherPostProcessor,
)
from tender_assistant.ai.promt_builders import PromptEngine
from tender_assistant.application.application import (
    AITenderForm,
    ApplicationOutput,
    CompositeTenderForm,
    FormTemplate,
    InlineBlanksForm,
    InlineBlanksQuery,
    KnowledgeBaseContext,
    TenderAIQuery,
    TenderApplication,
)
from tender_assistant.core.parsers import DataParser
from tender_assistant.core.pydantic_models import AssistantResult
from tender_assistant.documents.application_documents import (
    ApplicationDocuments,
    ComplectFolder,
    DocumentArchive,
    TitleMatcher,
)
from tender_assistant.documents.document_list import (
    ContextMatcher,
    DocumentList,
    DocumentListExtractor,
    DocumentListOutput,
    DocumentSections,
    NormativeChecker,
    SectionsMatcher,
)
from tender_assistant.models.request import APIRequest
from tender_assistant.reports.report_export import (
    ApplicationDocumentsReport,
    DocumentListReport,
    TenderApplicationReport,
)
from tender_assistant.reports.writers import TenderReportWriter

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Названия этапов для журнала — ключи совпадают с AssistantResult.errors.
_STAGE_TITLES = {
    "document_list": "Этап 1. Перечень документов",
    "prepared_documents": "Этап 2. Подготовка документов",
    "application": "Этап 3. Заполнение заявки",
}


class TenderAssistantService:
    """Собирает и выполняет три этапа подготовки тендерной заявки.

    1. Перечень документов — из требований тендера, с проверкой по нормативной базе.
    2. Подготовка файлов — семантический поиск в архиве и сбор комплекта.
    3. Заполнение шаблона заявки — по базе знаний и требованиям тендера.

    Этапы связаны по данным: перечень документов из этапа 1 идёт в этап 2.
    Этап 3 от них не зависит и выполняется в любом случае — даже если
    этап 1 или 2 упал; такие сбои возвращаются в ``AssistantResult.errors``.
    """

    def __init__(self, request: APIRequest, ai_model: AIModel = None):
        self.request = request
        self.ai_model = ai_model or ModelFactory.create()
        self.prompt_engine = PromptEngine()

    def result(self) -> AssistantResult:
        # Ошибки ввода (нет файла, неподдерживаемый формат) — до первого
        # обращения к модели: такой запрос не должен тратить токены.
        self._validate_inputs()
        self._log_request()

        started = time.perf_counter()
        result = AssistantResult(request_id=self.request.message_id)
        failures: List[Exception] = []

        document_list = self._run_stage(
            "document_list", lambda: self._document_list().result(), result, failures
        )
        if document_list is not None:
            result.document_list = document_list
            prepared = self._run_stage(
                "prepared_documents",
                lambda: self._application_documents(document_list.documents).result_set(),
                result,
                failures,
            )
            if prepared is not None:
                result.prepared_documents = prepared
        else:
            result.errors["prepared_documents"] = (
                "Этап пропущен: не получен перечень документов"
            )
            logger.warning("%s пропущен: не получен перечень документов",
                           _STAGE_TITLES["prepared_documents"])

        application = self._run_stage(
            "application", lambda: self._tender_application().result(), result, failures
        )
        if application is not None:
            result.application = application

        # Ни один этап не выполнился — отдавать пустой «успешный» ответ нельзя:
        # поднимаем первую ошибку, main.py превратит её в 400/502.
        # Этап 2 без этапа 1 не запускается, поэтому достаточно проверить 1 и 3.
        if document_list is None and application is None:
            logger.error("Ни один этап не выполнен за %.1f с", time.perf_counter() - started)
            raise failures[0]

        if result.errors:
            logger.warning(
                "Обработка завершена частично за %.1f с, не выполнены: %s",
                time.perf_counter() - started, ", ".join(result.errors),
            )
        else:
            logger.info("Обработка завершена за %.1f с", time.perf_counter() - started)
        return result

    def _log_request(self) -> None:
        request = self.request
        logger.info(
            "Входные данные:\n"
            "  требования: %s\n"
            "  шаблон заявки: %s\n"
            "  архив документов: %s\n"
            "  нормативная база: %s\n"
            "  база знаний: %s\n"
            "  результаты: %s (комплект: %s)",
            request.file_path,
            request.application_template_path,
            request.documents_folder_path,
            request.normative_base_folder or "не задана",
            request.knowledge_base_folder or "не задана (используется нормативная)",
            request.results_path,
            request.result_folder_name or "-",
        )

    def _validate_inputs(self) -> None:
        """Файлы, без которых этапы 1 и 3 не начнутся: существуют и читаются."""
        DataParser(file_path=self.request.file_path)
        DataParser(file_path=self.request.application_template_path)

    @staticmethod
    def _run_stage(
        name: str,
        stage: Callable[[], T],
        result: AssistantResult,
        failures: List[Exception],
    ) -> Optional[T]:
        """Выполняет этап; сбой записывается в result.errors, а не обрывает запрос.

        Этап 3 не зависит от этапов 1–2: ошибка модели на перечне документов
        не должна отнимать у пользователя заполненную заявку, и наоборот.
        """
        title = _STAGE_TITLES.get(name, name)
        logger.info("%s: старт", title)
        started = time.perf_counter()
        try:
            value = stage()
        except Exception as exc:
            # exception() — с полным traceback: по одной строке «не выполнен»
            # причину сбоя глубоко в разборе документа не найти.
            logger.exception(
                "%s: не выполнен через %.1f с — %s", title,
                time.perf_counter() - started, exc,
            )
            result.errors[name] = str(exc) or type(exc).__name__
            failures.append(exc)
            return None
        logger.info("%s: завершён за %.1f с", title, time.perf_counter() - started)
        return value

    # ── Этап 1 ────────────────────────────────────────────────────────────────

    def _document_list(self) -> DocumentList:
        report = DocumentListReport(output_dir=self.request.results_path)

        return DocumentList(
            extractor=DocumentListExtractor(
                document_sections=DocumentSections(
                    data_parser=DataParser(file_path=self.request.file_path)
                ),
                sections_matcher=SectionsMatcher(
                    ai_model=self.ai_model,
                    response_post_processor=SectionsMatcherResponse(),
                    prompt_engine=self.prompt_engine,
                ),
                context_matcher=ContextMatcher(
                    ai_model=self.ai_model,
                    response_post_processor=DocumentListResponse(),
                    prompt_engine=self.prompt_engine,
                ),
            ),
            normative_checker=NormativeChecker(
                ai_model=self.ai_model,
                normative_base_folder=self.request.normative_base_folder,
                response_post_processor=NormativeFilterResponse(),
                prompt_engine=self.prompt_engine,
            ),
            output=DocumentListOutput(results_path=self.request.results_path),
            report=report,
        )

    # ── Этап 2 ────────────────────────────────────────────────────────────────

    def _application_documents(self, documents) -> ApplicationDocuments:
        report = ApplicationDocumentsReport(output_dir=self.request.results_path)

        return ApplicationDocuments(
            archive=DocumentArchive(self.request.documents_folder_path),
            documents_list=documents,
            matcher=TitleMatcher(
                ai_model=self.ai_model,
                title_matcher_post_processor=TitleMatcherPostProcessor(),
                prompt_engine=self.prompt_engine,
            ),
            complect=ComplectFolder(
                results_path=self.request.results_path,
                folder_name=self.request.result_folder_name,
            ),
            report=report,
        )

    # ── Этап 3 ────────────────────────────────────────────────────────────────

    def _tender_application(self) -> TenderApplication:
        report = TenderApplicationReport(output_dir=self.request.results_path)

        # База знаний для заявки может быть отдельной от нормативной базы,
        # использованной при проверке перечня документов.
        knowledge_base = (
            self.request.knowledge_base_folder or self.request.normative_base_folder
        )

        # Два независимых заполнителя на один шаблон: построчный по markdown
        # и пропуски внутри абзацев по объектной модели docx.
        tender_form = CompositeTenderForm([
            AITenderForm(
                tender_query=TenderAIQuery(
                    ai_model=self.ai_model,
                    tender_row_postprocessor=TenderRowPostProcessor(),
                    prompt_engine=self.prompt_engine,
                ),
                fields_post_processor=FormFieldsResponse(),
                prompt_engine=self.prompt_engine,
            ),
            InlineBlanksForm(
                inline_query=InlineBlanksQuery(
                    ai_model=self.ai_model,
                    response_post_processor=InlineBlanksResponse(),
                    prompt_engine=self.prompt_engine,
                ),
            ),
        ])

        return TenderApplication(
            template=FormTemplate(self.request.application_template_path),
            context=KnowledgeBaseContext(
                tender_info_path=self.request.file_path,
                knowledge_base_folder=knowledge_base,
            ),
            tender_form=tender_form,
            output=ApplicationOutput(
                results_path=self.request.results_path,
                report_writer=TenderReportWriter(),
            ),
            report=report,
        )
