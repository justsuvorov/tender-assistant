import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Sequence

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, PatternFill

from tender_assistant.core.parsers import find_inline_blanks, xml_path
from tender_assistant.core.pydantic_models import DocumentListResult, FieldStatus, FilledField
from tender_assistant.reports.style import HEADER as _HEADER
from tender_assistant.reports.style import field_fill as _field_fill


# ── Подсветка в Word ──────────────────────────────────────────────────────────

_WORD_HIGHLIGHT = {
    FieldStatus.FOUND: WD_COLOR_INDEX.BRIGHT_GREEN,
    FieldStatus.CHECK: WD_COLOR_INDEX.YELLOW,
    FieldStatus.MISSING: WD_COLOR_INDEX.RED,
}

# Заглушки, которые шаблон оставляет под значение
_PLACEHOLDER = re.compile(r"_{3,}|\.{4,}|<[^<>]{0,80}>|«_+»|\[[^\[\]]{0,80}\]")

_MISSING_TEXT = "НЕ НАЙДЕНО"


def _normalise(text: str) -> str:
    """Нормализация текста для сопоставления: регистр, пробелы, пунктуация."""
    text = re.sub(r"[_\.]{2,}", " ", text or "")
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def _matches(cell_text: str, label: str) -> bool:
    """Ячейка/строка шаблона соответствует наименованию поля."""
    cell_norm = _normalise(cell_text)
    label_norm = _normalise(label)
    if not cell_norm or not label_norm:
        return False
    if cell_norm == label_norm:
        return True
    # Модель возвращает наименование поля с сокращениями и без нумерации,
    # поэтому засчитываем вхождение более длинной строки в короткую.
    shorter, longer = sorted((cell_norm, label_norm), key=len)
    return len(shorter) >= 8 and shorter in longer


def _display_value(field: FilledField) -> str:
    if field.status is FieldStatus.MISSING or not field.value:
        return _MISSING_TEXT
    return field.value


# ── Базовый интерфейс ─────────────────────────────────────────────────────────

class ReportWriter(ABC):
    """Записывает заполненную заявку в файл и возвращает путь к нему."""

    @abstractmethod
    def write(
        self,
        fields: Sequence[FilledField],
        output_path: Path,
        source_path: Optional[Path] = None,
    ) -> Path:
        pass


# ── Word ──────────────────────────────────────────────────────────────────────

