from typing import Optional

from pydantic import BaseModel, Field


class APIRequest(BaseModel):
    """Схема входящего запроса на подготовку тендерной заявки."""

    message_id: int = Field(..., description="Уникальный ID запроса в базе данных")
    user_id: Optional[int] = Field(None, description="ID пользователя (опционально)")
    priority: int = Field(0, description="Приоритет обработки")

    file_path: str = Field(
        ..., description="Путь к файлу с требованиями к подаче заявки на конкурс"
    )
    documents_folder_path: str = Field(
        ..., description="Путь к папке, где лежат документы организации"
    )
    results_path: str = Field(
        ..., description="Путь к папке, куда складываются результаты"
    )
    application_template_path: str = Field(
        ..., description="Путь к шаблону заявки, который нужно заполнить"
    )
    normative_base_folder: Optional[str] = Field(
        None, description="Папка с нормативной базой для проверки перечня документов"
    )
    knowledge_base_folder: Optional[str] = Field(
        None, description="Папка с эталонными заявками и базой знаний организации"
    )
    result_folder_name: Optional[str] = Field(
        None, description="Имя подпапки в results_path для собранного комплекта"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "message_id": 42,
                "user_id": 1001,
                "priority": 1,
                "file_path": "D:/tenders/44-2026/requirements.docx",
                "documents_folder_path": "D:/company/documents",
                "results_path": "D:/tenders/44-2026/result",
                "application_template_path": "D:/tenders/44-2026/form.docx",
                "normative_base_folder": "D:/company/normative",
                "knowledge_base_folder": "D:/company/reference_applications",
                "result_folder_name": "tender-44-2026",
            }
        }


class TenderAPIRequest(APIRequest):
    """Алиас для совместимости с прежним именованием."""
