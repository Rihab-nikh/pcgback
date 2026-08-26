"""Bank statement parsers: CSV, MT940 (:61:/:86:), CAMT.053 (ISO 20022 XML).

All parsers return the same normalized shape so the import endpoint and the
reconciliation service never care about the source format:

    {"date": "YYYY-MM-DD", "value_date": "YYYY-MM-DD" | None,
     "label": str, "reference": str | None, "amount": float}

Amounts are signed from the account holder's point of view:
credit (money in) > 0, debit (money out) < 0. Stdlib only — no new deps.
"""
import csv
import io
import re
import xml.etree.ElementTree as ET


class StatementParseError(ValueError):
    pass


def detect_format(filename: str, content: bytes) -> str:
    """csv | camt053 | mt940 — by extension first, then by content sniffing."""
    name = (filename or "").lower()
    head = content[:2000].decode("utf-8", errors="ignore")
    if name.endswith(".xml") or head.lstrip().startswith("<?xml") or "<Document" in head:
        return "camt053"
    if name.endswith((".sta", ".mt940", ".940")) or ":20:" in head or ":61:" in head:
        return "mt940"
    return "csv"


def parse_statement(filename: str, content: bytes) -> tuple[str, list[dict]]:
    fmt = detect_format(filename, content)
    text = content.decode("utf-8-sig", errors="replace")
    if fmt == "camt053":
        return fmt, parse_camt053(text)
    if fmt == "mt940":
        return fmt, parse_mt940(text)
    return fmt, parse_csv(text)


# ── CSV ──────────────────────────────────────────────────────────────────────
_DATE_KEYS = ("date", "date operation", "date opération", "booking date", "transaction date")
_VALUE_DATE_KEYS = ("date valeur", "value date", "valeur")
_LABEL_KEYS = ("libelle", "libellé", "label", "description", "narrative", "motif")
_REF_KEYS = ("reference", "référence", "ref", "numero", "n°")
_AMOUNT_KEYS = ("montant", "amount", "montant (mad)")
_DEBIT_KEYS = ("debit", "débit")
_CREDIT_KEYS = ("credit", "crédit")


def _norm_amount(raw: str) -> float | None:
    """Locale-safe amount parser for Moroccan/French and Anglo bank exports."""
    s = (raw or "").strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    # If both separators exist, the right-most one is the decimal separator.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                  # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) in (1, 2):
            s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s = "".join(parts)
    elif s.count(".") > 1:
        parts = s.split(".")
        if len(parts[-1]) in (1, 2):
            s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s = "".join(parts)
    try:
        value = float(s)
        return -abs(value) if neg else value
    except ValueError:
        return None


def _norm_date(raw: str) -> str | None:
    s = (raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", s)  # DD/MM/YYYY
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{2})[/.-](\d{2})[/.-](\d{2})$", s)      # DD/MM/YY
    if m:
        return f"20{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _pick(row: dict, keys: tuple[str, ...]) -> str:
    for k, v in row.items():
        if (k or "").strip().lower() in keys:
            return v or ""
    return ""


def parse_csv(text: str) -> list[dict]:
    delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise StatementParseError("CSV vide ou illisible")
    out: list[dict] = []
    for row in reader:
        date = _norm_date(_pick(row, _DATE_KEYS))
        if not date:
            continue  # skip headers/totals/blank lines
        amount = _norm_amount(_pick(row, _AMOUNT_KEYS))
        if amount is None:  # separate debit / credit columns
            debit = _norm_amount(_pick(row, _DEBIT_KEYS)) or 0.0
            credit = _norm_amount(_pick(row, _CREDIT_KEYS)) or 0.0
            if debit == 0.0 and credit == 0.0:
                continue
            amount = credit - abs(debit)
        out.append({
            "date": date,
            "value_date": _norm_date(_pick(row, _VALUE_DATE_KEYS)),
            "label": _pick(row, _LABEL_KEYS).strip() or "(sans libellé)",
            "reference": _pick(row, _REF_KEYS).strip() or None,
            "amount": round(amount, 2),
        })
    if not out:
        raise StatementParseError(
            "Aucune transaction reconnue — colonnes attendues : date, libellé, montant (ou débit/crédit)")
    return out


# ── MT940 ────────────────────────────────────────────────────────────────────
_MT940_61 = re.compile(
    r"^:61:(?P<date>\d{6})(?P<entry>\d{4})?(?P<sign>C|D|RC|RD)(?P<amount>[\d,]+)"
    r"(?P<code>[A-Z][A-Z0-9]{3})(?P<ref>[^\n]*)", re.MULTILINE)


def parse_mt940(text: str) -> list[dict]:
    out: list[dict] = []
    # Each :61: is a transaction; the following :86: block (if any) is its label
    blocks = re.split(r"(?=^:61:)", text, flags=re.MULTILINE)
    for block in blocks:
        m = _MT940_61.match(block.strip())
        if not m:
            continue
        d = m.group("date")
        date = f"20{d[0:2]}-{d[2:4]}-{d[4:6]}"
        amount = float(m.group("amount").replace(",", "."))
        if m.group("sign") in ("D", "RC"):  # debit, or reversal of credit
            amount = -amount
        label_m = re.search(r"^:86:(.*?)(?=^:\d{2}[A-Z]?:|\Z)", block, re.MULTILINE | re.DOTALL)
        label = " ".join(label_m.group(1).split()) if label_m else "(sans libellé)"
        out.append({
            "date": date, "value_date": date,
            "label": label[:500],
            "reference": (m.group("ref") or "").strip("/ ")[:100] or None,
            "amount": round(amount, 2),
        })
    if not out:
        raise StatementParseError("Aucune ligne :61: reconnue dans le fichier MT940")
    return out


# ── CAMT.053 ─────────────────────────────────────────────────────────────────
def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1]


def _find_text(elem: ET.Element, path_tail: str) -> str | None:
    for e in elem.iter():
        if _strip_ns(e.tag) == path_tail and e.text:
            return e.text.strip()
    return None


def parse_camt053(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise StatementParseError(f"XML CAMT.053 invalide : {e}")
    out: list[dict] = []
    for entry in root.iter():
        if _strip_ns(entry.tag) != "Ntry":
            continue
        amt_el = next((e for e in entry.iter() if _strip_ns(e.tag) == "Amt"), None)
        if amt_el is None or not amt_el.text:
            continue
        amount = float(amt_el.text)
        cdt_dbt = _find_text(entry, "CdtDbtInd") or "CRDT"
        if cdt_dbt == "DBIT":
            amount = -amount
        date = (_find_text(entry, "BookgDt") and _find_text(entry, "Dt")) or _find_text(entry, "Dt")
        label = _find_text(entry, "Ustrd") or _find_text(entry, "AddtlNtryInf") or "(sans libellé)"
        ref = _find_text(entry, "AcctSvcrRef") or _find_text(entry, "EndToEndId")
        if not date:
            continue
        out.append({
            "date": date[:10], "value_date": date[:10],
            "label": label[:500], "reference": ref, "amount": round(amount, 2),
        })
    if not out:
        raise StatementParseError("Aucune entrée <Ntry> trouvée dans le fichier CAMT.053")
    return out
