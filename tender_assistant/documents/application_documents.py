import shutil
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


class ApplicationDocuments:
    """Этап 2: собрать файлы документов из архива в папку заявки."""

    _SKIP_PREFIXES = ("~$", ".")

    def __init__(
        self,
        documents_path: str,
        documents_list: List[RequiredDocument],
        matcher: TitleMatcher,
        result_folder_name: str,
        results_path: Optional[str] = None,
        report: BaseReport = None,
    ):
        self.report = report or ApplicationDocumentsReport()
        self.result_folder_name = result_folder_name
        self.matcher = matcher
        self.documents_list = documents_list
        self.documents_path = documents_path
        self.results_path = results_path or settings.results_root

    def result_set(self) -> ApplicationDocumentsResult:
        """Ищет в архиве файлы по перечню документов и копирует их в папку заявки."""
        available_documents = self._documents_in_path(self.documents_path)
        target_folder = self._target_folder()

        rows: List[PreparedDocument] = []
        for document in self.documents_list:
            rows.append(
                self._prepare_document(document, available_documents, target_folder)
            )

        result = ApplicationDocumentsResult(
            rows=rows, result_folder=str(target_folder)
        )
        result.report_path = self.report.result(self._report_text(result))

        found = sum(1 for r in rows if r.status is DocumentStatus.FOUND)
        print(
            f"[INFO] Подготовлено {found}/{len(rows)} документов в {target_folder}",
            flush=True,
        )
        return result

    # ── шаги ──────────────────────────────────────────────────────────────────

    def _prepare_document(
        self,
        document: RequiredDocument,
        available_documents: dict,
        target_folder: Path,
    ) -> PreparedDocument:
        matched = self._search_in_files(document.name, list(available_documents))

        if not matched["files"]:
            return PreparedDocument(
                name=document.name,
                status=DocumentStatus.MISSING,
                note=matched["note"] or "Файл не найден в архиве",
            )

        copied = []
        for file_name in matched["files"]:
            destination = self._copy_document(
                available_documents[file_name], target_folder
            )
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

    def _documents_in_path(self, documents_path: str) -> dict:
        """{имя файла: путь} по всем файлам архива, включая вложенные папки."""
        root = Path(documents_path)
        if not root.exists():
            print(f"[WARN] Папка с документами не найдена: {documents_path}", flush=True)
            return {}

        documents = {}
        for file in sorted(root.rglob("*")):
            if not file.is_file() or file.name.startswith(self._SKIP_PREFIXES):
                continue
            # Одинаковые имена в разных подпапках: первый найденный выигрывает,
            # остальные различаем относительным путём.
            key = file.name
            if key in documents:
                key = str(file.relative_to(root))
            documents[key] = file

        print(f"[INFO] В архиве найдено файлов: {len(documents)}", flush=True)
        return documents

    def _search_in_files(self, document_name: str, documents: List[str]) -> dict:
        """Подбор файла под документ через LLM."""
        return self.matcher.document_name(document_name, documents)

    def _copy_document(self, source: Path, target_folder: Path) -> Optional[Path]:
        try:
            target_folder.mkdir(parents=True, exist_ok=True)
            destination = self._unique_path(target_folder / source.name)
            shutil.copy2(source, destination)
            return destination
        except OSError as exc:
            print(f"[WARN] Не скопирован {source}: {exc}", flush=True)
            return None

    def _target_folder(self) -> Path:
        folder = Path(self.results_path)
        if self.result_folder_name:
            folder = folder / self.result_folder_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Не затираем уже скопированный файл — добавляем счётчик к имени."""
        if not path.exists():
            return path
        for i in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path

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
