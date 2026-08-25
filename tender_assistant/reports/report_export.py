from datetime import datetime
from pathlib import Path
from typing import Optional

from tender_assistant.core.config import settings


class BaseReport:
    """Формирует и сохраняет отчёт в markdown, возвращает путь к файлу.

    Если каталог не задан при создании, он берётся из настроек. Каталог можно
    переопределить позже — оркестратор проставляет его, когда узнаёт путь
    результатов из запроса.
    """

    file_name = "report.md"
    title = "Отчёт"

    def __init__(self, output_dir: Optional[str] = None, file_name: Optional[str] = None):
        self._output_dir = output_dir
        if file_name:
            self.file_name = file_name

    @property
    def output_dir(self) -> str:
        return self._output_dir or settings.results_root

    @output_dir.setter
    def output_dir(self, value: Optional[str]) -> None:
        if value:
            self._output_dir = value

    def result(self, report_text: str) -> Optional[str]:
        if not report_text:
            return None

        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report_text, encoding="utf-8")
        except OSError as exc:
            print(f"[WARN] Отчёт '{self.title}' не сохранён: {exc}", flush=True)
            return None

        print(f"[INFO] Отчёт сохранён: {path}", flush=True)
        return str(path)

    def _path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = Path(self.file_name).stem
        suffix = Path(self.file_name).suffix or ".md"
        return Path(self.output_dir) / f"{stem}_{stamp}{suffix}"


class DocumentListReport(BaseReport):
    """Отчёт первого этапа — перечень необходимых документов."""

    file_name = "document_list.md"
    title = "Перечень документов"


class ApplicationDocumentsReport(BaseReport):
    """Отчёт второго этапа — статусы Есть / Нет / Проверить."""

    file_name = "documents_status.md"
    title = "Подготовка документов"


class TenderApplicationReport(BaseReport):
    """Отчёт третьего этапа — заполнение шаблона заявки."""

    file_name = "application_fill.md"
    title = "Заполнение заявки"


class ReportExport(BaseReport):
    """Сохраняет отчёт и дополнительно отдаёт структуру для API/UI.

    Запись в БД опциональна: если хранилище не передано, отчёт просто
    сохраняется в файл, а ответ формируется без обращения к базе.
    """

    file_name = "tender_report.md"
    title = "Отчёт ассистента"

    def __init__(
        self,
        output_dir: Optional[str] = None,
        file_name: Optional[str] = None,
        storage=None,
        message_id: Optional[int] = None,
    ):
        super().__init__(output_dir=output_dir, file_name=file_name)
        self._storage = storage
        self._message_id = message_id

    def result(self, report_text: str) -> dict:
        path = super().result(report_text)
        db_status = self._save_to_storage(report_text)

        return {
            "message_id": self._message_id,
            "status": "success",
            "db_status": db_status,
            "payload": {
                "text": report_text,
                "format": "markdown",
                "path": path,
            },
        }

    def _save_to_storage(self, report_text: str) -> str:
        if self._storage is None:
            return "skipped"
        try:
            self._storage.update(message_id=self._message_id, report_text=report_text)
            return "saved"
        except Exception as exc:
            # Ошибка хранилища не должна ронять запрос: текст отчёта
            # пользователь получит в любом случае.
            return f"error: {exc}"
