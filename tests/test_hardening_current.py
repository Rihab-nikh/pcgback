import pytest

from app.models.invoice import ExtractedInvoiceData, InvoiceLineItem
from app.services import accounts
from app.services.accounting import compute_invoice, generate_journal_entries
from app.services.bank_import import _norm_amount, parse_csv
from app.services.dates import normalize_date, due_before_invoice
from app.services.posting import to_cents, from_cents, selected_book_entries
from app.services.tax_rules import withholding_metadata_complete, tax_risk_level
from app.services.validation import run_validation


@pytest.fixture(autouse=True)
def _clean_account_cache():
    accounts.clear_cache()
    yield
    accounts.clear_cache()


def purchase(**overrides):
    data = dict(
        invoice_type="DOIT",
        invoice_category="facture_achat",
        date="2026-06-18",
        accounting_date="2026-06-18",
        invoice_number="FAC-UNIT-001",
        supplier_name="FOURNISSEUR TEST",
        supplier_ice="001234567890123",
        document_direction="purchase",
        accounting_nature="merchandise",
        montant_brut=10_000,
        tva_pct=20,
        requested_perspective="buyer",
    )
    data.update(overrides)
    return ExtractedInvoiceData(**data)


async def pipeline(data):
    calc = compute_invoice(data)
    entries = await generate_journal_entries(data, calc)
    checks, balances, verdict = run_validation(data, calc, entries)
    return calc, entries, checks, balances, verdict


@pytest.mark.asyncio
async def test_complete_purchase_invoice_is_valid_single_entity():
    calc, entries, checks, balances, verdict = await pipeline(purchase())
    assert verdict == "VALID", [(c.check_id, c.description) for c in checks if not c.passed]
    assert [e.perspective for e in entries] == ["Buyer"]
    assert all(b.balanced for b in balances)
    buyer = entries[0]
    assert any(l.account_number == "6111" and l.side == "DEBIT" for l in buyer.lines)
    assert any(l.account_number == "34552" and l.amount == 2000 for l in buyer.lines)
    assert any(l.account_number == "4411" and l.amount == calc.ttc for l in buyer.lines)


@pytest.mark.asyncio
async def test_payment_method_is_not_payment_evidence():
    _, entries, _, _, verdict = await pipeline(purchase(payment_mode="banque"))
    assert verdict == "VALID"
    assert all("Settlement" not in e.perspective for e in entries)


@pytest.mark.asyncio
async def test_withholding_is_fail_closed_without_metadata_and_valid_with_metadata():
    incomplete = purchase(
        invoice_category="facture_service",
        accounting_nature="professional_fees",
        retenue_a_la_source_pct=10,
    )
    assert withholding_metadata_complete(incomplete) is False
    assert tax_risk_level(incomplete) == "critical"
    _, _, checks, _, verdict = await pipeline(incomplete)
    assert verdict == "INVALID"
    assert any(c.check_id == 10 and not c.passed for c in checks)

    complete = purchase(
        invoice_category="facture_service",
        accounting_nature="professional_fees",
        retenue_a_la_source_pct=10,
        withholding_type="cit_iit",
        withholding_legal_basis="Configured firm rule / accountant-approved legal basis",
        withholding_base="net_financier_ht",
    )
    calc, entries, checks, balances, verdict = await pipeline(complete)
    assert verdict == "VALID", [(c.check_id, c.description, c.explanation) for c in checks if not c.passed]
    buyer = entries[0]
    assert any(l.account_number == "4452" and l.amount == 1000 for l in buyer.lines)
    assert any(l.account_number == "4411" and l.amount == calc.net_a_payer for l in buyer.lines)
    assert all(b.balanced for b in balances)


@pytest.mark.asyncio
async def test_credit_note_requires_reason_then_validates():
    missing_reason = purchase(invoice_type="AVOIR", document_direction="purchase_credit_note")
    _, _, checks, _, verdict = await pipeline(missing_reason)
    assert verdict == "INVALID"
    assert any(c.check_id == 23 and not c.passed for c in checks)

    valid_note = purchase(
        invoice_type="AVOIR",
        document_direction="purchase_credit_note",
        credit_note_reason="return_goods",
    )
    _, entries, checks, balances, verdict = await pipeline(valid_note)
    assert verdict == "VALID", [(c.check_id, c.description) for c in checks if not c.passed]
    assert any(l.account_number == "4411" and l.side == "DEBIT" for l in entries[0].lines)
    assert all(b.balanced for b in balances)


@pytest.mark.asyncio
async def test_2026_mixed_vat_routes_assets_and_charges():
    lines = [
        InvoiceLineItem(description="Laptop", unit_price=8000, line_total_ht=8000, tva_rate=20,
                        is_immobilisation=True, immobilisation_type="it"),
        InvoiceLineItem(description="Books", unit_price=2000, line_total_ht=2000, tva_rate=10,
                        accounting_nature="studies_documentation"),
    ]
    data = purchase(
        line_items=lines,
        capitalization_policy="capitalize",
        montant_brut=10_000,
    )
    calc, entries, checks, balances, verdict = await pipeline(data)
    assert calc.tva_amount == 1800.0
    assert verdict == "VALID", [(c.check_id, c.description, c.explanation) for c in checks if not c.passed]
    accts = {l.account_number for l in entries[0].lines}
    assert {"2355", "6141", "34551", "34552", "4411"} <= accts
    assert all(b.balanced for b in balances)


def test_money_centime_round_trip():
    assert to_cents("1234.567") == 123457
    assert from_cents(123457) == 1234.57
    assert to_cents(-0.005) == -1


def test_locale_bank_amounts_and_csv():
    assert _norm_amount("1.234,56") == 1234.56
    assert _norm_amount("1,234.56") == 1234.56
    assert _norm_amount("(42,50)") == -42.50
    rows = parse_csv("date;libelle;debit;credit;reference\n18/06/2026;FRAIS;25,50;;X1\n19/06/2026;VIREMENT;;1.200,00;X2\n")
    assert rows[0]["amount"] == -25.5
    assert rows[1]["amount"] == 1200.0


def test_dates_are_normalized_and_due_date_checked():
    assert normalize_date("18/06/2026") == "2026-06-18"
    assert normalize_date("18.06.26") == "2026-06-18"
    assert due_before_invoice("2026-06-18", "2026-06-17") is True
    assert due_before_invoice("2026-06-18", "2026-07-18") is False


def test_posting_selects_only_legal_owner_and_excludes_settlement():
    response = {
        "step1_identification": {"invoice_category": "facture_achat", "requested_perspective": "buyer"},
        "step4_journal_entries": [
            {"perspective": "Buyer", "lines": []},
            {"perspective": "Seller", "lines": []},
            {"perspective": "Buyer — Settlement", "lines": []},
        ],
    }
    assert selected_book_entries(response) == [{"perspective": "Buyer", "lines": []}]
