from voice_assistant.ai.model import AIModel
from voice_assistant.ai.postprocessor import PostProcessor
from voice_assistant.ai.preprocessor import Preprocessor
from voice_assistant.reports.report_export import ReportExport

class AIAssistantService:
    def __init__(self,
                 preprocessor: Preprocessor,
                 postprocessor: PostProcessor,
                 ai_model: AIModel,
                 report_export: ReportExport,
                 ):
        self._preprocessor = preprocessor
        self._postprocessor = postprocessor
        self._model = ai_model
        self._report_export = report_export

    def result(self):
        model_query = self._preprocessor.query()
        model_response = self._model.response(model_query)
        report = self._postprocessor.report(model_response)
        return self._report_export.response(report_text=report)

