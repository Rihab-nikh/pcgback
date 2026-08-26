"""Database access for PostgreSQL with an explicit SQLite test/legacy fallback.

Production uses PostgreSQL through psycopg 3. Existing repository SQL is kept
small and readable by adapting the legacy qmark placeholder style centrally.
SQLite support exists only for deterministic tests and one-shot legacy data
migration; production is prevented from using it by config.py.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import DATABASE_BACKEND, DATABASE_PATH, DATABASE_URL

_SQLITE_SCHEMA = Path(__file__).parent.parent / "db" / "schema.sql"
_POSTGRES_SCHEMA = Path(__file__).parent.parent / "db" / "schema_postgres.sql"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def _qmark_to_pyformat(sql: str) -> str:
    """Convert SQLite-style ? bind markers to psycopg %s markers.

    The project does not use '?' operators in SQL. A tiny quote-aware scanner
    avoids touching literal question marks should one be added later.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                # SQL escapes quote characters by doubling them.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            # psycopg uses percent for bind syntax; literal SQL percent signs
            # (for example LIKE '2%') must be doubled.
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_JSON_EXTRACT_RE = re.compile(
    r"json_extract\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*'\$\.([A-Za-z0-9_.]+)'\s*\)",
    flags=re.IGNORECASE,
)


def _postgres_sql(sql: str) -> str:
    """Translate the small SQLite SQL surface still used by repositories."""
    statement = sql.strip()
    if statement.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"

    # SQLite null-safe comparison: `col IS ?` with a bound None/value.
    sql = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", sql, flags=re.IGNORECASE)

    # SQLite JSON scalar extraction -> PostgreSQL jsonb text extraction.
    def repl(match: re.Match[str]) -> str:
        column, path = match.groups()
        parts = ", ".join("'%s'" % p for p in path.split("."))
        return f"jsonb_extract_path_text(({column})::jsonb, {parts})"

    sql = _JSON_EXTRACT_RE.sub(repl, sql)

    # SQLite INSERT OR IGNORE -> standard PostgreSQL conflict ignore.
    ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE))
    if ignore:
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.IGNORECASE)
        stripped = sql.rstrip().rstrip(";")
        if " ON CONFLICT " not in stripped.upper():
            sql = stripped + " ON CONFLICT DO NOTHING"

    return _qmark_to_pyformat(sql)



def _split_sql_script(script: str) -> list[str]:
    """Split DDL on semicolons while respecting SQL quotes/comments."""
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    i = 0
    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
                buf.append(ch)
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class _Connection:
    """Small adapter exposing the sqlite3-like methods the service layer uses."""

    def __init__(self, raw: Any, backend: str):
        self.raw = raw
        self.backend = backend

    def execute(self, sql: str, params: tuple | list = ()):
        if self.backend == "postgresql":
            sql = _postgres_sql(sql)
        return self.raw.execute(sql, tuple(params))

    def executemany(self, sql: str, params_seq):
        if self.backend == "postgresql":
            sql = _postgres_sql(sql)
        return self.raw.executemany(sql, params_seq)

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self.raw.executescript(script)
            return
        for statement in _split_sql_script(script):
            self.raw.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()
        return False


