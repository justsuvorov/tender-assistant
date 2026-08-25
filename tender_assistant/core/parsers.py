import re
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd
import pdfplumber
from docx import Document
from docx.oxml.ns import qn


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class MarkdownTableBuilder:
    """Convert tabular data (list of rows) to a GFM markdown table string."""

    @staticmethod
    def from_rows(rows: list[list[str]]) -> str:
        """
        rows[0] is treated as the header row.
        All rows must have the same number of columns (call normalise first).
        """
        if not rows:
            return ""

        col_widths = [3] * len(rows[0])
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(cell))

        def fmt_row(values: list[str]) -> str:
            cells = [v.ljust(col_widths[i]) for i, v in enumerate(values)]
            return "| " + " | ".join(cells) + " |"

        separator = "| " + " | ".join("-" * w for w in col_widths) + " |"

        lines = [fmt_row(rows[0]), separator]
        for row in rows[1:]:
            lines.append(fmt_row(row))

        return "\n".join(lines)

    @staticmethod
    def normalise(rows: list[list]) -> list[list[str]]:
        """Pad short rows and stringify every cell."""
        if not rows:
            return []
        max_cols = max(len(r) for r in rows)
        result = []
        for row in rows:
            cells = [str(c).replace("\n", " ").replace("\r", "").strip() if c is not None else "" for c in row]
            cells += [""] * (max_cols - len(cells))
            result.append(cells)
        return result


class WordParagraphConverter:
    """Extract plain text from a Word paragraph XML element, applying heading prefixes."""

    _WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    _HEADING_MAP = {
        "heading1": "#",  "Heading1": "#",
        "heading2": "##", "Heading2": "##",
        "heading3": "###","Heading3": "###",
        "heading4": "####","Heading4": "####",
    }

    def convert(self, element) -> str:
        text = "".join(node.text or "" for node in element.iter(qn("w:t"))).strip()
        if not text:
            return ""

        style_el = element.find(f".//{{{self._WNS}}}pStyle")
        style_val = style_el.get(f"{{{self._WNS}}}val", "") if style_el is not None else ""

        prefix = self._HEADING_MAP.get(style_val, "")
        return f"{prefix} {text}" if prefix else text


class WordTableExtractor:
    """Extract rows from a Word table XML element as list[list[str]]."""

    _WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def extract(self, element) -> list[list[str]]:
        ns = self._WNS
        rows = []
        for tr in element.findall(f"{{{ns}}}tr"):
            cells = []
            for tc in tr.findall(f"{{{ns}}}tc"):
                text = "".join(t.text or "" for t in tc.iter(f"{{{ns}}}t")).strip()
                cells.append(text)
            if cells:
                rows.append(cells)
        return rows


class PdfPageExtractor:
    """Extract text and tables from a single pdfplumber page."""

    def extract(self, page) -> list[str]:
        """Return list of markdown strings (text blocks and tables) for this page."""
        parts = []

        raw_tables = page.extract_tables()
        table_bboxes = [t.bbox for t in page.find_tables()] if raw_tables else []

        for raw_table in raw_tables:
            rows = MarkdownTableBuilder.normalise(raw_table)
            md = MarkdownTableBuilder.from_rows(rows)
            if md:
                parts.append(md)

        text = self._extract_text_outside_tables(page, table_bboxes)
        if text:
            parts.insert(0, text)

        return parts

    @staticmethod
    def _extract_text_outside_tables(page, table_bboxes: list) -> str:
        if not table_bboxes:
            return (page.extract_text() or "").strip()

        remaining = page
        for bbox in table_bboxes:
            try:
                remaining = remaining.outside_bbox(bbox)
            except Exception:
                pass
        return (remaining.extract_text() or "").strip()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class Parser(ABC):
    """Base interface for reading documents and converting to markdown."""

    @abstractmethod
    def read_document(self, file_path: str) -> str:
        pass


class Excel(Parser):
    """Read .xlsx/.xls files. Skips empty sheets, converts each sheet to a markdown table."""

    def read_document(self, file_path: str) -> str:
        xl = pd.ExcelFile(file_path)
        sections = []

        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name)

            if df.dropna(how="all").empty:
                continue

            df = (
                df.dropna(how="all")
                  .dropna(axis=1, how="all")
                  .reset_index(drop=True)
                  .fillna("")
            )

            rows = [list(df.columns.astype(str))] + df.astype(str).values.tolist()
            rows = MarkdownTableBuilder.normalise(rows)
            md_table = MarkdownTableBuilder.from_rows(rows)

            sections.append(f"## Лист: {sheet_name}\n\n{md_table}")

        return "\n\n".join(sections)


class Word(Parser):
    """Read .docx files. Converts headings, paragraphs, and tables to markdown."""

    def __init__(self):
        self._para_converter = WordParagraphConverter()
        self._table_extractor = WordTableExtractor()

    def read_document(self, file_path: str) -> str:
        doc = Document(file_path)
        parts = []

        for block in doc.element.body:
            tag = block.tag.split("}")[-1]

            if tag == "p":
                md = self._para_converter.convert(block)
                if md:
                    parts.append(md)

            elif tag == "tbl":
                rows = self._table_extractor.extract(block)
                rows = MarkdownTableBuilder.normalise(rows)
                md = MarkdownTableBuilder.from_rows(rows)
                if md:
                    parts.append(md)

        return "\n\n".join(parts)


class PDF(Parser):
    """Read .pdf files. Extracts text and tables page by page."""

    def __init__(self):
        self._page_extractor = PdfPageExtractor()

    def read_document(self, file_path: str) -> str:
        sections = []

        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                parts = self._page_extractor.extract(page)
                if parts:
                    sections.append(
                        f"## Страница {page_num}\n\n" + "\n\n".join(parts)
                    )

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class DataParser:
    """Read a file and return its content as a markdown string."""

    _SUPPORTED: dict[str, type[Parser]] = {
        ".xlsx": Excel,
        ".xlsm": Excel,
        ".xls":  Excel,
        ".docx": Word,
        ".doc":  Word,
        ".pdf":  PDF,
    }

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.parser = self._build_engine(file_path)

    def origin_data(self, file_path: str) -> str:
        """Read document, apply initial cleaning, return markdown string."""
        raw = self.parser.read_document(file_path)
        return self._clean(raw)

    def _build_engine(self, file_path: str) -> Parser:
        ext = Path(file_path).suffix.lower()
        parser_cls = self._SUPPORTED.get(ext)
        if parser_cls is None:
            raise ValueError(
                f"Unsupported file format: '{ext}'. "
                f"Supported: {list(self._SUPPORTED.keys())}"
            )
        return parser_cls()

    @staticmethod
    def _clean(data: str) -> str:
        data = data.strip()
        data = re.sub(r"\n{3,}", "\n\n", data)
        return data
