"""Этап 3: разбор шаблона заявки и заполнение полей."""

from pathlib import Path

import pytest

from tender_assistant.application.application import (
    AITenderForm,
    TenderAIQuery,
    TenderApplication,
)
from tender_assistant.core.pydantic_models import FieldStatus
from tender_assistant.reports.report_export import TenderApplicationReport
from tests.fakes import BrokenModel, Marker, ScriptedModel, as_json


class TestTenderAIQuery:
    def test_returns_value_and_status(self):
        model = ScriptedModel({Marker.FIELD_VALUE: as_json(
            {"value": "7701234567", "status": "found", "source": "kb", "note": ""}
        )})
        result = TenderAIQuery(ai_model=model).result("ИНН", "контекст")

        assert result["value"] == "7701234567"
        assert result["status"] == "found"

    def test_field_and_context_reach_the_prompt(self):
        model = ScriptedModel({Marker.FIELD_VALUE: as_json({"value": "", "status": "missing"})})
        TenderAIQuery(ai_model=model).result("ИНН", "ИНН: 7701234567")

        prompt = model.last_call()
        assert "ИНН: 7701234567" in prompt
        assert "### ЗАПРАШИВАЕМОЕ ПОЛЕ" in prompt

    def test_garbage_answer_requires_a_check(self):
        result = TenderAIQuery(ai_model=BrokenModel("7701234567")).result("ИНН", "к")
        assert result["status"] == "check"


class TestAITenderForm:
    def test_extracts_fields_and_fills_them(self, scripted_model):
        form = AITenderForm(tender_query=TenderAIQuery(ai_model=scripted_model))
        fields = form.prepare("| ИНН | |", "контекст")

        assert [f.label for f in fields] == [
            "Полное наименование участника", "ИНН", "Юридический адрес",
            "Контактный телефон", "Руководитель организации",
        ]
        assert fields[1].value == "7701234567"

    def test_statuses_are_mapped(self, scripted_model):
        form = AITenderForm(tender_query=TenderAIQuery(ai_model=scripted_model))
        statuses = {f.label: f.status for f in form.prepare("шаблон", "контекст")}

        assert statuses["ИНН"] is FieldStatus.FOUND
        assert statuses["Юридический адрес"] is FieldStatus.CHECK
        assert statuses["Контактный телефон"] is FieldStatus.MISSING

    def test_one_model_call_per_field(self, scripted_model):
        form = AITenderForm(tender_query=TenderAIQuery(ai_model=scripted_model))
        form.prepare("шаблон", "контекст")

        assert scripted_model.call_count(Marker.FORM_FIELDS) == 1
        assert scripted_model.call_count(Marker.FIELD_VALUE) == 5

    def test_template_reaches_the_extraction_prompt(self, scripted_model):
        form = AITenderForm(tender_query=TenderAIQuery(ai_model=scripted_model))
        form.prepare("| Уникальная строка шаблона | |", "контекст")

        assert "Уникальная строка шаблона" in scripted_model.last_call(Marker.FORM_FIELDS)

    def test_template_without_fields(self):
        model = ScriptedModel({Marker.FORM_FIELDS: as_json({"fields": []})})
        form = AITenderForm(tender_query=TenderAIQuery(ai_model=model))

        assert form.prepare("шаблон", "контекст") == []
        assert model.call_count(Marker.FIELD_VALUE) == 0

    def test_model_is_required_for_extraction(self):
        class QueryWithoutModel:
            def result(self, field_label, context):
                return {"value": "", "status": "missing", "source": "", "note": ""}

        form = AITenderForm(tender_query=QueryWithoutModel())
        with pytest.raises(RuntimeError, match="модель"):
            form.prepare("шаблон", "контекст")


class TestTenderApplication:
    def _build(self, template, requirements, knowledge, results_dir, model):
        return TenderApplication(
            application_template_path=str(template),
            tender_info_path=str(requirements),
            normative_base_folder=str(knowledge),
            tender_form=AITenderForm(tender_query=TenderAIQuery(ai_model=model)),
            report=TenderApplicationReport(output_dir=str(results_dir)),
            results_path=str(results_dir),
        )

    def test_fills_template_and_saves_it(
        self, template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
    ):
        from docx import Document

        result = self._build(
            template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
        ).result()

        filled = Path(result.filled_path)
        assert filled.exists()
        assert filled.name == "form_заполнено.docx"

        cells = {r.cells[0].text: r.cells[1].text
                 for r in Document(str(filled)).tables[0].rows}
        assert cells["ИНН"] == "7701234567"
        assert cells["Контактный телефон"] == "НЕ НАЙДЕНО"

    def test_context_holds_knowledge_base_and_tender_info(
        self, template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
    ):
        """Часть данных заявки лежит в файле требований — он тоже в контексте."""
        self._build(
            template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
        ).result()

        prompt = scripted_model.last_call(Marker.FIELD_VALUE)
        assert "БАЗА ЗНАНИЙ И ЭТАЛОННЫЕ ЗАЯВКИ" in prompt
        assert "7701234567" in prompt
        assert "ТРЕБОВАНИЯ ТЕНДЕРА" in prompt
        assert "Состав заявки участника" in prompt

    def test_missing_tender_info_is_survivable(
        self, template_docx, knowledge_dir, results_dir, scripted_model, tmp_path
    ):
        application = self._build(
            template_docx, tmp_path / "нет.docx", knowledge_dir,
            results_dir, scripted_model,
        )
        result = application.result()

        assert Path(result.filled_path).exists()
        assert "ТРЕБОВАНИЯ ТЕНДЕРА" not in scripted_model.last_call(Marker.FIELD_VALUE)

    def test_missing_knowledge_base_is_survivable(
        self, template_docx, requirements_docx, results_dir, scripted_model, tmp_path
    ):
        result = self._build(
            template_docx, requirements_docx, tmp_path / "нет-базы",
            results_dir, scripted_model,
        ).result()

        assert Path(result.filled_path).exists()

    def test_report_lists_fields_and_totals(
        self, template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
    ):
        result = self._build(
            template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
        ).result()

        report = Path(result.report_path).read_text(encoding="utf-8")
        assert "Отчёт по заполнению заявки" in report
        assert "Есть (зелёный)" in report
        assert "Проверить (жёлтый)" in report
        assert "Нет (красный)" in report
        assert "- Заполнено: 3" in report
        assert "- Требует проверки: 1" in report
        assert "- Не найдено: 1" in report

    def test_template_is_not_modified_in_place(
        self, template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
    ):
        before = template_docx.read_bytes()
        self._build(
            template_docx, requirements_docx, knowledge_dir, results_dir, scripted_model
        ).result()
        assert template_docx.read_bytes() == before
