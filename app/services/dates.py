"""Date normalization and accounting-period helpers.

All invoice/accounting dates are normalized to ISO ``YYYY-MM-DD`` before they
are persisted.  Raw OCR text remains available in the source document and
extraction audit trail; the accounting engine never compares locale-formatted
strings lexicographically.
"""
from __future__ import annotations

import re
from datetime import date, datetime


_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
)


def normalize_date(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # Common OCR/ISO timestamps: keep the calendar portion when it is valid.
    if re.match(r"^\d{4}-\d{2}-\d{2}[T\s]", raw):
        raw = raw[:10]
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    # Short French date DD/MM/YY.  00-79 => 2000s; 80-99 => 1900s.
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2})", raw)
    if m:
        yy = int(m.group(3))
        year = 2000 + yy if yy < 80 else 1900 + yy
        try:
            return date(year, int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def parse_iso(value: str | None) -> date | None:
    norm = normalize_date(value)
    if not norm:
        return None
    try:
        return date.fromisoformat(norm)
    except ValueError:
        return None


def is_future_date(value: str | None, *, today: date | None = None) -> bool:
    d = parse_iso(value)
    return bool(d and d > (today or date.today()))


def due_before_invoice(invoice_date: str | None, due_date: str | None) -> bool:
    inv = parse_iso(invoice_date)
    due = parse_iso(due_date)
    return bool(inv and due and due < inv)


def fiscal_year(value: str | None) -> int | None:
    d = parse_iso(value)
    return d.year if d else None
