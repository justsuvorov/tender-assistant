from pathlib import Path
from typing import List, Optional

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import (
    FormFieldsResponse,
    PostProcessor,
    TenderRowPostProcessor,
)
from tender_assistant.ai.promt_builders import NormativeBaseLoader, PromptEngine
from tender_assistant.core.config import settings
from tender_assistant.core.parsers import DataParser
from tender_assistant.core.pydantic_models import (
    FieldStatus,
    FilledField,
    FormField,
    TenderApplicationResult,
)
from tender_assistant.reports.report_export import BaseReport, TenderApplicationReport
from tender_assistant.reports.writers import ReportWriter, TenderReportWriter


class TenderQuery:
    """Интерфейс поиска значения одного поля заявки."""

    def result(self, field_label: str, context: str) -> dict:
        raise NotImplementedError


class TenderAIQuery(TenderQuery):
    """Ищет значение поля заявки в базе знаний и требованиях тендера через LLM."""

    def __init__(
        self,
        ai_model: AIModel,
        tender_row_postprocessor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.prompt_engine = prompt_engine or PromptEngine()
        self.tender_row_postprocessor = tender_row_postprocessor or TenderRowPostProcessor()
        self.ai_model = ai_model
        self.prompt = settings.tender_form_fill_template

    def result(self, field_label: str, context: str) -> dict:
        """Возвращает {'value', 'status', 'source', 'note'} для поля заявки."""
        query = self._prepare_query(field_label, context)
        response = self.ai_model.response(query)
        return self.tender_row_postprocessor.report(response)

    def _prepare_query(self, field_label: str, context: str) -> str:
        return self.prompt_engine.render(
            self.prompt,
            fit_key="context",
            fit_query=field_label,
            field_label=field_label,
            context=context,
        )


class TenderForm:
    """Интерфейс построчного заполнения формы заявки."""

    def prepare(self, md_template_form: str, context: str) -> List[FilledField]:
        raise NotImplementedError


class AITenderForm(TenderForm):
    """Разбирает шаблон на поля и заполняет их по одному."""

    def __init__(
        self,
        tender_query: TenderQuery,
        fields_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.tender_query = tender_query
        self.fields_post_processor = fields_post_processor or FormFieldsResponse()
        self.prompt_engine = prompt_engine or PromptEngine()
        self.fields_prompt = settings.tender_form_fields_template
        # Модель, извлекающая поля, берётся у запроса — отдельный клиент не нужен.
        self.ai_model = getattr(tender_query, "ai_model", None)

    def prepare(self, md_template_form: str, context: str) -> List[FilledField]:
        """Заполняет шаблон значениями из базы знаний и требований тендера."""
        fields = self._extract_queries_from_form(md_template_form)
        print(f"[INFO] В шаблоне заявки найдено полей: {len(fields)}", flush=True)

        filled: List[FilledField] = []
        for i, field in enumerate(fields, start=1):
            answer = self.tender_query.result(field.label, context)
            filled.append(
                FilledField(
                    label=field.label,
                    anchor=field.anchor,
                    kind=field.kind,
                    value=answer["value"],
                    status=FieldStatus(answer["status"]),
                    source=answer["source"],
                    note=answer["note"],
                )
            )
            print(
                f"[INFO] [{i}/{len(fields)}] {field.label} → {answer['status']}",
                flush=True,
            )

        return filled

    def _extract_queries_from_form(self, md_template_form: str) -> List[FormField]:
        if self.ai_model is None:
            raise RuntimeError("Не задана модель для разбора шаблона заявки")

        query = self.prompt_engine.render(
            self.fields_prompt,
            fit_key="template_text",
            fit_query="поля для заполнения",
            template_text=md_template_form,
        )
        response = self.ai_model.response(query)
        items = self.fields_post_processor.report(response)
        return [FormField(**item) for item in items]


class TenderApplication:
    """Этап 3: заполнить шаблон заявки данными из базы знаний."""

    def __init__(
        self,
        application_template_path: str,
        tender_info_path: str,
        normative_base_folder: Optional[str],
        tender_form: TenderForm,
        report_writer: ReportWriter = None,
        report: BaseReport = None,
        results_path: Optional[str] = None,
        base_loader: NormativeBaseLoader = None,
    ):
        self.report_writer = report_writer or TenderReportWriter()
        self.report = report or TenderApplicationReport()
        self.tender_form = tender_form
        self.normative_base_folder = normative_base_folder
        self.tender_info_path = tender_info_path
        self.application_template_path = application_template_path
        self.results_path = results_path or settings.results_root
        self.base_loader = base_loader or NormativeBaseLoader()

    def result(self) -> TenderApplicationResult:
        """Читает шаблон и базу знаний, заполняет поля и сохраняет заявку."""
        md_template_form = DataParser(self.application_template_path).origin_data()
        md_tender_info = self._read_tender_info()
        md_knowledge_base = self._read_base(self.normative_base_folder)

        context = self._build_context(md_knowledge_base, md_tender_info)
        fields = self.tender_form.prepare(md_template_form, context)

        result = TenderApplicationResult(fields=fields)
        result.filled_path = str(self._write_application(fields))
        result.report_path = self.report.result(self._report_text(result))

        return result

    # ── шаги ──────────────────────────────────────────────────────────────────

    def _read_tender_info(self) -> str:
        """Часть данных заявки берётся из требований тендера."""
        if not self.tender_info_path:
            return ""
        try:
            return DataParser(self.tender_info_path).origin_data()
        except Exception as exc:
            print(f"[WARN] Требования тендера не прочитаны: {exc}", flush=True)
            return ""

    def _read_base(self, folder: Optional[str]) -> str:
        """Читает все файлы базы знаний и склеивает их в markdown."""
        return self.base_loader.load(folder)

    @staticmethod
    def _build_context(md_knowledge_base: str, md_tender_info: str) -> str:
        parts = []
        if md_knowledge_base:
            parts.append("## БАЗА ЗНАНИЙ И ЭТАЛОННЫЕ ЗАЯВКИ\n\n" + md_knowledge_base)
        if md_tender_info:
            parts.append("## ТРЕБОВАНИЯ ТЕНДЕРА\n\n" + md_tender_info)
        return "\n\n---\n\n".join(parts)

    def _write_application(self, fields: List[FilledField]) -> Path:
        """Сохраняет заполненный шаблон с цветовой разметкой статусов."""
        source = Path(self.application_template_path)
        output = Path(self.results_path) / f"{source.stem}_заполнено{source.suffix}"
        return self.report_writer.write(fields, output_path=output, source_path=source)

    @staticmethod
    def _report_text(result: TenderApplicationResult) -> str:
        labels = {
            FieldStatus.FOUND: "Есть (зелёный)",
            FieldStatus.CHECK: "Проверить (жёлтый)",
            FieldStatus.MISSING: "Нет (красный)",
        }

        lines = [
            "# Отчёт по заполнению заявки",
            "",
            f"Файл заявки: {result.filled_path}",
            "",
            "| № | Поле | Значение | Статус | Источник | Примечание |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for i, field in enumerate(result.fields, start=1):
            lines.append(
                f"| {i} | {field.label} | {field.value} | {labels[field.status]} "
                f"| {field.source} | {field.note} |"
            )

        counters = {status: 0 for status in FieldStatus}
        for field in result.fields:
            counters[field.status] += 1

        lines += [
            "",
            "## Итого",
            "",
            f"- Заполнено: {counters[FieldStatus.FOUND]}",
            f"- Требует проверки: {counters[FieldStatus.CHECK]}",
            f"- Не найдено: {counters[FieldStatus.MISSING]}",
        ]
        return "\n".join(lines)
