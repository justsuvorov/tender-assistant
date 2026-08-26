"""Этап 2: семантический поиск файлов в архиве и сбор комплекта."""

from pathlib import Path

import pytest

from tender_assistant.core.pydantic_models import DocumentStatus, RequiredDocument
from tender_assistant.documents.application_documents import (
    ApplicationDocuments,
    ComplectFolder,
    DocumentArchive,
    TitleMatcher,
)
from tender_assistant.reports.report_export import ApplicationDocumentsReport
from tests.fakes import Marker, ScriptedModel, as_json


def doc(name: str) -> RequiredDocument:
    return RequiredDocument(name=name, mandatory=True, note="")


class TestTitleMatcher:
    def test_returns_matched_file(self):
        model = ScriptedModel({Marker.FILE_MATCH: as_json(
            {"files": ["Устав.docx"], "confidence": "high", "note": ""}
        )})
        result = TitleMatcher(ai_model=model).document_name(
            "Устав организации", ["Устав.docx", "Баланс.xlsx"]
        )
        assert result["files"] == ["Устав.docx"]
        assert result["confidence"] == "high"

    def test_target_and_files_reach_the_prompt(self):
        model = ScriptedModel({Marker.FILE_MATCH: as_json({"files": []})})
        TitleMatcher(ai_model=model).document_name("Устав организации", ["Устав.docx"])

        prompt = model.last_call()
        assert "Устав организации" in prompt
        assert "- Устав.docx" in prompt

    def test_empty_archive_skips_the_model(self):
        model = ScriptedModel({})
        result = TitleMatcher(ai_model=model).document_name("Устав", [])

        assert result["files"] == []
        assert model.calls == []

    def test_hallucinated_file_names_are_dropped(self):
        """Модель любит «поправить» имя файла — доверяем только реальным."""
        model = ScriptedModel({Marker.FILE_MATCH: as_json(
            {"files": ["Устав организации.docx"], "confidence": "high", "note": ""}
        )})
        result = TitleMatcher(ai_model=model).document_name("Устав", ["Устав.docx"])

        assert result["files"] == []
        assert result["confidence"] == "low"
        assert "не подтверждены" in result["note"]

    def test_matching_is_case_insensitive(self):
        model = ScriptedModel({Marker.FILE_MATCH: as_json(
            {"files": ["устав.DOCX"], "confidence": "high", "note": ""}
        )})
        result = TitleMatcher(ai_model=model).document_name("Устав", ["Устав.docx"])
        assert result["files"] == ["Устав.docx"]


class TestDocumentArchive:
    def test_files_are_found_recursively(self, archive_dir):
        files = DocumentArchive(str(archive_dir)).files()

        assert "Выписка ЕГРЮЛ 2026-07-14.pdf" in files
        assert "Устав ООО Ромашка ред.5.docx" in files  # во вложенной папке

    def test_office_lock_files_are_skipped(self, archive_dir):
        (archive_dir / "~$устав.docx").write_text("мусор", encoding="utf-8")
        assert "~$устав.docx" not in DocumentArchive(str(archive_dir)).files()

    def test_missing_archive_yields_nothing(self, tmp_path):
        assert DocumentArchive(str(tmp_path / "нет-папки")).files() == {}

    def test_same_name_in_two_folders_keeps_both(self, tmp_path):
        """Регрессия: подменять ключ только у второго файла нельзя — для файла
        в корне архива относительный путь равен имени, ключ не менялся, и один
        файл молча затирал другой (модель не видела его среди кандидатов)."""
        nested = tmp_path / "вложенная"
        nested.mkdir()
        (tmp_path / "устав.docx").write_text("a", encoding="utf-8")
        (nested / "устав.docx").write_text("b", encoding="utf-8")

        files = DocumentArchive(str(tmp_path)).files()

        assert len(files) == 2
        assert {p.read_text(encoding="utf-8") for p in files.values()} == {"a", "b"}

    def test_unique_names_stay_keyed_by_file_name(self, tmp_path):
        """Пути в ключах — только при коллизии: имя файла читается моделью лучше."""
        nested = tmp_path / "вложенная"
        nested.mkdir()
        (nested / "устав.docx").write_text("b", encoding="utf-8")

        assert list(DocumentArchive(str(tmp_path)).files()) == ["устав.docx"]


class TestComplectFolder:
    def test_folder_is_created_under_results(self, results_dir):
        complect = ComplectFolder(results_path=str(results_dir), folder_name="komplekt")
        assert complect.path == results_dir / "komplekt"
        assert complect.path.exists()

    def test_folder_name_is_optional(self, results_dir):
        assert ComplectFolder(results_path=str(results_dir)).path == results_dir

    def test_file_is_copied(self, results_dir, tmp_path):
        source = tmp_path / "устав.docx"
        source.write_text("данные", encoding="utf-8")

        copied = ComplectFolder(results_path=str(results_dir)).copy(source)

        assert copied.exists()
        assert copied.read_text(encoding="utf-8") == "данные"

    def test_existing_file_is_not_overwritten(self, results_dir, tmp_path):
        """Два требования перечня могут указывать на один файл архива."""
        source = tmp_path / "устав.docx"
        source.write_text("данные", encoding="utf-8")
        complect = ComplectFolder(results_path=str(results_dir))

        first, second = complect.copy(source), complect.copy(source)

        assert first.name == "устав.docx"
        assert second.name == "устав_1.docx"

    def test_unreadable_source_does_not_raise(self, results_dir, tmp_path):
        """Сбой копирования одного файла не должен ронять весь комплект."""
        assert ComplectFolder(results_path=str(results_dir)).copy(
            tmp_path / "нет-такого.docx"
        ) is None


