from dataclasses import dataclass


@dataclass
class ProcessingTask:
    message_id: int
    user_id: int = None
    priority: int = 0


class Preprocessor:
    def query(self):
        pass

