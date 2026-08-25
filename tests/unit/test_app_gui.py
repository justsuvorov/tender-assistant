"""Настольный интерфейс: контракт с API и чистые вспомогательные функции.

Сама отрисовка Qt не тестируется — проверяется то, что ломается молча:
несовпадение payload со схемой запроса и разбор ошибок сервиса.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="интерфейс не установлен")
pytest.importorskip("edifice", reason="интерфейс не установлен")

from app import main as gui  # noqa: E402
from tender_assistant.models.request import APIRequest  # noqa: E402


INPUTS = {
    "requirements_file": r"D:\tenders\44-2026\requirements.docx",
    "archive_folder": r"D:\company\documents",
    "template_file": r"D:\tenders\44-2026\form.docx",
    "results_folder": r"D:\tenders\44-2026\result",
    "normative_folder": r"D:\company\normative",
    "knowledge_folder": r"D:\company\reference",
}


class TestPayloadContract:
    """Рассинхрон GUI и схемы запроса иначе всплывёт только в рантайме."""

    def test_payload_validates_against_request_schema(self):
        request = APIRequest(**gui.build_payload(**INPUTS))

        assert request.file_path == INPUTS["requirements_file"]
        assert request.documents_folder_path == INPUTS["archive_folder"]
        assert request.application_template_path == INPUTS["template_file"]
        assert request.results_path == INPUTS["results_folder"]
        assert request.normative_base_folder == INPUTS["normative_folder"]
        assert request.knowledge_base_folder == INPUTS["knowledge_folder"]

    def test_payload_has_no_unknown_keys(self):
        assert set(gui.build_payload(**INPUTS)) <= set(APIRequest.model_fields)

    def test_all_required_schema_fields_are_sent(self):
        required = {
            name for name, field in APIRequest.model_fields.items()
            if field.is_required()
        }
        assert required <= set(gui.build_payload(**INPUTS))

    def test_optional_folders_become_null(self):
        payload = gui.build_payload(
            **{**INPUTS, "normative_folder": "", "knowledge_folder": ""}
        )
        assert payload["normative_base_folder"] is None
        assert payload["knowledge_base_folder"] is None
        assert APIRequest(**payload).normative_base_folder is None

    def test_result_folder_is_named_after_the_tender(self):
        assert gui.build_payload(**INPUTS)["result_folder_name"] == "requirements"

    def test_message_id_is_generated_when_not_given(self):
        assert gui.build_payload(**INPUTS)["message_id"] > 0

    def test_message_id_can_be_fixed(self):
        assert gui.build_payload(**INPUTS, message_id=7)["message_id"] == 7


class TestMissingInputs:
    def test_nothing_missing_when_all_filled(self):
        assert gui.missing_inputs(**INPUTS) == []

    def test_optional_folders_are_not_required(self):
        inputs = {**INPUTS, "normative_folder": "", "knowledge_folder": ""}
        assert gui.missing_inputs(**inputs) == []

    @pytest.mark.parametrize("field, name", [
        ("requirements_file", "файл требований тендера"),
        ("archive_folder", "папку с документами"),
        ("template_file", "шаблон заявки"),
        ("results_folder", "папку для результатов"),
    ])
    def test_each_required_field_is_reported(self, field, name):
        assert gui.missing_inputs(**{**INPUTS, field: ""}) == [name]

    def test_empty_form_reports_everything(self):
        assert len(gui.missing_inputs()) == 4


class TestErrorDetail:
    class Response:
        def __init__(self, payload=None, text=""):
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("не json")
            return self._payload

    def test_plain_detail(self):
        response = self.Response({"detail": "Файл не найден: form.docx"})
        assert gui._error_detail(response) == "Файл не найден: form.docx"

    def test_validation_error_is_flattened(self):
        """FastAPI отдаёт 422 списком — пользователю нужна одна строка."""
        response = self.Response({"detail": [
            {"loc": ["body", "application_template_path"],
             "msg": "Field required", "type": "missing"}
        ]})
        assert gui._error_detail(response) == (
            "application_template_path: Field required"
        )

    def test_non_json_response_falls_back_to_text(self):
        response = self.Response(None, text="502 Bad Gateway")
        assert gui._error_detail(response) == "502 Bad Gateway"


class TestFormatting:
    @pytest.mark.parametrize("seconds, expected", [
        (0, "0:00"), (5, "0:05"), (65, "1:05"), (600, "10:00"),
    ])
    def test_short_duration(self, seconds, expected):
        assert gui._fmt(seconds) == expected

    @pytest.mark.parametrize("seconds, expected", [
        (90, "~1 мин"), (600, "~10 мин"), (3700, "~1 ч 1 мин"),
    ])
    def test_long_duration(self, seconds, expected):
        assert gui._fmt_long(seconds) == expected

    def test_path_is_shortened_to_file_name(self):
        assert gui._short(r"D:\a\b\form.docx", "Не выбран") == "form.docx"

    def test_placeholder_for_empty_path(self):
        assert gui._short("", "Не выбран") == "Не выбран"


class TestConfig:
    def test_api_url_is_built_from_base(self):
        assert gui.API_URL.endswith("/api/update")
        assert gui.API_HEALTH_URL.endswith("/api/health")

    def test_defaults_are_present(self):
        assert gui._DEFAULT_CONFIG["api_base_url"].startswith("http")
        assert gui._DEFAULT_CONFIG["request_timeout"] > 0

    def test_broken_config_falls_back_to_defaults(self, tmp_path, monkeypatch):
        broken = tmp_path / "config.json"
        broken.write_text("{это не json", encoding="utf-8")
        monkeypatch.setattr(gui, "_CONFIG_PATH", broken)

        assert gui._load_config() == gui._DEFAULT_CONFIG

    def test_missing_config_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gui, "_CONFIG_PATH", tmp_path / "нет.json")
        assert gui._load_config() == gui._DEFAULT_CONFIG

    def test_config_overrides_defaults(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text('{"api_base_url": "http://10.0.0.1:9000"}', encoding="utf-8")
        monkeypatch.setattr(gui, "_CONFIG_PATH", config)

        loaded = gui._load_config()
        assert loaded["api_base_url"] == "http://10.0.0.1:9000"
        assert loaded["request_timeout"] == gui._DEFAULT_CONFIG["request_timeout"]


class TestResponseParsing:
    """Сводка читает ответ API — форма ответа не должна разъезжаться."""

    def test_stage_summaries_match_api_response(self, api_request, scripted_model):
        from fastapi.encoders import jsonable_encoder

        from tender_assistant.services.assistant import TenderAssistantService

        result = jsonable_encoder(
            TenderAssistantService(
                request=api_request, ai_model=scripted_model
            ).result()
        )

        document_list = result["document_list"]
        prepared = result["prepared_documents"]
        application = result["application"]

        assert len(document_list["documents"]) == 4
        assert document_list["report_path"]

        statuses = [row["status"] for row in prepared["rows"]]
        assert statuses.count("Есть") == 2
        assert statuses.count("Проверить") == 1
        assert statuses.count("Нет") == 1
        assert prepared["result_folder"]

        field_statuses = [f["status"] for f in application["fields"]]
        assert field_statuses.count("found") == 3
        assert field_statuses.count("check") == 1
        assert field_statuses.count("missing") == 1
        assert application["filled_path"]