class TestApplicationDocuments:
    """Оркестратор: спросить у matcher файл под требование и отдать в комплект.

    Обход архива и копирование живут в DocumentArchive и ComplectFolder.
    """

    def _build(self, documents, model, archive_dir, results_dir):
        return ApplicationDocuments(
            archive=DocumentArchive(str(archive_dir)),
            documents_list=documents,
            matcher=TitleMatcher(ai_model=model),
            complect=ComplectFolder(
                results_path=str(results_dir), folder_name="komplekt"
            ),
            report=ApplicationDocumentsReport(output_dir=str(results_dir)),
        )

    def test_takes_at_most_five_constructor_parameters(self):
        """Оркестратор не должен обрастать параметрами: всё, что сверх
        композиции зависимостей, — признак утёкшей в него работы."""
        import inspect

        parameters = inspect.signature(ApplicationDocuments.__init__).parameters
        assert len(parameters) - 1 <= 5  # без self

    def test_statuses_reflect_confidence(
        self, scripted_model, archive_dir, results_dir
    ):
        documents = [
            doc("Выписка из ЕГРЮЛ"),                              # high  → Есть
            doc("Бухгалтерский баланс за последний отчётный год"),# medium→ Проверить
            doc("Справка об отсутствии задолженности по налогам"),# нет   → Нет
        ]
        result = self._build(
            documents, scripted_model, archive_dir, results_dir
        ).result_set()

        assert [r.status for r in result.rows] == [
            DocumentStatus.FOUND, DocumentStatus.CHECK, DocumentStatus.MISSING
        ]

    def test_files_are_copied_into_the_result_folder(
        self, scripted_model, archive_dir, results_dir
    ):
        result = self._build(
            [doc("Выписка из ЕГРЮЛ")], scripted_model, archive_dir, results_dir
        ).result_set()

        copied = results_dir / "komplekt" / "Выписка ЕГРЮЛ 2026-07-14.pdf"
        assert copied.exists()
        assert result.rows[0].files == ["Выписка ЕГРЮЛ 2026-07-14.pdf"]
        assert Path(result.result_folder) == results_dir / "komplekt"

    def test_nested_archive_files_are_found(
        self, scripted_model, archive_dir, results_dir
    ):
        """Устав лежит во вложенной папке архива."""
        self._build(
            [doc("Устав организации")], scripted_model, archive_dir, results_dir
        ).result_set()

        assert (results_dir / "komplekt" / "Устав ООО Ромашка ред.5.docx").exists()

    def test_all_archive_files_are_offered_to_the_model(
        self, scripted_model, archive_dir, results_dir
    ):
        self._build(
            [doc("Выписка из ЕГРЮЛ")], scripted_model, archive_dir, results_dir
        ).result_set()

        prompt = scripted_model.last_call(Marker.FILE_MATCH)
        assert "Устав ООО Ромашка ред.5.docx" in prompt
        assert "Договор аренды офиса.docx" in prompt

    def test_missing_archive_does_not_crash(self, scripted_model, tmp_path, results_dir):
        result = self._build(
            [doc("Устав организации")], scripted_model,
            tmp_path / "нет-такой-папки", results_dir,
        ).result_set()

        assert result.rows[0].status is DocumentStatus.MISSING

    def test_several_candidates_require_a_human(self, archive_dir, results_dir):
        """Даже при high несколько кандидатов — выбор за человеком."""
        model = ScriptedModel({Marker.FILE_MATCH: as_json({
            "files": ["Баланс_2025.xlsx", "Договор аренды офиса.docx"],
            "confidence": "high", "note": "",
        })})
        result = self._build(
            [doc("Бухгалтерский баланс")], model, archive_dir, results_dir
        ).result_set()

        assert result.rows[0].status is DocumentStatus.CHECK
        assert len(result.rows[0].files) == 2

    def test_existing_file_is_not_overwritten(self, archive_dir, results_dir):
        """Два требования могут указывать на один файл — счётчик в имени."""
        model = ScriptedModel({Marker.FILE_MATCH: as_json(
            {"files": ["Баланс_2025.xlsx"], "confidence": "high", "note": ""}
        )})
        self._build(
            [doc("Баланс"), doc("Отчётность")], model, archive_dir, results_dir
        ).result_set()

        assert (results_dir / "komplekt" / "Баланс_2025.xlsx").exists()
        assert (results_dir / "komplekt" / "Баланс_2025_1.xlsx").exists()

    def test_empty_document_list(self, scripted_model, archive_dir, results_dir):
        result = self._build([], scripted_model, archive_dir, results_dir).result_set()
        assert result.rows == []

    def test_report_contains_table_and_totals(
        self, scripted_model, archive_dir, results_dir
    ):
        documents = [doc("Выписка из ЕГРЮЛ"), doc("Справка об отсутствии задолженности по налогам")]
        result = self._build(
            documents, scripted_model, archive_dir, results_dir
        ).result_set()

        report = Path(result.report_path).read_text(encoding="utf-8")
        assert "Отчёт по подготовке документов" in report
        assert "| Есть |" in report
        assert "| Нет |" in report
        assert "- Есть: 1" in report
        assert "- Нет: 1" in report

    def test_result_folder_name_is_optional(
        self, scripted_model, archive_dir, results_dir
    ):
        result = ApplicationDocuments(
            archive=DocumentArchive(str(archive_dir)),
            documents_list=[doc("Выписка из ЕГРЮЛ")],
            matcher=TitleMatcher(ai_model=scripted_model),
            complect=ComplectFolder(results_path=str(results_dir)),
            report=ApplicationDocumentsReport(output_dir=str(results_dir)),
        ).result_set()

        assert Path(result.result_folder) == results_dir
        assert (results_dir / "Выписка ЕГРЮЛ 2026-07-14.pdf").exists()
