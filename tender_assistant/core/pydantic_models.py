from pydantic import BaseModel, Field
from typing import Optional




class APIRequest(BaseModel):
    """
    Схема входящего запроса для обработки файла.
    """
    request_id: int = Field(..., description="Уникальный ID запроса. ID записи в базе данных")
    user_name: Optional[str] = Field(None, description="Имя пользователя (опционально)")
    file_path: str = Field(..., description="Путь к файлу")
    priority: int = Field(0, description="Приоритет обработки")
    max_chunks: int = Field(0, description="Максимальное число чанков (0 = из настроек)")
