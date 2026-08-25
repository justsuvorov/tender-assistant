import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Sequence

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, PatternFill

from tender_assistant.core.pydantic_models import FieldStatus, FilledField
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
        remaining = list(fields)

        remaining = self._fill_tables(document, remaining)
        remaining = self._fill_paragraphs(document, remaining)

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
        remaining = []

        for field in fields:
            if not self._fill_in_tables(document, field):
                remaining.append(field)

        return remaining

    def _fill_in_tables(self, document: Document, field: FilledField) -> bool:
        for table in document.tables:
            for row in table.rows:
                cells = row.cells
                for idx, cell in enumerate(cells):
                    if not _matches(cell.text, field.label):
                        continue

                    target = self._value_cell(cells, idx)
                    if target is None:
                        continue

                    self._write_cell(target, field)
                    return True
        return False

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

    def _write_cell(self, cell, field: FilledField) -> None:
        cell.text = _display_value(field)
        paragraph = cell.paragraphs[0]
        run = paragraph.runs[0] if paragraph.runs else paragraph.add_run("")
        run.font.highlight_color = _WORD_HIGHLIGHT[field.status]
        self._set_cell_background(cell, _field_fill(field.status.value))

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
