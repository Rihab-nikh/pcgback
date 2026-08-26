# PILOT.md — The next 60 days (no code allowed)

Feature development is frozen as of this file. The plan below replaces the roadmap.

## Week 0 — Deploy (1–2 days)

Backend (Railway / Fly.io / any VPS):
```bash
pip install -r requirements.txt gunicorn
export OPENAI_API_KEY=sk-...
export JWT_SECRET=$(openssl rand -hex 32)        # NOT the dev default
export SUPER_ADMIN_EMAIL=you@...  SUPER_ADMIN_PASSWORD=...
export DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require'
export STORAGE_BACKEND=s3
export R2_ACCOUNT_ID=<account-id>
export R2_BUCKET=<bucket>
export R2_ACCESS_KEY_ID=<access-key>
export R2_SECRET_ACCESS_KEY=<secret-key>
gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000
```
Frontend (Vercel or same VPS): `npm install && npm run build`; set `API_URL`
to the backend URL. Fix whatever trivial build errors surface (expected: a
handful; the code was verified structurally, not compiled).

Before firm #1 touches it: run `pytest` (all suites), process 5 of your own
invoices end to end, check HTTPS, verify the PostgreSQL connection over TLS,
and confirm database backups/restore are configured before a real firm uploads
accounting data. Production must not use SQLite.

## Weeks 1–2 — Recruit 3 firms

Not friends. Real firms, even 2-person ones. The pitch (sell time, not AI):
> « Je traite gratuitement un mois de vos factures. Vous gardez votre logiciel
> — nous validons avant, et vous exportez le FEC dedans. Vous verrez sur vos
> propres chiffres combien d'erreurs sont intercept ées avant écriture. »

Setup per firm: register the firm, create their accountants, create 2–3
clients, sit with them for the first 10 invoices.

## Weeks 2–6 — Observe (the discipline part)

Sit beside them. Don't explain, don't defend. Log every: hesitation, ignored
feature, workaround, "je voudrais que…", and every 👎 with its reason.
The insight cards, confidence panel, and Mémoire IA page are hypotheses —
watch whether anyone actually reads them.

## Success criteria — defined BEFORE firm #1 (almost every target starts with "measure current")

| Metric | Baseline | Pilot target |
|---|---|---|
| Time to process one invoice | measure current (stopwatch, week 1) | −40 % or better |
| Corrections per invoice | measure week 1 | down month over month |
| AI approval rate (approve w/o edit) | 0 % at start | > 80 % after learning |
| Duplicates caught | current manual count | higher than baseline |
| Low-confidence invoices | measure week 1 | decreasing |
| Satisfaction (👍 share) | n/a | > 80 % positive |
| Close blockers found pre-close | current manual | up initially, then stable |
| Time searching for documents | measure current | significant reduction |

No baseline captured in week 1 = no improvement claim in the day-45 report.

## Measure — every metric already has an endpoint

| Metric (advisor's list) | Where |
|---|---|
| Avg processing time | `/metrics/outcomes` → avg_ai_processing_ms; time-to-approve = reviewed_at − created_at (SQL) |
| Approval rate | approved / (approved+rejected) from `/dashboard` stats |
| Corrections per invoice | `/metrics/outcomes` → corrections_per_invoice — **the learning curve; chart it weekly** |
| Confidence distribution | confidence column; per-field via response_json |
| Duplicate detection accuracy | flags from `/metrics/outcomes` vs 👎 wrong-duplicate feedback = false-positive rate |
| Month-end closing duration | ask the firm before (baseline) and time it with `/close/readiness` after |
| Satisfaction + failure modes | `/metrics/feedback` → satisfaction_rate + failure_modes ranked |
| DAU / retention | audit_logs: distinct user_id per day; firms active week over week |

## Day-45 report per firm (one page, their numbers)
- X invoices processed · Y duplicates blocked before posting · Z errors caught
- corrections/invoice: week 1 vs week 6 (the moat, measured)
- satisfaction rate + top failure mode
- their quote about month-end close

If the numbers are good → that page is your sales deck. If they're bad →
failure_modes tells you the ONE thing to fix. Either way: no speculative
features. The backlog is now written by accountants.
