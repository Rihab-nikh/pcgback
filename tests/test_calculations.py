"""Golden tests for the calculation cascade.

Each case is a hand-verified 'exam answer'. If a refactor changes any number,
these fail — that is the point.
"""
from app.models.invoice import ExtractedInvoiceData
from app.services.accounting import compute_invoice


def make(**kw) -> ExtractedInvoiceData:
    base = dict(invoice_type="DOIT", invoice_category="facture_achat", montant_brut=10_000)
    base.update(kw)
    return ExtractedInvoiceData(**base)


def test_plain_invoice_20pct_tva():
    calc = compute_invoice(make(tva_pct=20))
    assert calc.net_commercial == 10_000
    assert calc.tva_amount == 2_000
    assert calc.ttc == 12_000
    assert calc.net_a_payer == 12_000


def test_full_discount_cascade():
    # brut 10 000, rabais 5%, remise 10%, ristourne 2%, escompte 1%, TVA 20%
    calc = compute_invoice(make(rabais_pct=5, remise_pct=10, ristourne_pct=2, escompte_pct=1, tva_pct=20))
    assert calc.net1 == 9_500.00
    assert calc.net2 == 8_550.00
    assert calc.net_commercial == 8_379.00
    assert calc.escompte_amount == 83.79
    assert calc.net_financier_ht == 8_295.21
    assert calc.tva_amount == 1_659.04
    assert calc.ttc == 9_954.25


def test_ras_on_service_invoice():
    # HT 5 000, TVA 20% = 1 000, TTC 6 000, RAS 10% of HT = 500 -> net a payer 5 500
    calc = compute_invoice(make(
        invoice_category="facture_service", montant_brut=5_000,
        tva_pct=20, retenue_a_la_source_pct=10,
    ))
    assert calc.ttc == 6_000
    assert calc.retenue_a_la_source_amount == 500
    assert calc.net_a_payer == 5_500


def test_droits_de_timbre_added_on_top():
    calc = compute_invoice(make(montant_brut=1_000, tva_pct=20, droits_de_timbre=20))
    assert calc.ttc == 1_200
    assert calc.net_a_payer == 1_220


def test_conditional_cash_stamp_duty_is_not_applied_to_bank_payment():
    calc = compute_invoice(make(
        montant_brut=165.83,
        tva_pct=20,
        montant_ttc=199.00,
        droits_de_timbre=0.50,
        droits_de_timbre_mentionne=0.50,
        droits_de_timbre_condition="Uniquement en cas de règlement en espèces",
        net_a_payer_document=199.00,
        payment_mode="banque",
    ))
    assert calc.ttc == 199.00
    assert calc.droits_de_timbre == 0.00
    assert calc.net_a_payer == 199.00


def test_conditional_cash_stamp_duty_applies_when_cash_is_evidenced():
    calc = compute_invoice(make(
        montant_brut=165.83,
        tva_pct=20,
        droits_de_timbre_mentionne=0.50,
        droits_de_timbre_condition="Uniquement en cas de règlement en espèces",
        payment_mode="caisse",
    ))
    assert calc.droits_de_timbre == 0.50
    assert calc.net_a_payer == 199.50


def test_tva_exempt():
    calc = compute_invoice(make(tva_pct=0))
    assert calc.tva_amount == 0
    assert calc.ttc == 10_000
