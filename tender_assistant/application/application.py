from pathlib import Path
from typing import List

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import PostProcessor
from tender_assistant.ai.promt_builders import PromptEngine
from tender_assistant.core.parsers import Parser, DataParser
from tender_assistant.reports.writers import ReportWriter


class TenderQuery:
    def result(self, query_template: str, normative_base: str, tender_info: str)->str:
        pass


class TenderAIQuery(TenderQuery):
    def __init__(self, ai_model: AIModel,
                       tender_row_postprocessor: PostProcessor,
                       prompt_engine: PromptEngine,
                 ):
        self.prompt_engine = prompt_engine
        self.tender_row_postprocessor = tender_row_postprocessor
        self.ai_model = ai_model

    def result(self, query_template: str, normative_base: str, tender_info: str)->str:
        '''Searching for answer for query in query template in normative base and tender info. Return cleaned text from LLM'''
        query = self._prepare_query(query_template, normative_base, tender_info)
        response = self.ai_model.response(query)
        return self.tender_row_postprocessor.report(response)

    def _prepare_query(self, query_template, normative_base, tender_info):
        prompt = self.prompt_engine(query_template, normative_base, tender_info)
        query = ''
        result_query = prompt + query
        return result_query



class TenderForm:
    def prepare(self, *params):
        pass


class AITenderForm(TenderForm):
    def __init__(self,
                 tender_query: TenderQuery,
                 ):

        self.tender_query = tender_query

    def prepare(self,
                 md_template_form: str,
                 md_normative_base: str,
                 md_tender_info: str,):
        """Return fiiled template with info from normative nase amd tender info as Markdown file"""
        form_queries = self._extract_queries_from_form(md_template_form)
        results = []
        for form_query in form_queries:
            results.append(self.tender_query.result(form_query, md_normative_base, md_tender_info))
        return "".join(results)



    def _extract_queries_from_form(self, md_template_form)->List[str]:
        pass


class TenderApplication:
    def __init__(self,
                 application_template_path: str,
                 tender_info_path: str,
                 normative_base_folder: str,
                 tender_form: TenderForm,
                 report_writer: ReportWriter,
                 ):
        self.report_writer = report_writer
        self.tender_form = tender_form
        self.normative_base_folder = normative_base_folder
        self.tender_info_path = tender_info_path
        self.application_template_path = application_template_path



    def result(self):
        """Prepare and fill application template with info from notmative base or verified form"""
        md_template_form = DataParser(self.application_template_path).origin_data()
        md_tender_info_form = DataParser(self.tender_info_path).origin_data()
        md_normative_data = self._read_base(self.normative_base_folder)
        result_md = self._prepare_md_result(md_template_form, md_tender_info_form, md_normative_data)
        report_export = self._report(result_md)


    def _read_base(self, normative_base_folder):
        """Read and concat all data in folder. Return in Markdown format"""

    def _prepare_md_result(self,md_template_form, md_tender_info_form, md_normative_data):
        return self.tender_form.prepare(md_template_form, md_tender_info_form, md_normative_data)

    def _report(self, result_md):
        '''Save markdown in init template format with color filled fields (green - ok), red (no info), yelow (to check)'''
        self.report_writer.write(result_md, output_path=Path(self.application_template_path + ''))