class WordApplicationWriter(ReportWriter):
    """Заполняет .docx-шаблон заявки, выделяя значения цветом статуса."""

    def write(
        self,
        fields: Sequence[FilledField],
        output_path: Path,
        source_path: Optional[Path] = None,
    ) -> Path:
        if source_path is None:
            raise ValueError("Для заполнения шаблона нужен путь к исходному файлу")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, output_path)

        document = Document(str(output_path))

        # Инлайн-пропуски (несколько на один абзац ячейки, см. core/parsers.py)
        # идут отдельным путём: их адрес — конкретное место в абзаце
        # (anchor = id пропуска), а не поиск ячейки по тексту метки.
        inline_fields = [f for f in fields if f.kind == "inline"]
        table_fields = [f for f in fields if f.kind != "inline"]

        remaining = self._fill_tables(document, table_fields)
        remaining = self._fill_paragraphs(document, remaining)
        remaining += self._fill_inline_blanks(document, inline_fields)

        if remaining:
            self._append_unmatched(document, remaining)

        document.save(str(output_path))
        print(
            f"[INFO] Заявка заполнена: {len(fields) - len(remaining)}/{len(fields)} "
            f"полей размещено в шаблоне",
            flush=True,
        )
        return output_path

    def _fill_tables(
        self, document: Document, fields: List[FilledField]
    ) -> List[FilledField]:
        """Находит ячейку под каждое поле и пишет туда значение.

        Поиск и запись разнесены на два прохода намеренно: в реальных
        шаблонах (например, форма Росатова 223-ФЗ) колонка «Предложение
        участника» бывает объединена по вертикали на много строк — то есть
        несколько РАЗНЫХ полей формы физически указывают на ОДНУ и ту же
        ячейку. При записи «нашёл — сразу написал» второе и последующие
        поля молча затирали бы ответ предыдущих (см. регрессионный тест
        ``test_fields_sharing_a_merged_cell_are_not_overwritten``). Поэтому
        сначала поля группируются по фактической целевой ячейке (python-docx
        возвращает один и тот же объект для всех строк, охваченных
        объединением — ключ группировки строится через xml_path(), а не
        id(): на реальном документе id() Python-обёрток лживо совпадает и
        для НЕ объединённых ячеек тоже, см. docstring xml_path в core/parsers.py),
        и только затем пишутся: одно поле — как раньше, несколько — единым
        списком в одной ячейке.
        """
        remaining = []
        groups: dict = {}  # xml_path(tc) -> (cell, [fields])

        for field in fields:
            target = self._find_table_cell(document, field.label)
            if target is None:
                remaining.append(field)
                continue

            key = xml_path(target._tc)
            if key not in groups:
                groups[key] = (target, [])
            groups[key][1].append(field)

        for cell, matched_fields in groups.values():
            self._write_cell(cell, matched_fields)

        return remaining

    @staticmethod
    def _find_table_cell(document: Document, label: str):
        """Ячейка под значение для поля с данным наименованием, если есть."""
        for table in document.tables:
            for row in table.rows:
                cells = row.cells
                for idx, cell in enumerate(cells):
                    if _matches(cell.text, label):
                        target = WordApplicationWriter._value_cell(cells, idx)
                        if target is not None:
                            return target
        return None

    @staticmethod
    def _value_cell(cells, label_idx: int):
        """Ячейка под значение: первая пустая справа от наименования."""
        for cell in cells[label_idx + 1:]:
            if not cell.text.strip() or _PLACEHOLDER.search(cell.text):
                return cell
        # Двухколоночная форма без пустых ячеек — берём соседнюю справа.
        if label_idx + 1 < len(cells):
            return cells[label_idx + 1]
        return None

    def _write_cell(self, cell, fields) -> None:
        """Пишет одно или несколько полей (общая объединённая ячейка) в cell."""
        if isinstance(fields, FilledField):
            fields = [fields]

        if len(fields) == 1:
            field = fields[0]
            cell.text = _display_value(field)
            paragraph = cell.paragraphs[0]
            run = paragraph.runs[0] if paragraph.runs else paragraph.add_run("")
            run.font.highlight_color = _WORD_HIGHLIGHT[field.status]
            self._set_cell_background(cell, _field_fill(field.status.value))
            return

        # Несколько полей делят одну объединённую ячейку — каждое своей
        # строкой с меткой, подсветка своя на каждую строку. Фон ячейки не
        # красим одним цветом: статусы разных полей внутри неё могут не
        # совпадать.
        cell.text = ""
        for i, field in enumerate(fields):
            paragraph = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
            paragraph.add_run(f"{field.label}: ")
            run = paragraph.add_run(_display_value(field))
            run.font.highlight_color = _WORD_HIGHLIGHT[field.status]

    def _fill_inline_blanks(
        self, document: Document, fields: List[FilledField]
    ) -> List[FilledField]:
        """Пишет подобранные значения в поле СПРАВА от текста с пропусками.

        Сам текст требования не правится: пропуски вида ``____`` остаются
        на месте, а ответы появляются в соседней колонке («Предложение
        участника») — размеченные цветом статуса, чтобы человек проверил
        и перенёс их в текст сам. Автоподстановка прямо в формулировку
        требования сделала бы правку незаметной при вычитке.

        Пропуски находятся заново на ЭТОЙ копии документа (детектор
        детерминирован — тот же файл даёт те же id в том же порядке, что и
        при подборе значений в application/application.py), затем каждое
        поле сопоставляется по anchor (id пропуска) со своим местом.
        """
        if not fields:
            return []

        blanks = find_inline_blanks(document)
        by_anchor = {str(blank.id): blank for blank in blanks}

        groups: dict = {}  # xml_path(tc) -> (ячейка-приёмник, [поля])
        order: List[str] = []
        remaining = []

        for field in fields:
            blank = by_anchor.get(field.anchor)
            target = self._blank_answer_cell(blank) if blank else None
            if target is None:
                remaining.append(field)
                continue

            key = xml_path(target._tc)
            if key not in groups:
                groups[key] = (target, [])
                order.append(key)
            groups[key][1].append(field)

        for key in order:
            cell, matched_fields = groups[key]
            self._append_answers_to_cell(cell, matched_fields)

        return remaining

    @staticmethod
    def _blank_answer_cell(blank):
        """Ячейка справа от той, где стоит пропуск, или None для последней колонки.

        Для последней колонки приёмника нет — такое поле уходит в остаток и
        попадает в блок «Поля, не размещённые в шаблоне» в конце документа,
        а не пишется куда попало.
        """
        next_index = blank.cell_index + 1
        if not blank.row_cells or next_index >= len(blank.row_cells):
            return None
        return blank.row_cells[next_index]

    @staticmethod
    def _append_answers_to_cell(cell, fields: List[FilledField]) -> None:
        """Дописывает ответы в конец ячейки, не затирая её содержимое.

        В колонке-приёмнике обычно уже стоит подсказка шаблона («Да» / «Нет»,
        «указать реквизиты»): она нужна человеку при проверке, поэтому
        ответы добавляются под ней, а не вместо неё.
        """
        for field in fields:
            paragraph = cell.add_paragraph()
            paragraph.add_run(f"{field.label}: ")
            run = paragraph.add_run(_display_value(field))
            run.font.highlight_color = _WORD_HIGHLIGHT[field.status]

    def _fill_paragraphs(
        self, document: Document, fields: List[FilledField]
    ) -> List[FilledField]:
        remaining = []

        for field in fields:
            if not self._fill_in_paragraphs(document, field):
                remaining.append(field)

        return remaining

    def _fill_in_paragraphs(self, document: Document, field: FilledField) -> bool:
        for paragraph in document.paragraphs:
            text = paragraph.text
            if not text.strip() or not _matches(text, field.label):
                continue

            value = _display_value(field)

            if _PLACEHOLDER.search(text):
                new_text = _PLACEHOLDER.sub(value, text, count=1)
                self._rewrite_paragraph(paragraph, text, new_text, value, field)
            else:
                run = paragraph.add_run(f" {value}")
                run.font.highlight_color = _WORD_HIGHLIGHT[field.status]
            return True

        return False

    @staticmethod
    def _rewrite_paragraph(paragraph, old_text, new_text, value, field) -> None:
        """Перезаписывает абзац, подсвечивая только подставленное значение."""
        for run in list(paragraph.runs):
            run._element.getparent().remove(run._element)

        before, _, after = new_text.partition(value)

        if before:
            paragraph.add_run(before)
        highlighted = paragraph.add_run(value)
        highlighted.font.highlight_color = _WORD_HIGHLIGHT[field.status]
        if after:
            paragraph.add_run(after)

    def _append_unmatched(self, document: Document, fields: List[FilledField]) -> None:
        """Поля, которым не нашлось места в шаблоне, выносим в конец файла."""
        document.add_paragraph()
        document.add_heading("Поля, не размещённые в шаблоне", level=2)

        for field in fields:
            paragraph = document.add_paragraph()
            paragraph.add_run(f"{field.label}: ")
            run = paragraph.add_run(_display_value(field))
            run.font.highlight_color = _WORD_HIGHLIGHT[field.status]
            if field.note:
                paragraph.add_run(f" ({field.note})")

    @staticmethod
    def _set_cell_background(cell, hex_color: str) -> None:
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)


