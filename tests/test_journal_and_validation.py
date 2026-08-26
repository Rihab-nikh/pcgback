"""Journal generation + validation tests.

Runs with ACCOUNTS_OFFLINE=1 (set in conftest.py) so no network calls are
made — account resolution goes straight to the hardcoded PCG fallbacks.
"""
import pytest

from app.models.invoice import ExtractedInvoiceData, InvoiceLineItem
from app.services import accounts
from app.services.accounting import compute_invoice, generate_journal_entries
from app.services.validation import run_validation


@pytest.fixture(autouse=True)
def _clean_cache():
    accounts.clear_cache()
    yield
    accounts.clear_cache()


def make(**kw) -> ExtractedInvoiceData:
    base = dict(
        invoice_type="DOIT", invoice_category="facture_achat", montant_brut=10_000,
        date="2026-06-18", accounting_date="2026-06-18",
        invoice_number="JRN-0001", supplier_name="FOURNISSEUR TEST",
        supplier_ice="001234567890123", document_direction="purchase",
        accounting_nature="merchandise", requested_perspective="buyer",
    )
    base.update(kw)
    return ExtractedInvoiceData(**base)


async def run(data: ExtractedInvoiceData):
    calc = compute_invoice(data)
    entries = await generate_journal_entries(data, calc)
    checks, balances, verdict = run_validation(data, calc, entries)
    return calc, entries, checks, balances, verdict


@pytest.mark.asyncio
async def test_doit_invoice_balances_and_validates():
    calc, entries, checks, balances, verdict = await run(make(tva_pct=20))
    assert verdict == "VALID"
    assert all(b.balanced for b in balances)
    # Hardened workflow posts one legal owner only (buyer for a purchase).
    assert [e.perspective for e in entries] == ["Buyer"]
    supplier_line = next(l for l in entries[0].lines if l.account_number == "4411")
    assert supplier_line.amount == calc.ttc


@pytest.mark.asyncio
async def test_immobilisation_uses_class2_and_34551():
    data = make(is_immobilisation=True, immobilisation_type="it", capitalization_policy="capitalize", tva_pct=20)
    _, entries, checks, _, verdict = await run(data)
    assert verdict == "VALID"
    buyer = next(e for e in entries if e.perspective == "Buyer")
    assert any(l.account_number.startswith("23") for l in buyer.lines)      # capitalized
    assert any(l.account_number == "34551" for l in buyer.lines)            # asset TVA
    assert not any(l.account_number == "34552" for l in buyer.lines)


@pytest.mark.asyncio
async def test_ras_creates_4452_credit_and_reduced_fournisseur():
    data = make(invoice_category="facture_service", accounting_nature="professional_fees", montant_brut=5_000,
                tva_pct=20, retenue_a_la_source_pct=10,
                withholding_type="cit_iit",
                withholding_legal_basis="Configured firm rule / accountant-approved legal basis",
                withholding_base="net_financier_ht")
    calc, entries, checks, balances, verdict = await run(data)
    assert verdict == "VALID"
    buyer = next(e for e in entries if e.perspective == "Buyer")
    ras = next(l for l in buyer.lines if l.account_number == "4452")
    assert ras.amount == 500
    fournisseur = next(l for l in buyer.lines if l.account_number == "4411")
    assert fournisseur.amount == 5_500  # ttc - ras


@pytest.mark.asyncio
async def test_multiline_mixed_tva_rates_split_correctly():
    # Laptop 8000@20% (asset), books 400@0%, installation 2000@10% (asset), service 500@7%
    lines = [
        InvoiceLineItem(description="Ordinateur", unit_price=8000, line_total_ht=8000, tva_rate=20,
                        is_immobilisation=True, immobilisation_type="it"),
        InvoiceLineItem(description="Livres", quantity=2, unit_price=200, line_total_ht=400, tva_rate=0),
        InvoiceLineItem(description="Installation", unit_price=2000, line_total_ht=2000, tva_rate=10,
                        is_immobilisation=True, immobilisation_type="installation"),
        InvoiceLineItem(description="Maintenance", unit_price=500, line_total_ht=500, tva_rate=7),
    ]
    # Note: montant_brut = sum of lines; the invoice-level tva_pct drives calc.tva_amount,
    # and the multiline split distributes it with rounding correction on the last group.
    data = make(montant_brut=10_900, tva_pct=20, line_items=lines)
    calc, entries, _, _, _ = await run(data)
    buyer = next(e for e in entries if e.perspective == "Buyer")
    # One debit per (type, rate) group
    goods_debits = [l for l in buyer.lines if l.side == "DEBIT" and not l.account_number.startswith("345")]
    assert len(goods_debits) == 4
    # TVA split sums to total TVA
    tva_debits = [l for l in buyer.lines if l.account_number in ("34551", "34552")]
    assert round(sum(l.amount for l in tva_debits), 2) == calc.tva_amount


