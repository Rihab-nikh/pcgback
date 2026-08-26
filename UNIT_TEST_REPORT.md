# Unit Test Report — PCG Maroc AI Hardened

Date: 2026-08-25

## Environment note

The sandbox did not have the `openai` Python package installed and had no network access to install it. For offline test collection only, a temporary external stub was placed on `PYTHONPATH`. The application source itself was not modified to fake OpenAI behavior.

## Results

### Current hardened deterministic unit suite

Command scope:
- `tests/test_calculations.py`
- `tests/test_hardening_current.py`

Result: **14 passed, 0 failed**.

Coverage of hardened behavior includes:
- invoice calculation cascade and VAT math
- Decimal/centime rounding
- buyer-only legal-entity journal generation
- no settlement journal merely because `payment_mode` is bank/cash
- withholding/RAS fail-closed when legal metadata is missing
- valid CIT/IIT withholding posting to 4452 with reduced supplier balance
- credit-note reason requirement and reversal posting
- 2026 mixed VAT rates (10%/20%) and split between asset VAT 34551 and charge VAT 34552
- localized Moroccan/French bank amount parsing
- date normalization and due-date consistency
- posted-book selection excludes the other legal entity and settlement views

### Full legacy backend suite

Result: **44 passed, 33 failed**.

This is not a clean release gate because the package's own `AUDIT_FIXES.md` states that the legacy tests were not migrated after hardening. Confirmed stale expectations include:
- expecting both buyer and seller journal perspectives
- expecting invoice/payment wording to generate settlement entries
- treating approved extraction JSON as ledger truth rather than normalized posted batches
- importing bank statements without required opening/closing balance controls
- mutating posted ODs instead of using immutable reversal
- weaker validation fixtures missing invoice date, number, supplier identity, document direction, withholding legal basis, etc.
- duplicate tests that reuse identical file bytes even though hardened duplicate controls flag them

## Release recommendation

The deterministic accounting core tested here is behaving consistently with the hardened doctrine. However, the repository is **not test-suite clean** because the old integration/API tests still encode pre-hardening behavior.

Before treating CI as a deployment gate, migrate or replace those 33 stale tests so that `pytest` can be expected to finish with 0 failures under the hardened accounting model.

For an accountant pilot, keep the accountant in review/approval mode and do not treat automated validation as professional sign-off. Golden accounting cases should be independently validated by the accountant.
