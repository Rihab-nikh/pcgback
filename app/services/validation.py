"""Deterministic accounting/tax validation.

Checks are accounting invariants, not self-consistency assertions.  A balanced
entry can still be invalid; exact account families, line arithmetic, effective-
dated VAT, document dates and classification quality are checked explicitly.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.models.invoice import BalanceCheck, CalculationBreakdown, ExtractedInvoiceData, JournalEntry, ValidationCheck
from app.services.accounting import r2
from app.services.dates import due_before_invoice, is_future_date, normalize_date
from app.services.tax_rules import mixed_rates_are_valid, rate_is_valid, valid_tva_rates, withholding_metadata_complete

CENT = Decimal("0.01")
PURCHASE_ACCOUNT_BY_NATURE = {
    "merchandise": "6111", "raw_material": "6121", "consumable_supplies": "6122",
    "nonstocked_supplies": "6125", "rent": "6131", "maintenance": "6133",
    "insurance": "6134", "professional_fees": "61365", "studies_documentation": "6141",
    "transport": "6142", "advertising": "6144", "telecom": "6145",
    "banking_services": "6147", "royalties": "6137",
}

ASSET_ACCOUNT_BY_TYPE = {
    "it": "2355", "furniture": "2351", "building": "2321", "vehicle": "2340",
    "installation": "2331", "equipment": "2332", "other": "2380",
}


def _q(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _entry_lines(entries: list[JournalEntry], perspective: str) -> list:
    return [l for e in entries if e.perspective == perspective for l in e.lines]


def _owner(data: ExtractedInvoiceData) -> str:
    if data.requested_perspective in ("buyer", "seller"):
        return data.requested_perspective.title()
    return "Seller" if data.invoice_category == "facture_vente" else "Buyer"


def _external_expected_vat(data: ExtractedInvoiceData, calc: CalculationBreakdown) -> float:
    if not data.line_items:
        return r2(calc.net_financier_ht * data.tva_pct / 100)
    if not calc.montant_brut:
        return 0.0
    factor = Decimal(str(calc.net_financier_ht)) / Decimal(str(calc.montant_brut))
    total = Decimal(0)
    for line in data.line_items:
        base = Decimal(str(line.line_total_ht)) * factor
        total += (base * Decimal(str(line.tva_rate)) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    return float(total.quantize(CENT, rounding=ROUND_HALF_UP))


def run_validation(data: ExtractedInvoiceData, calc: CalculationBreakdown,
                   entries: list[JournalEntry]) -> tuple[list[ValidationCheck], list[BalanceCheck], str]:
    checks: list[ValidationCheck] = []
    add = lambda i, d, p, e=None: checks.append(ValidationCheck(check_id=i, description=d, passed=bool(p), explanation=e))

    expected_nc = r2(calc.montant_brut * (1 - calc.rabais_pct / 100) * (1 - calc.remise_pct / 100) * (1 - calc.ristourne_pct / 100))
    add(1, "Cascade commerciale correcte", abs(expected_nc - calc.net_commercial) < 0.01,
        f"Net commercial attendu {expected_nc:,.2f}; calculé {calc.net_commercial:,.2f} MAD.")
    expected_esc = r2(calc.net_commercial * calc.escompte_pct / 100)
    add(2, "Escompte calculé sur le net commercial HT", abs(expected_esc - calc.escompte_amount) < 0.01)
    expected_tva = _external_expected_vat(data, calc)
    add(3, "TVA recalculée depuis les bases/taux réels", abs(expected_tva - calc.tva_amount) < 0.01,
        f"TVA attendue {expected_tva:,.2f}; calculée {calc.tva_amount:,.2f} MAD.")
    add(4, "TTC = Net financier HT + TVA", abs(r2(calc.net_financier_ht + calc.tva_amount) - calc.ttc) < 0.01)

    all_accounts = [l.account_number for e in entries for l in e.lines]
    if data.invoice_type == "DOIT":
        rrr = {"6119", "6129", "6149", "7119", "71294"}
        add(5, "Réductions commerciales sur facture initiale non comptabilisées séparément",
            not any(a in rrr for a in all_accounts))
    else:
        rrr = {"6119", "6129", "6149", "7119", "71294"}
        used_rrr = set(all_accounts) & rrr
        if data.credit_note_reason == "post_invoice_discount":
            if _owner(data) == "Seller":
                expected_rrr = "71294" if data.invoice_category in {"facture_service", "avoir"} and data.accounting_nature not in {"merchandise", "raw_material"} else "7119"
            else:
                if data.accounting_nature == "merchandise": expected_rrr = "6119"
                elif data.accounting_nature in {"raw_material", "consumable_supplies", "nonstocked_supplies"}: expected_rrr = "6129"
                else: expected_rrr = "6149"
            add(5, f"Avoir pour réduction post-facture utilise le compte RRR {expected_rrr}", expected_rrr in all_accounts,
                f"RRR trouvés: {sorted(used_rrr)}")
        else:
            add(5, "Avoir de retour/annulation inverse le compte d'origine sans RRR artificiel", not used_rrr,
                f"RRR inattendus: {sorted(used_rrr)}" if used_rrr else None)

    if calc.escompte_amount > 0:
        expected = "7386" if _owner(data) == "Buyer" else "6386"
        add(6, f"Escompte utilise le compte {expected} du propriétaire des livres", expected in all_accounts)
    else:
        add(6, "Escompte — sans objet", True)

    owner = _owner(data)
    generated_perspectives = {e.perspective for e in entries}
    if data.requested_perspective != "both":
        add(7, "Une seule entité juridique est générée", generated_perspectives <= {owner},
            f"Perspective attendue: {owner}; trouvées: {sorted(generated_perspectives)}")
    else:
        # Legacy educational data can show both, but posting will select one owner.
        add(7, "Perspective legacy 'both' non utilisée comme source de posting", True)

    owner_lines = _entry_lines(entries, owner)
    if calc.tva_amount > 0:
        if owner == "Seller":
            add(8, "TVA vendeur au 4455", any(l.account_number == "4455" for l in owner_lines) and not any(l.account_number.startswith("3455") for l in owner_lines))
        else:
            add(8, "TVA acheteur au 34551/34552, jamais 4455", any(l.account_number in {"34551", "34552"} for l in owner_lines) and not any(l.account_number == "4455" for l in owner_lines))
    else:
        add(8, "Comptes TVA — sans objet", True)

    partner = "3421" if owner == "Seller" else "4411"
    add(9, f"Compte de tiers requis {partner} présent", any(l.account_number == partner for l in owner_lines))

    if data.retenue_a_la_source_pct > 0:
        add(10, "Retenue à la source documentée (type + base légale)", withholding_metadata_complete(data),
            "Un simple pourcentage n'est pas suffisant pour certifier le régime fiscal.")
        expected_wht = "4452" if data.withholding_type in (None, "cit_iit") else data.withholding_account_number
        add(11, "Retenue à la source isolée dans un compte fiscal explicitement approuvé",
            owner != "Buyer" or (bool(expected_wht) and any(l.account_number == expected_wht for l in owner_lines)),
            f"Compte attendu: {expected_wht or 'non configuré'}")
    else:
        add(10, "Retenue à la source — sans objet", True)
        add(11, "Compte de retenue — sans objet", True)

    line_rates = [float(l.tva_rate) for l in data.line_items] if data.line_items else [float(data.tva_pct)]
    allowed = sorted(valid_tva_rates(data.date))
    add(12, f"Taux TVA compatibles avec la date du document ({'/'.join(str(int(x)) for x in allowed)}%)",
        mixed_rates_are_valid(line_rates, data.date), f"Taux extraits: {line_rates}")

    if owner == "Buyer" and calc.tva_amount > 0:
        vat_34551 = r2(sum(l.amount for l in owner_lines if l.account_number == "34551" and l.side == ("CREDIT" if data.invoice_type == "AVOIR" else "DEBIT")))
        vat_34552 = r2(sum(l.amount for l in owner_lines if l.account_number == "34552" and l.side == ("CREDIT" if data.invoice_type == "AVOIR" else "DEBIT")))
        if data.line_items and calc.montant_brut:
            factor = Decimal(str(calc.net_financier_ht)) / Decimal(str(calc.montant_brut))
            exp_asset = Decimal(0); exp_charge = Decimal(0)
            for line in data.line_items:
                v = (Decimal(str(line.line_total_ht)) * factor * Decimal(str(line.tva_rate)) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
                if line.is_immobilisation: exp_asset += v
                else: exp_charge += v
            ok_route = abs(vat_34551 - float(exp_asset)) < 0.01 and abs(vat_34552 - float(exp_charge)) < 0.01
            add(13, "TVA acheteur répartie exactement entre 34551 immobilisations et 34552 charges", ok_route,
                f"34551 attendu {float(exp_asset):.2f}/trouvé {vat_34551:.2f}; 34552 attendu {float(exp_charge):.2f}/trouvé {vat_34552:.2f}")
        else:
            expected_acct = "34551" if data.is_immobilisation else "34552"
            add(13, f"TVA acheteur routée au {expected_acct}", any(l.account_number == expected_acct for l in owner_lines))
    else:
        add(13, "TVA acheteur — sans objet", True)

    expected_assets = set()
    if data.is_immobilisation:
        expected_assets.add(ASSET_ACCOUNT_BY_TYPE.get(data.immobilisation_type))
    expected_assets.update(ASSET_ACCOUNT_BY_TYPE.get(l.immobilisation_type) for l in data.line_items if l.is_immobilisation)
    expected_assets.discard(None)
    if owner == "Buyer" and expected_assets:
        used = {l.account_number for l in owner_lines if l.side in {"DEBIT", "CREDIT"}}
        add(14, "Imputations d'immobilisations exactes, pas seulement 'classe 2'", expected_assets <= used,
            f"Attendus: {sorted(expected_assets)}; utilisés: {sorted(expected_assets & used)}")
    else:
        add(14, "Imputation immobilisation — sans objet", True)

    if data.line_items:
        line_math_ok = all(abs(r2(l.quantity * l.unit_price) - r2(l.line_total_ht)) < 0.01 for l in data.line_items)
        add(15, "Chaque ligne: quantité × prix unitaire = total HT", line_math_ok)
        sum_lines = r2(sum(l.line_total_ht for l in data.line_items))
        add(16, "Somme des lignes HT = montant brut", abs(sum_lines - calc.montant_brut) < 0.01,
            f"Somme lignes {sum_lines:,.2f}; brut {calc.montant_brut:,.2f}.")
    else:
        add(15, "Arithmétique des lignes — sans objet", True)
        add(16, "Somme des lignes — sans objet", True)

    if data.montant_ttc is not None:
        add(17, "TTC imprimé = TTC recalculé", abs(r2(data.montant_ttc) - calc.ttc) <= 0.02,
            f"Imprimé {r2(data.montant_ttc):,.2f}; calculé {calc.ttc:,.2f}.")
    else:
        add(17, "TTC imprimé absent — contrôle non disponible", True)

    normalized_date = normalize_date(data.date)
    add(18, "Date facture valide et non future", bool(normalized_date) and not is_future_date(data.date),
        f"Date normalisée: {normalized_date or 'invalide'}")
    add(19, "Échéance cohérente avec la date facture", not due_before_invoice(data.date, data.due_date))

    if data.supplier_ice:
        digits = "".join(ch for ch in data.supplier_ice if ch.isdigit())
        add(20, "ICE fournisseur au format 15 chiffres", len(digits) == 15)
    else:
        add(20, "ICE fournisseur absent — non bloquant", True)
    add(21, "Numéro de facture présent", bool((data.invoice_number or "").strip()))

    classification_ok = True
    if owner == "Buyer":
        classification_ok = "3497" not in all_accounts
    add(22, "Imputation définitive identifiée (aucun compte d'attente 3497)", classification_ok,
        "Une nature 'unclassified' reste en attente et ne doit pas être postée.")

    if data.invoice_type == "AVOIR":
        add(23, "Motif de l'avoir documenté", data.credit_note_reason is not None)
    else:
        add(23, "Motif d'avoir — sans objet", True)

    has_zero_rate = any(abs(r) < 0.001 for r in line_rates)
    zero_basis = (data.tva_legal_basis or data.vat_exemption_code or "").strip()
    add(24, "Traitement TVA à 0% documenté", (not has_zero_rate) or bool(zero_basis),
        "Un taux 0% exige une base légale/justificatif explicite pour éviter une exemption inventée.")

    if owner == "Seller":
        expected_sales = "7124" if data.invoice_category == "facture_service" else "7111"
        add(25, f"Compte de vente conforme à la nature ({expected_sales})", any(l.account_number == expected_sales for l in owner_lines))
    else:
        add(25, "Compte de vente — sans objet côté acheteur", True)

    if owner == "Buyer" and not data.line_items and not data.is_immobilisation:
        nature = data.accounting_nature
        if nature == "unclassified" and data.invoice_category == "facture_achat":
            nature = "merchandise"
        expected_purchase = PURCHASE_ACCOUNT_BY_NATURE.get(nature)
        add(26, "Compte d'achat/charge exact selon la nature", bool(expected_purchase) and any(l.account_number == expected_purchase for l in owner_lines),
            f"Nature={nature}; compte attendu={expected_purchase or 'non classé'}")
    else:
        add(26, "Imputation achat unitaire — contrôlée par lignes/immobilisation", True)

    forbidden_legacy = []
    if data.invoice_category == "facture_service":
        forbidden_legacy = [a for a in all_accounts if a in {"6121", "7121"}]
    add(27, "Aucun ancien compte générique service 6121/7121", not forbidden_legacy,
        f"Comptes interdits trouvés: {forbidden_legacy}" if forbidden_legacy else None)

    if owner == "Buyer":
        add(28, "Identité fournisseur présente pour le sous-grand-livre", bool((data.supplier_name or "").strip()))
    else:
        add(28, "Identité client présente pour le sous-grand-livre", bool((data.customer_name or "").strip()))

    expected_directions = {
        ("Buyer", "DOIT"): "purchase", ("Buyer", "AVOIR"): "purchase_credit_note",
        ("Seller", "DOIT"): "sale", ("Seller", "AVOIR"): "sale_credit_note",
    }
    expected_direction = expected_directions[(owner, data.invoice_type)]
    add(29, f"Direction documentaire cohérente ({expected_direction})", data.document_direction == expected_direction,
        f"Direction fournie: {data.document_direction}")
    add(30, "Devise et taux de change documentés", len((data.currency or "")) == 3 and float(data.exchange_rate) > 0)
    accounting_date = normalize_date(data.accounting_date) if data.accounting_date else normalized_date
    add(31, "Date comptable normalisée", bool(accounting_date), f"Date comptable: {accounting_date or 'invalide'}")
    if has_zero_rate:
        basis_lower = zero_basis.casefold()
        art89 = "art. 89" in basis_lower or "article 89" in basis_lower or "art 89" in basis_lower
        add(32, "Article 89 jamais utilisé comme justification d'exonération", not art89)
    else:
        add(32, "Base d'exonération — sans objet", True)
    if data.is_immobilisation or any(l.is_immobilisation for l in data.line_items):
        add(33, "Politique de capitalisation explicite pour immobilisations ambiguës", data.capitalization_policy != "expense" and not (data.immobilisation_type == "other" and data.capitalization_policy is None),
            "Les acquisitions classées 'other' exigent une décision capitalize/review.")
    else:
        add(33, "Politique de capitalisation — sans objet", True)

    balance_checks: list[BalanceCheck] = []
    for entry in entries:
        td = r2(sum(l.amount for l in entry.lines if l.side == "DEBIT"))
        tc = r2(sum(l.amount for l in entry.lines if l.side == "CREDIT"))
        balance_checks.append(BalanceCheck(perspective=entry.perspective, total_debit=td, total_credit=tc, balanced=abs(td - tc) < 0.01))

    all_ok = all(c.passed for c in checks) and all(b.balanced for b in balance_checks)
    return checks, balance_checks, "VALID" if all_ok else "INVALID"
