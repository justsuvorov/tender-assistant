from typing import List

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import PostProcessor, SectionsMatcherResponse, DocumentListResponse
from tender_assistant.core.config import settings
from tender_assistant.core.parsers import DataParser
from tender_assistant.reports.report_export import BaseReport


class DocumentSections:
    def __init__(self,
                 data_parser: DataParser,
                 ):
        self.data_parser = data_parser

    def sections_list(self)->str:
        pass


class SectionsMatcher:
    def __init__(self,
                 ai_model: AIModel,
                 response_post_processor: PostProcessor = SectionsMatcherResponse()):
        self.response_post_processor = response_post_processor
        self.ai_model = ai_model
        self.prompt = settings.document_list_searcher_template


    def result(self, text_headers: str)->str:
        query_for_model = "".join(
                [text_headers , self.prompt]
            )
        model_response = self.ai_model.response(query_for_model)
        result_header_name = self.response_post_processor.report(model_response)
        return result_header_name


class ContextMatcher:
    def __init__(self,
                 ai_model: AIModel,
                 data_parser: DataParser,
                 response_post_processor: PostProcessor = DocumentListResponse(),
                 ):
        self.data_parser = data_parser
        self.response_post_processor = response_post_processor
        self.ai_model = ai_model
        self.prompt = settings.document_list_header_searcher_template


    def result(self, full_text: str, header_name: str) -> str:
        text = self._section_text_by_header_name(full_text, header_name)
        query_for_model = "".join(
            [text, self.prompt]
        )
        model_response = self.ai_model.response(query_for_model)
        document_names_list = self.response_post_processor.report(model_response)
        return document_names_list

    def _section_text_by_header_name(self, full_text, header_name):
        pass


class DocumentList:
    def __init__(self,
                 report: BaseReport,
                 document_sections:DocumentSections,
                 sections_matcher: SectionsMatcher,
                 context_matcher: ContextMatcher,
                 ):
        self.context_matcher = context_matcher
        self.sections_matcher = sections_matcher
        self.document_sections = document_sections
        self.report = report

    def result(self)->List[str]:
        """Reads document. Extracts paragraph names and headers. Searching for suitible header name for documents list info. Read this paragraph and returns documnets list"""
        document_sections = self.document_sections.sections_list()
        section_with_documents_text = self. sections_matcher.result(text_headers=document_sections)
        document_list = self.context_matcher.result(section_with_documents_text)
        report_text = self._prepare_report_text()
        report = self.report.result(report_text)
        return document_list