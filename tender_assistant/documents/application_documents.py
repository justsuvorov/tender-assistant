from typing import List
from zoneinfo import available_timezones

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import PostProcessor
from tender_assistant.core.config import settings
from tender_assistant.reports.report_export import ReportExport, ApplicationDocumentsReport


class TitleMatcher:
    def __init__(self,
                 ai_model: AIModel,
                 title_matcher_post_processor: PostProcessor,
                 ):
        self.title_matcher_post_processor = title_matcher_post_processor
        self.ai_model = ai_model
        self.prompt = settings.document_name_matcher


    def document_name(self, target_name, name_list ):
        query_for_model = self._prepare_query(target_name, name_list)
        response = self.ai_model.response(query_for_model)
        return self.title_matcher_post_processor.report(response)

    def _prepare_query(self, target_name, name_list)->str:
        pass


class ApplicationDocuments:
    def __init__(self,
                 documents_path: str,
                 documents_list: List[str],
                 matcher: TitleMatcher,
                 result_folder_name: str,
                 report: ReportExport=ApplicationDocumentsReport(),
                 ):
        self.report = report
        self.result_folder_name = result_folder_name
        self.matcher = matcher
        self.documents_list = documents_list
        self.documents_path = documents_path


    def result_set(self):
        """Searching for documents in documents path by file name. and copy it in result folder name"""
        available_documents = self._documents_in_path(self.documents_path)
        for document_name in self.documents_list:
            self._copy_document(file_name=self._search_in_files(document_name=document_name,
                                                                documents=available_documents))

        self._prepare_report()

    def _search_in_files(self, document_name, documents)->str:
        '''Returns file_name due to llm search by target name in doc list'''
        return self.matcher.document_name(document_name, documents)

    def _copy_document(self, file_name):
        """Copy in result_folder"""
        pass

    def _prepare_report(self):
        """Prepare report as table with init doc list and status (Found, not found, doc to check)"""