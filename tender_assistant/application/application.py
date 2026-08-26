from pathlib import Path
from typing import Dict, List, Optional

from docx import Document

from tender_assistant.ai.model import AIModel
from tender_assistant.ai.postprocessor import (
    FormFieldsResponse,
    InlineBlanksResponse,
    PostProcessor,
    TenderRowPostProcessor,
)
from tender_assistant.ai.promt_builders import NormativeBaseLoader, PromptEngine
from tender_assistant.core.config import settings
from tender_assistant.core.parsers import DataParser, InlineBlank, find_inline_blanks, render_blanks_context
from tender_assistant.core.pydantic_models import (
    FieldStatus,
    FilledField,
    FormField,
    TenderApplicationResult,
)
from tender_assistant.reports.report_export import BaseReport, TenderApplicationReport
from tender_assistant.reports.writers import ReportWriter, TenderReportWriter

_WORD_SUFFIXES = {".docx", ".doc"}


class TenderQuery:
    """Интерфейс поиска значения одного поля заявки."""

    def result(self, field_label: str, context: str) -> dict:
        raise NotImplementedError


class TenderAIQuery(TenderQuery):
    """Ищет значение поля заявки в базе знаний и требованиях тендера через LLM.

    Материалы (эталонные заявки + требования тендера) — общие для всех полей
    одной заявки, а полей бывают десятки. Если модель поддерживает prompt
    caching (``ai_model.supports_prompt_caching``), материалы отправляются
    отдельным кэшируемым блоком одинаковыми на каждый вызов: первый раз —
    по полной цене, следующие в течение ~5 минут — по цене чтения кэша.
    Иначе — как раньше: материалы обрезаются под конкретное поле, чтобы не
    жечь токены и не переполнять окно (актуально для self-hosted моделей
    с небольшим контекстом).
    """

    def __init__(
        self,
        ai_model: AIModel,
        tender_row_postprocessor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.prompt_engine = prompt_engine or PromptEngine()
        self.tender_row_postprocessor = tender_row_postprocessor or TenderRowPostProcessor()
        self.ai_model = ai_model
        self.context_template = settings.tender_form_fill_context_template
        self.field_template = settings.tender_form_fill_field_template

    def result(self, field_label: str, context: str) -> dict:
        """Возвращает {'value', 'status', 'source', 'note'} для поля заявки."""
        query = self.prompt_engine.render(self.field_template, field_label=field_label)

        if self.ai_model.supports_prompt_caching:
            materials = self.prompt_engine.render(self.context_template, context=context)
            response = self.ai_model.response_with_cache(materials, query)
        else:
            materials = self.prompt_engine.render(
                self.context_template,
                fit_key="context",
                fit_query=field_label,
                context=context,
            )
            response = self.ai_model.response(materials + "\n" + query)

        return self.tender_row_postprocessor.report(response)


class InlineBlanksQuery(TenderQuery):
    """Пакетный запрос значений для пропусков внутри абзацев шаблона.

    В отличие от TenderAIQuery (один вызов на поле), здесь ВСЕ пропуски
    уходят ОДНИМ запросом: у форм вроде Росатова таких пропусков может
    набраться больше десятка (несколько на один пронумерованный пункт).
    По вызову на каждый означало бы столько же round-trip'ов даже при
    включённом prompt caching — кэш экономит стоимость и повторную
    обработку контекста, но не сетевую задержку самого вызова.
    """

    def __init__(
        self,
        ai_model: AIModel,
        response_post_processor: PostProcessor = None,
        prompt_engine: PromptEngine = None,
    ):
        self.ai_model = ai_model
        self.response_post_processor = response_post_processor or InlineBlanksResponse()
        self.prompt_engine = prompt_engine or PromptEngine()
        self.context_template = settings.tender_form_fill_context_template
        self.blanks_template = settings.tender_form_inline_blanks_template

    def result(self, blanks: List[InlineBlank], context: str) -> Dict[str, dict]:
        """Возвращает {"<id>": {'value','status','source','note'}, ...}."""
        if not blanks:
            return {}

        blanks_context = render_blanks_context(blanks)
        query = self.prompt_engine.render(
            self.blanks_template, blanks_context=blanks_context
        )

        if self.ai_model.supports_prompt_caching:
            materials = self.prompt_engine.render(self.context_template, context=context)
            response = self.ai_model.response_with_cache(materials, query)
        else:
            materials = self.prompt_engine.render(
                self.context_template,
                fit_key="context",
                fit_query=blanks_context,
                context=context,
            )
            response = self.ai_model.response(materials + "\n" + query)

        return self.response_post_processor.report(response)


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
        inline_query: InlineBlanksQuery = None,
    ):
        self.report_writer = report_writer or TenderReportWriter()
        self.report = report or TenderApplicationReport()
        self.tender_form = tender_form
        self.normative_base_folder = normative_base_folder
        self.tender_info_path = tender_info_path
        self.application_template_path = application_template_path
        self.results_path = results_path or settings.results_root
        self.base_loader = base_loader or NormativeBaseLoader()
        self.inline_query = inline_query

    def result(self) -> TenderApplicationResult:
        """Читает шаблон и базу знаний, заполняет поля и сохраняет заявку."""
        md_template_form = DataParser(self.application_template_path).origin_data()
        md_tender_info = self._read_tender_info()
        md_knowledge_base = self._read_base(self.normative_base_folder)

        context = self._build_context(md_knowledge_base, md_tender_info)
        fields = self.tender_form.prepare(md_template_form, context)
        fields = fields + self._fill_inline_blanks(context)

        result = TenderApplicationResult(fields=fields)
        result.filled_path = str(self._write_application(fields))
        result.report_path = self.report.result(self._report_text(result))

        return result

    def _fill_inline_blanks(self, context: str) -> List[FilledField]:
        """Несколько пропусков в одном абзаце (см. core/parsers.py) — отдельный
        путь параллельно построчному: старый механизм даёт одно значение на
        одну ячейку и не умеет различать несколько пропусков внутри неё.
        """
        if self.inline_query is None:
            return []
        if Path(self.application_template_path).suffix.lower() not in _WORD_SUFFIXES:
            return []

        document = Document(self.application_template_path)
        blanks = find_inline_blanks(document)
        if not blanks:
            return []

        print(f"[INFO] Инлайн-пропусков в шаблоне найдено: {len(blanks)}", flush=True)
        answers = self.inline_query.result(blanks, context)

        fields = []
        for blank in blanks:
            answer = answers.get(str(blank.id)) or {
                "value": "", "status": "missing", "source": "", "note": "",
            }
            fields.append(FilledField(
                label=blank.label,
                anchor=str(blank.id),
                kind="inline",
                value=answer["value"],
                status=FieldStatus(answer["status"]),
                source=answer["source"],
                note=answer["note"],
            ))
        return fields

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
