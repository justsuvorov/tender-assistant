"""HTTP-слой: валидация запроса, коды ошибок, форма ответа."""

import pytest
from fastapi.testclient import TestClient

from main import app
from tender_assistant.ai.model import ModelFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def payload(api_request):
    return api_request.model_dump()


@pytest.fixture
def with_fake_model(monkeypatch, scripted_model):
    monkeypatch.setattr(ModelFactory, "create", staticmethod(lambda: scripted_model))
    return scripted_model


class TestHealth:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestUpdateValidation:
    def test_empty_body_is_rejected(self, client):
        assert client.post("/api/update", json={}).status_code == 422

    def test_missing_required_field(self, client, payload):
        del payload["application_template_path"]
        response = client.post("/api/update", json=payload)

        assert response.status_code == 422
        assert "application_template_path" in response.text

    def test_optional_fields_may_be_omitted(self, client, payload, with_fake_model):
        for field in ("normative_base_folder", "knowledge_base_folder",
                      "result_folder_name", "user_id"):
            payload.pop(field, None)

        assert client.post("/api/update", json=payload).status_code == 200


class TestUpdateErrors:
    def test_missing_file_yields_400(self, client, payload, with_fake_model, tmp_path):
        payload["file_path"] = str(tmp_path / "нет-такого.docx")
        response = client.post("/api/update", json=payload)

        assert response.status_code == 400

    def test_unsupported_format_yields_400(self, client, payload, with_fake_model, tmp_path):
        bad = tmp_path / "требования.rtf"
        bad.write_text("stub", encoding="utf-8")
        payload["file_path"] = str(bad)

        response = client.post("/api/update", json=payload)

        assert response.status_code == 400
        assert "Unsupported file format" in response.json()["detail"]

    def test_model_failure_yields_502(self, client, payload, monkeypatch):
        """Сбой провайдера LLM — это ошибка шлюза, а не запроса клиента."""
        class DeadModel:
            def response(self, query):
                raise RuntimeError("Ошибка Anthropic API: сервис недоступен")

        monkeypatch.setattr(ModelFactory, "create", staticmethod(DeadModel))
        response = client.post("/api/update", json=payload)

        assert response.status_code == 502
        assert "недоступен" in response.json()["detail"]


class TestUpdateSuccess:
    @pytest.fixture
    def body(self, client, payload, with_fake_model):
        response = client.post("/api/update", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    def test_response_shape(self, body):
        assert set(body) == {
            "request_id", "document_list", "prepared_documents", "application"
        }
        assert body["request_id"] == 42

    def test_document_list_section(self, body):
        section = body["document_list"]

        assert [d["name"] for d in section["documents"]][0] == "Выписка из ЕГРЮЛ"
        assert section["source_headers"] == ["3. Состав заявки участника"]
        assert section["report_path"]

    def test_prepared_documents_section(self, body):
        rows = body["prepared_documents"]["rows"]

        assert rows[0]["status"] == "Есть"
        assert rows[0]["files"] == ["Выписка ЕГРЮЛ 2026-07-14.pdf"]
        assert body["prepared_documents"]["result_folder"].endswith("komplekt")

    def test_application_section(self, body):
        fields = {f["label"]: f for f in body["application"]["fields"]}

        assert fields["ИНН"]["value"] == "7701234567"
        assert fields["ИНН"]["status"] == "found"
        assert fields["Контактный телефон"]["status"] == "missing"
        assert body["application"]["filled_path"].endswith("form_заполнено.docx")

    def test_response_is_json_serialisable(self, body):
        """Enum-статусы должны уезжать строками, а не объектами."""
        import json

        assert isinstance(json.dumps(body), str)


class TestOpenAPI:
    def test_schema_is_generated(self, client):
        schema = client.get("/openapi.json").json()

        assert "/api/update" in schema["paths"]
        assert "/api/health" in schema["paths"]

    def test_request_example_is_documented(self, client):
        schema = client.get("/openapi.json").json()
        assert "example" in schema["components"]["schemas"]["APIRequest"]
