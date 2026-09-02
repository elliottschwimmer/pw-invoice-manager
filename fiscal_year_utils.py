"""City of Berkeley fiscal year runs 7/1 - 6/30. Labeled by the year it
ends in, e.g. "FY26" = 7/1/2025 - 6/30/2026."""
from datetime import date


def fiscal_year_label_for(d: date) -> str:
    end_year = d.year + 1 if d.month >= 7 else d.year
    return f"FY{str(end_year)[-2:]}"


def current_fiscal_year_label() -> str:
    return fiscal_year_label_for(date.today())


def current_fiscal_year_end_date() -> date:
    today = date.today()
    end_year = today.year + 1 if today.month >= 7 else today.year
    return date(end_year, 6, 30)


def po_needs_fiscal_year_review(po) -> bool:
    if po.fiscal_year_scope != "fiscal_year":
        return False
    return po.fiscal_year_label != current_fiscal_year_label()
