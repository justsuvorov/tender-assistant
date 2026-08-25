"""Этап 2: семантический поиск файлов в архиве и сбор комплекта."""

from pathlib import Path

import pytest

from tender_assistant.core.pydantic_models import DocumentStatus, RequiredDocument
from tender_assistant.documents.application_documents import (
    ApplicationDocuments,
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


class TestApplicationDocuments:
    def _build(self, documents, model, archive_dir, results_dir):
        return ApplicationDocuments(
            documents_path=str(archive_dir),
            documents_list=documents,
            matcher=TitleMatcher(ai_model=model),
            result_folder_name="komplekt",
            results_path=str(results_dir),
            report=ApplicationDocumentsReport(output_dir=str(results_dir)),
        )

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
            documents_path=str(archive_dir),
            documents_list=[doc("Выписка из ЕГРЮЛ")],
            matcher=TitleMatcher(ai_model=scripted_model),
            result_folder_name=None,
            results_path=str(results_dir),
            report=ApplicationDocumentsReport(output_dir=str(results_dir)),
        ).result_set()

        assert Path(result.result_folder) == results_dir
        assert (results_dir / "Выписка ЕГРЮЛ 2026-07-14.pdf").exists()
