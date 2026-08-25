from voice_assistant.ai.preprocessor import ProcessingTask
from voice_assistant.models.schema import DBSpeakingObject


class BaseReport:
    """Prepare and save report in markdown"""
    def result(self, report_text: str):
        pass


class DocumentListReport(BaseReport):
    def __init__(self,
                 ):
        pass

    def result(self, report_text: str):
        pass


class ApplicationDocumentsReport(BaseReport):


    def result(self, report_text: str):
        pass


class ReportExport(BaseReport):
    def __init__(self, db_speaking: DBSpeakingObject,
                 processing_task: ProcessingTask):

        self._db = db_speaking
        self._processing_task = processing_task


    def result(self, report_text: str) -> dict:
        """
        Записывает результат в БД и формирует словарь для JSONResponse.
        """
        message_id = self._processing_task.message_id
        try:
            self._db.update_voice_post(
                message_id=message_id, 
                post_text=report_text
            )
            db_status = "saved"
        except Exception as e:
            # Логируем, но не "роняем" API, чтобы пользователь всё равно получил текст
            db_status = f"error: {str(e)}"

        # 2. Формируем структуру для Telegram / UI
        # Этот словарь уйдет в jsonable_encoder
        return {
            "message_id": message_id,
            "status": "success",
            "db_status": db_status,
            "payload": {
                "text": report_text,
                "format": "telegram_markdown", # Подсказка для UI
            }
        }
