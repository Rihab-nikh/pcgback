> **Read [DOCTRINE.md](DOCTRINE.md) before writing any code in this repo.**

# PCG Marocain Accounting Engine

Extracts invoice data from images (GPT-4o vision) and produces exam-ready
journal entries following Moroccan PCG rules, with TVA/RAS/immobilisation
routing and 14 automated validation checks.

This is the refactored, modular version of the original single-file service.
**Behavior is unchanged** — same endpoints, same request/response models,
same accounting logic.

## Structure

```
app/
├── main.py                  # FastAPI app, endpoints, pipeline orchestration
├── core/
│   ├── config.py            # All env/config access (one place)
│   └── logging.py           # codewords logger, stdlib fallback for tests
├── models/
│   └── invoice.py           # All Pydantic models
├── services/
│   ├── accounts.py          # Chart-of-accounts resolution + PCG constants
│   ├── accounting.py        # Calculation cascade + journal generation
│   ├── validation.py        # 14 checks + balance verification
│   └── reporting.py         # Summary table + markdown report
└── ai/
    ├── client.py            # Shared AsyncOpenAI client
    ├── extraction.py        # Vision extraction + post-parse invariants
    └── prompts/
        └── invoice_extraction.md   # The system prompt, versionable as a file
tests/
├── test_calculations.py             # Golden tests for the cascade
└── test_journal_and_validation.py   # Journal + validation tests (offline)
```

## What the refactor actually changed

1. **Six near-identical account resolvers collapsed into one.**
   `_resolve_account`, `_tva_charge_account`, `_tva_asset_account`,
   `_seller_tva_account`, `_fournisseur_account`, `_ras_account` were ~90%
   copy-paste. They are now one generic `resolve_account()` plus thin named
   wrappers preserving each one's original query, prefix, similarity
   threshold, and timeout.

2. **The TVA-split-by-group logic deduplicated.** It appeared twice
   (DOIT and AVOIR branches, ~40 lines each, identical except DEBIT/CREDIT).
   Now one `_split_multiline_tva()` helper; the side is a parameter.

3. **The immobilisation-type → search-query mapping** (repeated three times
   as if/elif chains) is now one dict, `IMMOBILISATION_QUERIES`.

4. **The 250-line extraction prompt moved to a markdown file**
   (`app/ai/prompts/invoice_extraction.md`) so it can be diffed and
   versioned independently of code.

5. **`ACCOUNTS_OFFLINE=1` env flag** skips the search service entirely and
   uses PCG fallbacks — this is what makes the test suite runnable with no
   network and no services.

6. **Magic account numbers named**: `CLIENT_ACCOUNT`, `ESCOMPTE_ACCORDE`,
   `BANQUE_ACCOUNT`, etc. live in `services/accounts.py`.

