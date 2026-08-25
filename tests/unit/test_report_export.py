"""Сохранение markdown-отчётов."""

from pathlib import Path

import pytest

from tender_assistant.reports.report_export import (
    ApplicationDocumentsReport,
    BaseReport,
    DocumentListReport,
    ReportExport,
    TenderApplicationReport,
)
from tender_assistant.reports.report_models import ReportRow, TenderReport
from tender_assistant.reports.style import GREEN, RED, YELLOW, document_fill, field_fill


class TestBaseReport:
    def test_writes_file_and_returns_path(self, tmp_path):
        path = BaseReport(output_dir=str(tmp_path)).result("# Отчёт")

        assert Path(path).read_text(encoding="utf-8") == "# Отчёт"
        assert Path(path).parent == tmp_path

    def test_file_name_carries_a_timestamp(self, tmp_path):
        path = Path(BaseReport(output_dir=str(tmp_path)).result("текст"))
        assert path.stem.startswith("report_")
        assert path.suffix == ".md"

    def test_custom_file_name(self, tmp_path):
        path = Path(
            BaseReport(output_dir=str(tmp_path), file_name="свой.md").result("текст")
        )
        assert path.stem.startswith("свой_")

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_report_is_not_written(self, tmp_path, text):
        assert BaseReport(output_dir=str(tmp_path)).result(text) is None
        assert list(tmp_path.iterdir()) == []

    def test_output_directory_is_created(self, tmp_path):
        target = tmp_path / "нет" / "такой"
        assert Path(BaseReport(output_dir=str(target)).result("текст")).exists()

    def test_output_dir_can_be_set_later(self, tmp_path):
        """Оркестратор узнаёт путь результатов только из запроса."""
        report = BaseReport()
        report.output_dir = str(tmp_path)
        assert Path(report.result("текст")).parent == tmp_path

    def test_setting_empty_output_dir_is_ignored(self, tmp_path):
        report = BaseReport(output_dir=str(tmp_path))
        report.output_dir = ""
        assert report.output_dir == str(tmp_path)

    def test_defaults_to_settings_root(self):
        from tender_assistant.core.config import settings

        assert BaseReport().output_dir == settings.results_root

    def test_unwritable_target_does_not_raise(self, tmp_path):
        """Сбой записи отчёта не должен ронять пайплайн."""
        blocker = tmp_path / "занято"
        blocker.write_text("файл вместо папки", encoding="utf-8")

        assert BaseReport(output_dir=str(blocker / "внутри")).result("текст") is None

    @pytest.mark.parametrize("cls, stem", [
        (DocumentListReport, "document_list"),
        (ApplicationDocumentsReport, "documents_status"),
        (TenderApplicationReport, "application_fill"),
    ])
    def test_stage_reports_have_distinct_names(self, tmp_path, cls, stem):
        path = Path(cls(output_dir=str(tmp_path)).result("текст"))
        assert path.stem.startswith(stem + "_")


class TestReportExport:
    def test_returns_payload_and_writes_file(self, tmp_path):
        result = ReportExport(output_dir=str(tmp_path), message_id=42).result("# Отчёт")

        assert result["message_id"] == 42
        assert result["status"] == "success"
        assert result["payload"]["format"] == "markdown"
        assert Path(result["payload"]["path"]).exists()

    def test_without_storage_db_step_is_skipped(self, tmp_path):
        result = ReportExport(output_dir=str(tmp_path)).result("текст")
        assert result["db_status"] == "skipped"

    def test_storage_is_called(self, tmp_path):
        class Storage:
            def __init__(self):
                self.saved = None

            def update(self, message_id, report_text):
                self.saved = (message_id, report_text)

        storage = Storage()
        result = ReportExport(
            output_dir=str(tmp_path), storage=storage, message_id=7
        ).result("текст")

        assert storage.saved == (7, "текст")
        assert result["db_status"] == "saved"

    def test_storage_failure_does_not_lose_the_report(self, tmp_path):
        class BrokenStorage:
            def update(self, message_id, report_text):
                raise RuntimeError("база недоступна")

        result = ReportExport(
            output_dir=str(tmp_path), storage=BrokenStorage()
        ).result("текст")

        assert result["db_status"].startswith("error:")
        assert result["payload"]["text"] == "текст"


class TestStyle:
    @pytest.mark.parametrize("status, colour", [
        ("found", GREEN), ("check", YELLOW), ("missing", RED),
    ])
    def test_field_colours(self, status, colour):
        assert field_fill(status) == colour

    @pytest.mark.parametrize("status, colour", [
        ("Есть", GREEN), ("Проверить", YELLOW), ("Нет", RED),
    ])
    def test_document_colours(self, status, colour):
        assert document_fill(status) == colour

    def test_unknown_status_is_white(self):
        assert field_fill("что-то ещё") == "FFFFFF"


class TestReportModels:
    def test_merge_concatenates_rows_and_summaries(self):
        first = TenderReport(
            title="Отчёт",
            rows=[ReportRow(subject="Устав", value="устав.docx", status="Есть")],
            summary="первый",
        )
        second = TenderReport(
            rows=[ReportRow(subject="Баланс", value="", status="Нет")],
            summary="второй",
        )
        merged = TenderReport.merge([first, second])

        assert [r.subject for r in merged.rows] == ["Устав", "Баланс"]
        assert merged.summary == "первый\n\nвторой"
        assert merged.title == "Отчёт"

    def test_merge_of_nothing(self):
        assert TenderReport.merge([]).rows == []
