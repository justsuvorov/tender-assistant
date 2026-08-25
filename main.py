from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from tender_assistant.ai.model import GeminiModel, ModelFactory
from tender_assistant.ai.postprocessor import SectionsMatcherResponse, DocumentListResponse
from tender_assistant.ai.promt_builders import PromptEngine
from tender_assistant.application.application import TenderApplication, AITenderForm, TenderAIQuery
from tender_assistant.core.parsers import DataParser
from tender_assistant.documents.application_documents import ApplicationDocuments, TitleMatcher
from tender_assistant.documents.document_list import DocumentList, DocumentSections, SectionsMatcher, ContextMatcher
from tender_assistant.models.request import APIRequest
from tender_assistant.reports.report_export import DocumentListReport

app = FastAPI()


@app.post("/api/update")
def submit(request: APIRequest):
    file_path = request.file_path
    llm_model = ModelFactory.create()
    document_list = DocumentList(report=DocumentListReport(),
                                 document_sections=DocumentSections(data_parser=DataParser(file_path=file_path)),
                                 sections_matcher=SectionsMatcher(ai_model=llm_model,
                                                                  response_post_processor=SectionsMatcherResponse(),
                                                                  ),
                                 context_matcher=ContextMatcher(ai_model=llm_model,
                                                                response_post_processor=DocumentListResponse(),
                                                                )


                                 ).result()

    documents_for_application = ApplicationDocuments(documents_path=request.documents_folder_path,
                                                     documents_list=document_list,
                                                     matcher=TitleMatcher(ai_model=llm_model,
                                                                          title_matcher_post_processor=TitleMatcherPostProccessor),
                                                     result_folder_name=request.result_folder_name,
                                                     )
    documents_for_application.result_set()

    result = TenderApplication(application_template_path=request.application_template_path,
                              tender_info_path=request.tender_info_path,
                              normative_base_folder=request.normative_base_folder,
                              tender_form= AITenderForm(tender_query=TenderAIQuery(ai_model=llm_model,
                                                                                   tender_row_postprocessor=TenderRowPostProcessor(),
                                                                                   prompt_engine=PromptEngine(),
                                                                                   )
                                                        ),
                              report_writer=TenderReportWriter(),
                              )


    return JSONResponse(content=jsonable_encoder(result))






if __name__ == '__main__':


# See PyCharm help at https://www.jetbrains.com/help/pycharm/