## Running

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
export ACCOUNT_SEARCH_URL=http://localhost:8001   # optional
uvicorn app.main:app --reload
```

## Tests

```bash
pip install pytest pytest-asyncio
pytest
```

The golden tests in `test_calculations.py` are hand-verified exam answers.
Any refactor that changes an amount breaks a test — add a new golden case
every time you find a real invoice the engine gets wrong.

## Fixed: multi-line invoices with discounts

The original monolith debited gross `line_total_ht` per group on the buyer
side while crediting the fournisseur at TTC computed from net commercial —
so any multi-line invoice with rabais/remise/ristourne could not balance.

The engine now distributes discounts proportionally: goods lines are scaled
by `net_commercial / montant_brut` (last group absorbs the rounding cent so
lines sum exactly to net commercial), and per-group TVA weights are scaled by
`net_financier_ht / montant_brut`. Discount-free multi-line invoices produce
byte-identical entries to before (covered by a regression test).

## v2 — Multi-tenant SaaS API

Everything below is implemented and covered by tests. PostgreSQL is the
default/runtime database via `DATABASE_URL`; production refuses SQLite. The
legacy SQLite adapter is retained only for deterministic tests and the one-shot
SQLite→PostgreSQL migration utility. JWT (HS256) and PBKDF2 remain stdlib.

Auth & roles: POST /auth/register-firm, /auth/login, /auth/refresh, GET /auth/me.
Roles: super_admin (platform only), firm_admin, accountant. Bootstrap super
admin from SUPER_ADMIN_EMAIL / SUPER_ADMIN_PASSWORD env.

Tenancy: every query is firm_id-scoped in the repository layer; accountants
are further restricted to clients assigned to them; cross-tenant access
returns 404 (never reveals existence). Suspended firms are locked out.

Platform (super admin): GET /platform/stats, /platform/firms,
PATCH /platform/firms/{id}?active=, GET /platform/audit.

Team (firm admin): GET/POST /team/accountants, PATCH /team/accountants/{id}
(role, deactivate; self-deactivation blocked). Workload counts included.

Clients: GET/POST /clients, GET/PATCH /clients/{id}, GET /clients/{id}/summary
(workspace header). Search, archive, accountant assignment.

Invoices: POST /invoices/upload?client_id= (stores original file under
private S3/R2 object storage, runs the AI pipeline, persists the full InvoiceResponse, derives
confidence = passed_checks/total, detects duplicates by firm+supplier+number+TTC),
GET /invoices (status/client/search/date filters + pagination),
GET /invoices/{id} (full stored response), GET /invoices/{id}/file,
POST /invoices/{id}/review (approve/reject), POST /invoices/{id}/reprocess.

Reporting: GET /dashboard (role-aware), GET /journal (flat ledger + balance),
GET /exports/journal.csv, GET /reports/monthly, GET /validation/summary
(failing checks ranked across the firm).

System: GET/POST notifications (invoice_processed / validation_failed /
duplicate_detected), GET /audit (firm) — who/what/when on every mutation.
GET /health.

Not built on purpose: bank reconciliation, payroll, document classification
beyond invoices, billing/Stripe — the AI pipeline does not support them yet,
and pretending otherwise would just be mock data with an endpoint.


## v3 — The intelligence layer (AI-first, not OCR-first)

Deterministic by design: every insight is computed from the firm's own invoice
history (SQL + arithmetic), so it runs in milliseconds and cannot hallucinate.
The LLM is used only where language is the problem (extraction, NL->filter
translation) — never as the source of accounting facts.

- Cross-invoice insights after every processing run (app/services/insights.py):
  duplicates, supplier ICE changes, VAT-rate deviation vs history,
  charge<->immobilisation classification drift, wrong-client detection,
  conflicts with the firm's learned preferences. Persisted, surfaced on the
  invoice (GET /invoices/{id} -> insights) and firm-wide (GET /insights).
- Learning from corrections (supplier_priors table): every approval confirms
  a supplier's profile, every human correction confirms it double. Established
  priors (2+ confirmations) check future extractions.
- Month-end close assistant (GET /close/readiness): "ready to close except
  for..." — pending reviews, failed checks, open duplicates, open anomalies,
  and probable MISSING invoices via supplier numbering-sequence gap detection.
- Explainability (account_explanations on invoice detail): why 2355, why
  34551, why 4452 — plain-language PCG reasoning grounded in the entry.
- Land-and-expand export (GET /exports/fec): FEC format importable by
  Cegid/Sage/Quadratus — firms keep their system, PCG Maroc AI becomes the
  AI processing + validation layer in front of it. Note: verify against your
  target system's import once; FEC dialects vary slightly.
- NL queries (POST /assistant/query): the LLM translates the question into a
  constrained FilterSpec; answers always come from the tenant-scoped SQL.

## v4 — Health, knowledge, trust

- GET /health/clients — Accounting Health Score per client (worst first), the
  manager scan view: score 0-100 + concrete issues + recommendation. Also on
  every client summary. Penalty weights live in app/services/health.py — tune
  them with real firms.
- GET /knowledge — the firm knowledge base: every learned supplier profile
  with its confirmation count ("TVA 20 %, immobilisation — confirmé 17 fois").
  When an extraction matches an established prior, the invoice detail carries
  the trust line: "conforme aux habitudes de votre cabinet, confirmé N fois."
- confidence_breakdown on invoice detail — "why 92%" decomposed into named
  factors: PCG checks, extraction read quality (per-field), supplier known,
  firm habits, open anomalies. Plus a verdict hint.
- GET /metrics/outcomes — measured numbers for pilot reports: duplicates
  blocked before posting, invalid entries caught, anomalies surfaced,
  corrections per invoice (watch it fall as the system learns), suppliers
  learned, AI processing time. No invented percentages.

# pcgback