@pytest.mark.asyncio
async def test_multiline_with_discounts_balances():
    """Regression: multi-line + discounts used to produce unbalanced buyer
    entries (gross line debits vs net-based fournisseur credit)."""
    lines = [
        InvoiceLineItem(description="Ordinateur", unit_price=8000, line_total_ht=8000, tva_rate=20,
                        is_immobilisation=True, immobilisation_type="it"),
        InvoiceLineItem(description="Maintenance", unit_price=2000, line_total_ht=2000, tva_rate=20),
    ]
    data = make(montant_brut=10_000, tva_pct=20, remise_pct=10, escompte_pct=2, line_items=lines)
    calc, entries, checks, balances, verdict = await run(data)
    assert verdict == "VALID"
    assert all(b.balanced for b in balances)
    buyer = next(e for e in entries if e.perspective == "Buyer")
    # Goods debits sum exactly to net commercial (discounts distributed)
    goods = [l for l in buyer.lines if l.side == "DEBIT" and not l.account_number.startswith("345")]
    assert round(sum(l.amount for l in goods), 2) == calc.net_commercial == 9_000.00
    # Laptop group scaled 8000 -> 7200; service group absorbs the rest
    assert sorted(l.amount for l in goods) == [1_800.00, 7_200.00]
    # TVA split still sums exactly to total TVA
    tva_lines = [l for l in buyer.lines if l.account_number in ("34551", "34552")]
    assert round(sum(l.amount for l in tva_lines), 2) == calc.tva_amount


@pytest.mark.asyncio
async def test_multiline_no_discount_amounts_unchanged():
    """The fix must not alter discount-free multi-line behavior."""
    lines = [
        InvoiceLineItem(description="Ordinateur", unit_price=8000, line_total_ht=8000, tva_rate=20,
                        is_immobilisation=True, immobilisation_type="it"),
        InvoiceLineItem(description="Maintenance", unit_price=2000, line_total_ht=2000, tva_rate=20),
    ]
    data = make(montant_brut=10_000, tva_pct=20, line_items=lines)
    calc, entries, _, balances, verdict = await run(data)
    assert verdict == "VALID"
    buyer = next(e for e in entries if e.perspective == "Buyer")
    goods = [l for l in buyer.lines if l.side == "DEBIT" and not l.account_number.startswith("345")]
    assert sorted(l.amount for l in goods) == [2_000.00, 8_000.00]


@pytest.mark.asyncio
async def test_avoir_reverses_and_balances():
    _, entries, _, balances, verdict = await run(make(invoice_type="AVOIR", document_direction="purchase_credit_note", credit_note_reason="return_goods", tva_pct=20))
    assert verdict == "VALID"
    buyer = next(e for e in entries if e.perspective == "Buyer")
    assert any(l.side == "DEBIT" and l.account_number == "4411" for l in buyer.lines)


@pytest.mark.asyncio
async def test_payment_mode_does_not_create_settlement_when_ras_present():
    data = make(invoice_category="facture_service", accounting_nature="professional_fees", montant_brut=5_000, tva_pct=20,
                retenue_a_la_source_pct=10, payment_mode="banque",
                withholding_type="cit_iit",
                withholding_legal_basis="Configured firm rule / accountant-approved legal basis",
                withholding_base="net_financier_ht")
    calc, entries, _, balances, verdict = await run(data)
    assert verdict == "VALID" and calc.net_a_payer == 5_500
    assert all("Settlement" not in e.perspective for e in entries)
    assert all(b.balanced for b in balances)
