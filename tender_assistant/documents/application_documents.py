import shutil
from collections import Counter
from pathlib import Path
from typing import List, Optional, Sequence

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import PostProcessor, TitleMatcherPostProcessor
from tender_assistant.ai.promt_builders import PromptEngine
from tender_assistant.core.config import settings
from tender_assistant.core.pydantic_models import (
    ApplicationDocumentsResult,
    DocumentStatus,
    PreparedDocument,
    RequiredDocument,
)
from tender_assistant.reports.report_export import ApplicationDocumentsReport, BaseReport


class TitleMatcher:
    """Подбирает файл под требуемый документ по смыслу его названия."""

    def __init__(
        self,
        ai_model: AIModel,
        title_matcher_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.title_matcher_post_processor = (
            title_matcher_post_processor or TitleMatcherPostProcessor()
        )
        self.ai_model = ai_model
        self.prompt_engine = prompt_engine or PromptEngine()
        self.prompt = settings.document_name_matcher

    def document_name(self, target_name: str, name_list: Sequence[str]) -> dict:
        """Возвращает {'files': [...], 'confidence': ..., 'note': ...}."""
        if not name_list:
            return {"files": [], "confidence": "low", "note": "Папка с документами пуста"}

        query_for_model = self._prepare_query(target_name, name_list)
        response = self.ai_model.response(query_for_model)
        matched = self.title_matcher_post_processor.report(response)

        # Модель иногда возвращает переформулированное имя — оставляем только
        # то, что действительно есть в папке.
        available = {name.lower(): name for name in name_list}
        matched["files"] = [
            available[f.lower()] for f in matched["files"] if f.lower() in available
        ]
        if not matched["files"] and matched["confidence"] != "low":
            matched["confidence"] = "low"
            matched["note"] = (matched["note"] + " Имена файлов не подтверждены.").strip()

        return matched

    def _prepare_query(self, target_name: str, name_list: Sequence[str]) -> str:
        listing = "\n".join(f"- {name}" for name in name_list)
        return self.prompt_engine.render(
            self.prompt,
            fit_key="name_list",
            fit_query=target_name,
            target_name=target_name,
            name_list=listing,
        )


class DocumentArchive:
    """Архив документов организации: что в нём лежит."""

    _SKIP_PREFIXES = ("~$", ".")

    def __init__(self, documents_path: str):
        self.documents_path = documents_path

    def files(self) -> dict:
        """{имя файла: путь} по всем файлам архива, включая вложенные папки."""
        root = Path(self.documents_path)
        if not root.exists():
            print(
                f"[WARN] Папка с документами не найдена: {self.documents_path}",
                flush=True,
            )
            return {}

        files = [
            file for file in sorted(root.rglob("*"))
            if file.is_file() and not file.name.startswith(self._SKIP_PREFIXES)
        ]

        # Одинаковые имена в разных папках различаем относительным путём —
        # причём ОБА файла, а не только второй найденный. Подменять ключ
        # только у второго нельзя: для файла в корне архива относительный
        # путь равен имени, ключ не меняется, и один из файлов молча
        # затирает другой — модель не увидит его среди кандидатов.
        name_counts = Counter(file.name for file in files)
        documents = {
            (file.name if name_counts[file.name] == 1 else str(file.relative_to(root))): file
            for file in files
        }

        print(f"[INFO] В архиве найдено файлов: {len(documents)}", flush=True)
        return documents


class ComplectFolder:
    """Папка, куда собирается комплект документов заявки."""

    def __init__(self, results_path: Optional[str] = None, folder_name: str = None):
        self.results_path = results_path or settings.results_root
        self.folder_name = folder_name

    @property
    def path(self) -> Path:
        folder = Path(self.results_path)
        if self.folder_name:
            folder = folder / self.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def copy(self, source: Path) -> Optional[Path]:
        """Копирует файл в папку комплекта. None — если скопировать не удалось."""
        try:
            destination = self._unique_path(self.path / source.name)
            shutil.copy2(source, destination)
            return destination
        except OSError as exc:
            print(f"[WARN] Не скопирован {source}: {exc}", flush=True)
            return None

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Не затираем уже скопированный файл — добавляем счётчик к имени.

        Два требования перечня могут указывать на один и тот же файл архива.
        """
        if not path.exists():
            return path
        for i in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path


class ApplicationDocuments:
    """Этап 2: собрать файлы документов из архива в папку заявки.

    Оркестратор: для каждого требования перечня спросить у matcher подходящий
    файл и передать его в комплект. Обход архива и копирование живут в
    DocumentArchive и ComplectFolder.
    """

    def __init__(
        self,
        archive: DocumentArchive,
        documents_list: List[RequiredDocument],
        matcher: TitleMatcher,
        complect: ComplectFolder = None,
        report: BaseReport = None,
    ):
        self.archive = archive
        self.documents_list = documents_list
        self.matcher = matcher
        self.complect = complect or ComplectFolder()
        self.report = report or ApplicationDocumentsReport()

    def result_set(self) -> ApplicationDocumentsResult:
        """Ищет в архиве файлы по перечню документов и копирует их в папку заявки."""
        available_documents = self.archive.files()

        rows = [
            self._prepare_document(document, available_documents)
            for document in self.documents_list
        ]

        result = ApplicationDocumentsResult(
            rows=rows, result_folder=str(self.complect.path)
        )
        result.report_path = self.report.result(self._report_text(result))

        found = sum(1 for r in rows if r.status is DocumentStatus.FOUND)
        print(
            f"[INFO] Подготовлено {found}/{len(rows)} документов "
            f"в {result.result_folder}",
            flush=True,
        )
        return result

    def _prepare_document(
        self, document: RequiredDocument, available_documents: dict
    ) -> PreparedDocument:
        matched = self.matcher.document_name(document.name, list(available_documents))

        if not matched["files"]:
            return PreparedDocument(
                name=document.name,
                status=DocumentStatus.MISSING,
                note=matched["note"] or "Файл не найден в архиве",
            )

        copied = []
        for file_name in matched["files"]:
            destination = self.complect.copy(available_documents[file_name])
            if destination:
                copied.append(destination.name)

        if not copied:
            return PreparedDocument(
                name=document.name,
                status=DocumentStatus.MISSING,
                note="Файл найден, но не скопирован — см. лог",
            )

        # Уверенное совпадение — «Есть», неуверенное или несколько кандидатов —
        # «Проверить»: решение остаётся за человеком.
        confident = matched["confidence"] == "high" and len(copied) == 1
        return PreparedDocument(
            name=document.name,
            status=DocumentStatus.FOUND if confident else DocumentStatus.CHECK,
            files=copied,
            note=matched["note"],
        )

    @staticmethod
    def _report_text(result: ApplicationDocumentsResult) -> str:
        """Отчёт-таблица: документ — статус Есть/Нет/Проверить — файлы."""
        lines = [
            "# Отчёт по подготовке документов",
            "",
            f"Папка комплекта: {result.result_folder}",
            "",
            "| № | Документ | Статус | Файлы | Примечание |",
            "| --- | --- | --- | --- | --- |",
        ]
        for i, row in enumerate(result.rows, start=1):
            files = ", ".join(row.files)
            lines.append(
                f"| {i} | {row.name} | {row.status.value} | {files} | {row.note} |"
            )

        counters = {status: 0 for status in DocumentStatus}
        for row in result.rows:
            counters[row.status] += 1

        lines += [
            "",
            "## Итого",
            "",
            f"- Есть: {counters[DocumentStatus.FOUND]}",
            f"- Проверить: {counters[DocumentStatus.CHECK]}",
            f"- Нет: {counters[DocumentStatus.MISSING]}",
        ]
        return "\n".join(lines)
