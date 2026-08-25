from dataclasses import dataclass, field
from typing import List


@dataclass
class ReportRow:
    """Строка табличного отчёта ассистента."""

    subject: str          # требуемый документ или поле заявки
    value: str            # найденное значение / имя файла
    status: str           # "Есть" | "Нет" | "Проверить"
    comment: str = ""


@dataclass
class TenderReport:
    """Сводный отчёт по этапу работы ассистента."""

    title: str = "Отчёт"
    rows: List[ReportRow] = field(default_factory=list)
    summary: str = ""
    raw_text: str = ""  # исходный markdown, запасной вариант вывода

    @classmethod
    def merge(cls, reports: List["TenderReport"]) -> "TenderReport":
        return cls(
            title=reports[0].title if reports else "Отчёт",
            rows=[row for r in reports for row in r.rows],
            summary="\n\n".join(r.summary for r in reports if r.summary),
            raw_text="\n\n---\n\n".join(r.raw_text for r in reports if r.raw_text),
        )