# ── Excel ─────────────────────────────────────────────────────────────────────

class ExcelApplicationWriter(ReportWriter):
    """Заполняет .xlsx-шаблон заявки, заливая ячейки цветом статуса."""

    def write(
        self,
        fields: Sequence[FilledField],
        output_path: Path,
        source_path: Optional[Path] = None,
    ) -> Path:
        if source_path is None:
            raise ValueError("Для заполнения шаблона нужен путь к исходному файлу")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, output_path)

        workbook = load_workbook(output_path)
        wrap = Alignment(vertical="top", wrap_text=True)
        placed = 0

        for field in fields:
            if self._fill_field(workbook, field, wrap):
                placed += 1

        workbook.save(output_path)
        print(
            f"[INFO] Заявка заполнена: {placed}/{len(fields)} полей "
            f"размещено в шаблоне",
            flush=True,
        )
        return output_path

    def _fill_field(self, workbook, field: FilledField, wrap: Alignment) -> bool:
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.value is None or isinstance(cell, MergedCell):
                        continue
                    if not _matches(str(cell.value), field.label):
                        continue

                    target = self._value_cell(worksheet, cell)
                    if target is None:
                        continue

                    target.value = _display_value(field)
                    target.fill = PatternFill(
                        "solid", fgColor=_field_fill(field.status.value)
                    )
                    target.alignment = wrap
                    return True
        return False

    @staticmethod
    def _value_cell(worksheet, label_cell):
        """Ячейка под значение: первая пустая справа в той же строке."""
        for column in range(label_cell.column + 1, label_cell.column + 6):
            candidate = worksheet.cell(row=label_cell.row, column=column)
            if isinstance(candidate, MergedCell):
                continue
            if candidate.value is None or not str(candidate.value).strip():
                return candidate

        candidate = worksheet.cell(row=label_cell.row, column=label_cell.column + 1)
        return None if isinstance(candidate, MergedCell) else candidate


# ── Выбор писателя по формату шаблона ─────────────────────────────────────────

