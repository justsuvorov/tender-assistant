from dataclasses import dataclass

from voice_assistant.ai.encoders import Encoder
from voice_assistant.ai.promt_builders import PromptEngine
from voice_assistant.models.schema import DBSpeakingObject


@dataclass
class ProcessingTask:
    message_id: int
    user_id: int = None
    priority: int = 0


class Preprocessor:
    def query(self):
        pass


class VoicePreprocessor(Preprocessor):
    def __init__(self,
                 db_speaking_object: DBSpeakingObject,
                 request: ProcessingTask,
                 encoder: Encoder,
                 prompt_engine: PromptEngine,
                 ):
        self._db = db_speaking_object
        self._request = request
        self._voice_encoder = encoder
        self._prompt_engine = prompt_engine

    def query(self):
        origin_data = self._db.get_origin_data(self._request.message_id)
        text_content = self._voice_encoder.prepared_data(origin_data["path"])
        references = self._db.get_style_reference(limit=3)

        return self._prompt_engine.build(
            source_text=text_content,
            context=references
        )
