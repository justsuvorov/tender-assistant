from pathlib import Path
from typing import Dict, List, Optional, Sequence

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
from tender_assistant.core.parsers import (
    DataParser,
    InlineBlank,
    find_inline_blanks,
    render_blanks_context,
)
from tender_assistant.core.pydantic_models import (
    FieldStatus,
    FilledField,
    FormField,
    TenderApplicationResult,
)
from tender_assistant.reports.report_export import BaseReport, TenderApplicationReport
from tender_assistant.reports.writers import ReportWriter, TenderReportWriter

_WORD_SUFFIXES = {".docx", ".doc"}


# ── Шаблон заявки ─────────────────────────────────────────────────────────────

class FormTemplate:
    """Файл шаблона заявки и его представления.

    Разные заполнители смотрят на шаблон по-разному: построчный — на
    markdown, инлайн-пропуски — на объектную модель docx. Держать оба
    чтения здесь дешевле и честнее, чем перечитывать файл в каждом:
    представления собираются лениво и переиспользуются.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._markdown: Optional[str] = None
        self._document = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_word(self) -> bool:
        return self._path.suffix.lower() in _WORD_SUFFIXES

    @property
    def markdown(self) -> str:
        if self._markdown is None:
            self._markdown = DataParser(str(self._path)).origin_data()
        return self._markdown

    @property
    def document(self) -> Document:
        """Объектная модель docx. Только для Word-шаблонов (см. is_word)."""
        if self._document is None:
            self._document = Document(str(self._path))
        return self._document


# ── Материалы для заполнения ──────────────────────────────────────────────────

class ApplicationContext:
    """Интерфейс сборки материалов, по которым заполняется заявка."""

    def build(self) -> str:
        raise NotImplementedError


class KnowledgeBaseContext(ApplicationContext):
    """Материалы = база знаний организации + требования тендера.

    Требования тендера нужны наравне с базой знаний: часть данных заявки
    (предмет закупки, номер лота, сроки) есть только в них.
    """

    def __init__(
        self,
        tender_info_path: Optional[str],
        knowledge_base_folder: Optional[str],
        base_loader: NormativeBaseLoader = None,
    ):
        self.tender_info_path = tender_info_path
        self.knowledge_base_folder = knowledge_base_folder
        self.base_loader = base_loader or NormativeBaseLoader()

    def build(self) -> str:
        parts = []

        knowledge_base = self.base_loader.load(self.knowledge_base_folder)
        if knowledge_base:
            parts.append("## БАЗА ЗНАНИЙ И ЭТАЛОННЫЕ ЗАЯВКИ\n\n" + knowledge_base)

        tender_info = self._read_tender_info()
        if tender_info:
            parts.append("## ТРЕБОВАНИЯ ТЕНДЕРА\n\n" + tender_info)

        return "\n\n---\n\n".join(parts)

    def _read_tender_info(self) -> str:
        if not self.tender_info_path:
            return ""
        try:
            return DataParser(self.tender_info_path).origin_data()
        except Exception as exc:
            # Заявку всё равно нужно заполнить тем, что есть в базе знаний.
            print(f"[WARN] Требования тендера не прочитаны: {exc}", flush=True)
            return ""


# ── Запросы к модели ──────────────────────────────────────────────────────────

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


# ── Заполнители формы ─────────────────────────────────────────────────────────

class TenderForm:
    """Интерфейс заполнения формы заявки.

    Реализация получает шаблон целиком и сама решает, как его читать:
    построчно по markdown или по объектной модели docx.
    """

    def prepare(self, template: FormTemplate, context: str) -> List[FilledField]:
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

    def prepare(self, template: FormTemplate, context: str) -> List[FilledField]:
        """Заполняет шаблон значениями из базы знаний и требований тендера."""
        fields = self._extract_queries_from_form(template.markdown)
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


class InlineBlanksForm(TenderForm):
    """Заполняет пропуски внутри абзацев (несколько на один абзац ячейки).

    Отдельный заполнитель, а не ветка внутри AITenderForm: построчный разбор
    идёт по плоскому markdown, где несколько пропусков одного абзаца
    неразличимы, и даёт одно значение на ячейку. Здесь же нужна объектная
    модель docx, чтобы адресовать каждый пропуск отдельно.
    """

    def __init__(self, inline_query: InlineBlanksQuery):
        self.inline_query = inline_query

    def prepare(self, template: FormTemplate, context: str) -> List[FilledField]:
        if not template.is_word:
            return []

        blanks = find_inline_blanks(template.document)
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


class CompositeTenderForm(TenderForm):
    """Несколько заполнителей на один шаблон, результаты складываются.

    Заполнители независимы и адресуют разные места документа, поэтому
    порядок влияет только на порядок полей в отчёте.
    """

    def __init__(self, forms: Sequence[TenderForm]):
        self.forms = list(forms)

    def prepare(self, template: FormTemplate, context: str) -> List[FilledField]:
        fields: List[FilledField] = []
        for form in self.forms:
            fields.extend(form.prepare(template, context))
        return fields


# ── Сохранение заполненной заявки ─────────────────────────────────────────────

class ApplicationOutput:
    """Куда и чем сохраняется заполненная заявка."""

    def __init__(self, results_path: Optional[str] = None, report_writer: ReportWriter = None):
        self.results_path = results_path or settings.results_root
        self.report_writer = report_writer or TenderReportWriter()

    def save(self, template: FormTemplate, fields: Sequence[FilledField]) -> Path:
        """Сохраняет копию шаблона с проставленными значениями и разметкой."""
        source = template.path
        output = Path(self.results_path) / f"{source.stem}_заполнено{source.suffix}"
        return self.report_writer.write(fields, output_path=output, source_path=source)


# ── Оркестратор ───────────────────────────────────────────────────────────────

class TenderApplication:
    """Этап 3: заполнить шаблон заявки данными из базы знаний.

    Оркестратор: собирает материалы, отдаёт их заполнителю формы, сохраняет
    результат и пишет отчёт. Сам ничего не читает и не разбирает — вся
    работа с документами живёт в FormTemplate, ApplicationContext,
    TenderForm и ApplicationOutput.
    """

    def __init__(
        self,
        template: FormTemplate,
        context: ApplicationContext,
        tender_form: TenderForm,
        output: ApplicationOutput = None,
        report: BaseReport = None,
    ):
        self.template = template
        self.context = context
        self.tender_form = tender_form
        self.output = output or ApplicationOutput()
        self.report = report or TenderApplicationReport()

    def result(self) -> TenderApplicationResult:
        context = self.context.build()
        fields = self.tender_form.prepare(self.template, context)

        result = TenderApplicationResult(fields=fields)
        result.filled_path = str(self.output.save(self.template, fields))
        result.report_path = self.report.result(self._report_text(result))

        return result

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