class TenderReportWriter(ReportWriter):
    """Подбирает писателя под формат шаблона заявки.

    PDF-шаблоны заполнить нельзя, поэтому для них результат выгружается
    в .docx рядом с исходным файлом.
    """

    _WRITERS = {
        ".docx": WordApplicationWriter,
        ".doc": WordApplicationWriter,
        ".xlsx": ExcelApplicationWriter,
        ".xlsm": ExcelApplicationWriter,
        ".xls": ExcelApplicationWriter,
    }

    def write(
        self,
        fields: Sequence[FilledField],
        output_path: Path,
        source_path: Optional[Path] = None,
    ) -> Path:
        suffix = Path(source_path or output_path).suffix.lower()
        writer_cls = self._WRITERS.get(suffix)

        if writer_cls is None:
            print(
                f"[WARN] Формат шаблона '{suffix}' не поддерживает заполнение — "
                f"результат будет выгружен в .docx",
                flush=True,
            )
            return self._write_standalone(fields, output_path.with_suffix(".docx"))

        return writer_cls().write(fields, output_path, source_path)

    @staticmethod
    def _write_standalone(fields: Sequence[FilledField], output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        document = Document()
        document.add_heading("Заявка на участие в конкурсе", level=1)

        table = document.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for i, title in enumerate(["Поле", "Значение", "Примечание"]):
            cell = table.rows[0].cells[i]
            cell.text = title
            cell.paragraphs[0].runs[0].bold = True

        writer = WordApplicationWriter()
        for field in fields:
            cells = table.add_row().cells
            cells[0].text = field.label
            cells[2].text = field.note
            writer._write_cell(cells[1], field)

        document.save(str(output_path))
        return output_path


# ── Перечень документов (этап 1) ────────────────────────────────────────────

class DocumentListWriter(ReportWriter):
    """Оформляет перечень документов из этапа 1 в готовый Word-документ.

    Наследует общий интерфейс ``ReportWriter``, как и писатели заявки —
    в модуле один контракт «результат → файл» на все стадии, а не два
    параллельных. Дополняет диагностический markdown-отчёт
    (``DocumentListReport``), а не заменяет его: markdown остаётся
    машиночитаемым логом этапа, а этот файл — предъявляемым документом.
    ``source_path`` в этой реализации не используется — перечень
    собирается с нуля, а не поверх шаблона.
    """

    _HEADERS = ["№", "Документ", "Обязательность", "Примечание"]
    _EXCLUDED_HEADERS = ["Документ", "Причина исключения"]

    def write(
        self,
        result: DocumentListResult,
        output_path: Path,
        source_path: Optional[Path] = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        document = Document()
        document.add_heading("Перечень документов для участия в закупке", level=1)

        if result.source_headers:
            note = document.add_paragraph(
                "Источник: " + "; ".join(result.source_headers)
            )
            note.runs[0].italic = True

        self._write_documents_table(document, result)

        if result.excluded:
            document.add_heading(
                "Исключено при проверке по нормативной базе", level=2
            )
            self._write_excluded_table(document, result)

        document.save(str(output_path))
        print(f"[INFO] Перечень документов оформлен: {output_path}", flush=True)
        return output_path

    def _write_documents_table(self, document: Document, result: DocumentListResult) -> None:
        table = document.add_table(rows=1, cols=len(self._HEADERS))
        table.style = "Table Grid"
        self._write_header_row(table, self._HEADERS)

        for i, doc in enumerate(result.documents, start=1):
            cells = table.add_row().cells
            cells[0].text = str(i)
            cells[1].text = doc.name
            cells[2].text = "обязателен" if doc.mandatory else "по условию"
            cells[3].text = doc.note
            for cell in cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(10)

        self._apply_column_widths(table, [6, 40, 18, 40])

    def _write_excluded_table(self, document: Document, result: DocumentListResult) -> None:
        table = document.add_table(rows=1, cols=len(self._EXCLUDED_HEADERS))
        table.style = "Table Grid"
        self._write_header_row(table, self._EXCLUDED_HEADERS)

        for doc in result.excluded:
            cells = table.add_row().cells
            cells[0].text = doc.name
            cells[1].text = doc.note
            for cell in cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(10)

        self._apply_column_widths(table, [45, 55])

    @staticmethod
    def _write_header_row(table, titles: List[str]) -> None:
        cells = table.rows[0].cells
        for i, title in enumerate(titles):
            cells[i].text = title
            run = cells[i].paragraphs[0].runs[0]
            run.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            run.font.size = Pt(10)
            DocumentListWriter._set_cell_background(cells[i], _HEADER)

    @staticmethod
    def _apply_column_widths(table, weights: List[int]) -> None:
        """Ширина колонок пропорционально весам, суммарно ~17 см (лист A4)."""
        total = sum(weights)
        table_width_cm = 17.0
        for column, weight in zip(table.columns, weights):
            width = Cm(table_width_cm * weight / total)
            for cell in column.cells:
                cell.width = width

    @staticmethod
    def _set_cell_background(cell, hex_color: str) -> None:
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)
