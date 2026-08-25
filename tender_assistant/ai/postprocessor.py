import re
from abc import ABC


class PostProcessor:
    """Interface for preparing reports and answers from LLM"""

    def report(self, raw_text: str) -> str:
        pass



class SectionsMatcherResponse(PostProcessor):
    def __init__(self):
        pass

    def report(self, raw_text: str)->str:
        return self._clean_text(raw_text)

    def _clean_text(self, raw_text):
        pass


class DocumentListResponse(PostProcessor):
    def __init__(self):
        pass

    def report(self, raw_text: str)->str:
        return self._clean_text(raw_text)

    def _clean_text(self, raw_text):
        pass