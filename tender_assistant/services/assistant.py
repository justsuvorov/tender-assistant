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


class TenderAssistantService:
    """Собирает и выполняет три этапа подготовки тендерной заявки.

    1. Перечень документов — из требований тендера, с проверкой по нормативной базе.
    2. Подготовка файлов — семантический поиск в архиве и сбор комплекта.
    3. Заполнение шаблона заявки — по базе знаний и требованиям тендера.

    Этапы связаны по данным: перечень документов из этапа 1 идёт в этап 2.
    Этап 3 от них не зависит и выполняется в любом случае.
    """

    def __init__(self, request: APIRequest, ai_model: AIModel = None):
        self.request = request
        self.ai_model = ai_model or ModelFactory.create()
        self.prompt_engine = PromptEngine()

    def result(self) -> AssistantResult:
        result = AssistantResult(request_id=self.request.message_id)

        result.document_list = self._document_list().result()
        result.prepared_documents = self._application_documents(
            result.document_list.documents
        ).result_set()
        result.application = self._tender_application().result()

        return result

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
