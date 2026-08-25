from dataclasses import dataclass, field


@dataclass
class ReportRow:
    client_requirement: str
    program_coverage: str
    status: str        # "Есть" | "Нет" | "Частично"
    comment: str


@dataclass
class InsuranceReport:
    rows: list[ReportRow] = field(default_factory=list)
    summary: str = ""
    raw_text: str = ""  # original LLM output, kept as fallback

    @classmethod
    def merge(cls, reports: list["InsuranceReport"]) -> "InsuranceReport":
        rows = [row for r in reports for row in r.rows]
        summary = "\n\n".join(r.summary for r in reports if r.summary)
        raw_text = "\n\n---\n\n".join(r.raw_text for r in reports if r.raw_text)
        return cls(rows=rows, summary=summary, raw_text=raw_text)