def connect() -> _Connection:
    if DATABASE_BACKEND == "sqlite":
        assert DATABASE_PATH is not None
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(DATABASE_PATH)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        return _Connection(raw, "sqlite")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised in deployment env
        raise RuntimeError(
            "PostgreSQL is configured but psycopg is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return _Connection(raw, "postgresql")


def _sqlite_migrate(conn: _Connection) -> None:
    """Compatibility upgrades for legacy SQLite test databases only."""

    def columns(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    cols = columns("firms")
    if cols and "accounting_software" not in cols:
        conn.execute("ALTER TABLE firms ADD COLUMN accounting_software TEXT")
    if cols and "country" not in cols:
        conn.execute("ALTER TABLE firms ADD COLUMN country TEXT NOT NULL DEFAULT 'MA'")
    if cols and "currency" not in cols:
        conn.execute("ALTER TABLE firms ADD COLUMN currency TEXT NOT NULL DEFAULT 'MAD'")
    if cols and "logo" not in cols:
        conn.execute("ALTER TABLE firms ADD COLUMN logo TEXT")
    if cols and "settings" not in cols:
        conn.execute("ALTER TABLE firms ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")

    cols = columns("users")
    if cols and "department" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN department TEXT")
    if cols and "phone" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")

    cols = columns("invoices")
    if cols and "is_archived" not in cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")
    for name, ddl in (
        ("extraction_confidence", "REAL"),
        ("validation_pass_rate", "REAL"),
        ("accounting_rule_confidence", "REAL"),
        ("tax_risk_level", "TEXT"),
        ("reviewer_confidence", "REAL"),
        ("supplier_ice", "TEXT"),
        ("customer_name", "TEXT"),
        ("customer_ice", "TEXT"),
        ("source_hash", "TEXT"),
        ("currency", "TEXT NOT NULL DEFAULT 'MAD'"),
        ("exchange_rate", "TEXT NOT NULL DEFAULT '1'"),
        ("document_direction", "TEXT NOT NULL DEFAULT 'purchase'"),
        ("ttc_cents", "INTEGER"),
        ("net_a_payer_cents", "INTEGER"),
        ("validation_override_note", "TEXT"),
        ("posting_status", "TEXT NOT NULL DEFAULT 'unposted'"),
        ("posting_date", "TEXT"),
        ("posted_by", "TEXT"),
        ("posted_at", "TEXT"),
    ):
        if cols and name not in cols:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {name} {ddl}")

    cols = columns("bank_transactions")
    if cols and "amount_cents" not in cols:
        conn.execute("ALTER TABLE bank_transactions ADD COLUMN amount_cents INTEGER")
        conn.execute("UPDATE bank_transactions SET amount_cents=CAST(ROUND(amount*100) AS INTEGER) WHERE amount_cents IS NULL")

    cols = columns("manual_entry_lines")
    if cols and "amount_cents" not in cols:
        conn.execute("ALTER TABLE manual_entry_lines ADD COLUMN amount_cents INTEGER")
        conn.execute("UPDATE manual_entry_lines SET amount_cents=CAST(ROUND(amount*100) AS INTEGER) WHERE amount_cents IS NULL")

    cols = columns("bank_statements")
    for name, ddl in (
        ("opening_balance_cents", "INTEGER"),
        ("closing_balance_cents", "INTEGER"),
        ("debit_total_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("credit_total_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("control_difference_cents", "INTEGER"),
        ("statement_hash", "TEXT"),
        ("period_start", "TEXT"),
        ("period_end", "TEXT"),
    ):
        if cols and name not in cols:
            conn.execute(f"ALTER TABLE bank_statements ADD COLUMN {name} {ddl}")

    fks = conn.execute("PRAGMA foreign_key_list(lettrage_lines)").fetchall()
    if any(fk[2] == "invoices" for fk in fks):
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""CREATE TABLE lettrage_lines_v13 (
            lettrage_id TEXT NOT NULL REFERENCES lettrages(id),
            firm_id     TEXT NOT NULL REFERENCES firms(id),
            invoice_id  TEXT NOT NULL,
            entry_idx   INTEGER NOT NULL,
            line_idx    INTEGER NOT NULL,
            side        TEXT NOT NULL,
            amount      REAL NOT NULL,
            UNIQUE (firm_id, invoice_id, entry_idx, line_idx))""")
        conn.execute("INSERT INTO lettrage_lines_v13 SELECT * FROM lettrage_lines")
        conn.execute("DROP TABLE lettrage_lines")
        conn.execute("ALTER TABLE lettrage_lines_v13 RENAME TO lettrage_lines")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lettrage_lines ON lettrage_lines(lettrage_id)")
        conn.execute("PRAGMA foreign_keys=ON")

    cols = columns("posting_batches")
    for name, ddl in (("manual_entry_id", "TEXT"), ("bank_transaction_id", "TEXT"), ("reversed_by", "TEXT")):
        if cols and name not in cols:
            conn.execute(f"ALTER TABLE posting_batches ADD COLUMN {name} {ddl}")

    cols = columns("invoice_edits")
    if cols and "edit_session_id" not in cols:
        conn.execute("ALTER TABLE invoice_edits ADD COLUMN edit_session_id TEXT NOT NULL DEFAULT ''")

    cols = columns("supplier_priors")
    for name, ddl in (
        ("locked", "INTEGER NOT NULL DEFAULT 0"),
        ("rule_source", "TEXT NOT NULL DEFAULT 'ai_learned'"),
        ("payment_account", "TEXT"),
        ("rule_description", "TEXT"),
        ("auto_publish", "INTEGER NOT NULL DEFAULT 0"),
        ("extract_line_items", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if cols and name not in cols:
            conn.execute(f"ALTER TABLE supplier_priors ADD COLUMN {name} {ddl}")


SCHEMA_VERSION = 16


def _seed_reference_rules(conn: _Connection) -> None:
    ts = now()
    account_rules = {
        "sale.merchandise": ("7111", "Ventes de marchandises au Maroc"),
        "sale.service": ("7124", "Ventes de services produits au Maroc"),
        "purchase.merchandise": ("6111", "Achats de marchandises"),
        "purchase.raw_material": ("6121", "Achats de matières premières"),
        "purchase.consumable_supplies": ("6122", "Achats de matières et fournitures consommables"),
        "purchase.nonstocked_supplies": ("6125", "Achats non stockés de matières et fournitures"),
        "purchase.rent": ("6131", "Locations et charges locatives"),
        "purchase.maintenance": ("6133", "Entretien et réparations"),
        "purchase.insurance": ("6134", "Primes d'assurances"),
        "purchase.professional_fees": ("61365", "Honoraires"),
        "purchase.studies_documentation": ("6141", "Études, recherches et documentation"),
        "purchase.transport": ("6142", "Transports"),
        "purchase.advertising": ("6144", "Publicité, publications et relations publiques"),
        "purchase.telecom": ("6145", "Frais postaux et télécommunications"),
        "purchase.banking_services": ("6147", "Services bancaires"),
        "purchase.royalties": ("6137", "Redevances pour brevets, marques et droits similaires"),
        "asset.building": ("2321", "Bâtiments"),
        "asset.installation": ("2331", "Installations techniques"),
        "asset.equipment": ("2332", "Matériel et outillage"),
        "asset.vehicle": ("2340", "Matériel de transport"),
        "asset.furniture": ("2351", "Mobilier de bureau"),
        "asset.it": ("2355", "Matériel informatique"),
        "asset.other": ("2380", "Autres immobilisations corporelles"),
        "vat.input.asset": ("34551", "Etat — TVA récupérable (Immobilisations)"),
        "vat.input.expense": ("34552", "Etat — TVA récupérable (Charges)"),
        "vat.output": ("4455", "Etat — TVA facturée"),
        "partner.supplier": ("4411", "Fournisseurs"),
        "partner.customer": ("3421", "Clients"),
    }
    for key, (num, label) in account_rules.items():
        rid = "platform-account-" + key.replace(".", "-")
        conn.execute("""INSERT OR IGNORE INTO account_rules
            (id,firm_id,rule_key,account_number,account_label,effective_from,legal_basis,is_active,created_at)
            VALUES(?,NULL,?,?,?,?,?,1,?)""",
            (rid, key, num, label, "1900-01-01", "PCG Maroc — règle plateforme à confirmer selon politique du cabinet", ts))

    tax_rows = [
        ("vat-legacy-0", "VAT_RATE", "legacy", "1900-01-01", "2025-12-31", "0", "historical_rate", None),
        ("vat-legacy-7", "VAT_RATE", "legacy", "1900-01-01", "2025-12-31", "7", "historical_rate", None),
        ("vat-legacy-10", "VAT_RATE", "legacy", "1900-01-01", "2025-12-31", "10", "historical_rate", None),
        ("vat-legacy-14", "VAT_RATE", "legacy", "1900-01-01", "2025-12-31", "14", "historical_rate", None),
        ("vat-legacy-20", "VAT_RATE", "legacy", "1900-01-01", "2025-12-31", "20", "historical_rate", None),
        ("vat-2026-0", "VAT_RATE", "general", "2026-01-01", None, "0", "2026_rate_set", None),
        ("vat-2026-10", "VAT_RATE", "general", "2026-01-01", None, "10", "2026_rate_set", None),
        ("vat-2026-20", "VAT_RATE", "general", "2026-01-01", None, "20", "2026_rate_set", None),
    ]
    for rid, ttype, nature, start, end, rate, treatment, account in tax_rows:
        conn.execute("""INSERT OR IGNORE INTO tax_rules
            (id,firm_id,tax_type,transaction_nature,effective_from,effective_to,rate,legal_basis,account_number,recoverability,tax_treatment_code,required_evidence,is_active,created_at)
            VALUES(?,NULL,?,?,?,?,?,?,?,?,?,'[]',1,?)""",
            (rid, ttype, nature, start, end, rate, "Versioned Moroccan tax-rate registry; verify transaction-specific legal basis", account, None, treatment, ts))


def lock_posting_sequence(conn: _Connection, firm_id: str, fiscal_year: int) -> None:
    """Serialize per-firm/year journal numbering on PostgreSQL.

    SQLite's BEGIN IMMEDIATE already serializes writers. PostgreSQL needs an
    explicit transaction-scoped advisory lock to preserve unique sequencing
    under concurrent posting requests.
    """
    if conn.backend == "postgresql":
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"posting:{firm_id}:{fiscal_year}",))


def init_db() -> None:
    with connect() as conn:
        if DATABASE_BACKEND == "sqlite":
            _sqlite_migrate(conn)
            conn.executescript(_SQLITE_SCHEMA.read_text(encoding="utf-8"))
            _sqlite_migrate(conn)
        else:
            conn.executescript(_POSTGRES_SCHEMA.read_text(encoding="utf-8"))
        _seed_reference_rules(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (SCHEMA_VERSION, "postgresql-runtime-migration", now()),
        )
        conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(sql, params)
        conn.commit()
