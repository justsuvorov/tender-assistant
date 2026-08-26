from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Этап 1: перечень документов ───────────────────────────────────────────────

class RequiredDocument(BaseModel):
    """Документ, который требуется приложить к заявке."""

    name: str = Field(..., description="Название документа")
    mandatory: bool = Field(True, description="Обязателен ли документ")
    note: str = Field("", description="Уточнение из текста конкурсной документации")


class DocumentListResult(BaseModel):
    """Результат первого этапа — перечень документов для заявки."""

    documents: List[RequiredDocument] = Field(default_factory=list)
    excluded: List[RequiredDocument] = Field(
        default_factory=list, description="Отброшено при проверке по нормативной базе"
    )
    source_headers: List[str] = Field(
        default_factory=list, description="Заголовки разделов, из которых взят перечень"
    )
    report_path: Optional[str] = None
    formatted_path: Optional[str] = Field(
        None, description="Оформленный перечень документов (Word)"
    )


# ── Этап 2: подготовка файлов ─────────────────────────────────────────────────

class DocumentStatus(str, Enum):
    FOUND = "Есть"
    MISSING = "Нет"
    CHECK = "Проверить"


class PreparedDocument(BaseModel):
    """Строка отчёта по подготовке файлов."""

    name: str
    status: DocumentStatus = DocumentStatus.MISSING
    files: List[str] = Field(default_factory=list, description="Скопированные файлы")
    note: str = ""


class ApplicationDocumentsResult(BaseModel):
    rows: List[PreparedDocument] = Field(default_factory=list)
    result_folder: Optional[str] = None
    report_path: Optional[str] = None


# ── Этап 3: заполнение заявки ─────────────────────────────────────────────────

class FieldStatus(str, Enum):
    FOUND = "found"     # зелёный
    CHECK = "check"     # жёлтый
    MISSING = "missing" # красный


class FormField(BaseModel):
    """Поле шаблона заявки, которое нужно заполнить."""

    label: str
    anchor: str = ""
    kind: str = "line"


class FilledField(FormField):
    """Заполненное поле шаблона."""

    value: str = ""
    status: FieldStatus = FieldStatus.MISSING
    source: str = ""
    note: str = ""


class TenderApplicationResult(BaseModel):
    fields: List[FilledField] = Field(default_factory=list)
    filled_path: Optional[str] = None
    report_path: Optional[str] = None


# ── Итог всего пайплайна ──────────────────────────────────────────────────────

class AssistantResult(BaseModel):
    request_id: Optional[int] = None
    document_list: DocumentListResult = Field(default_factory=DocumentListResult)
    prepared_documents: ApplicationDocumentsResult = Field(
        default_factory=ApplicationDocumentsResult
    )
    application: TenderApplicationResult = Field(default_factory=TenderApplicationResult)
