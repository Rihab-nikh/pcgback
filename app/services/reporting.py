"""Summary table and markdown report generation."""
from app.models.invoice import (
    BalanceCheck,
    CalculationBreakdown,
    ExtractedInvoiceData,
    InvoiceSummaryRow,
    JournalEntry,
    PaymentStatusInfo,
    ValidationCheck,
)


def build_summary_table(calc: CalculationBreakdown) -> list[InvoiceSummaryRow]:
    rows: list[InvoiceSummaryRow] = []
    rows.append(InvoiceSummaryRow(element="Montant brut", montant=f"{calc.montant_brut:,.2f}"))
    if calc.rabais_pct > 0:
        rows.append(InvoiceSummaryRow(element=f"Rabais {calc.rabais_pct}%", montant=f"(-) {calc.rabais_amount:,.2f}"))
    if calc.remise_pct > 0:
        rows.append(InvoiceSummaryRow(element=f"Remise {calc.remise_pct}%", montant=f"(-) {calc.remise_amount:,.2f}"))
    if calc.ristourne_pct > 0:
        rows.append(InvoiceSummaryRow(element=f"Ristourne {calc.ristourne_pct}%", montant=f"(-) {calc.ristourne_amount:,.2f}"))
    rows.append(InvoiceSummaryRow(element="**Net commercial HT**", montant=f"**{calc.net_commercial:,.2f}**"))
    if calc.escompte_pct > 0:
        rows.append(InvoiceSummaryRow(element=f"Escompte {calc.escompte_pct}%", montant=f"(-) {calc.escompte_amount:,.2f}"))
    rows.append(InvoiceSummaryRow(element="**Net financier HT**", montant=f"**{calc.net_financier_ht:,.2f}**"))
    if calc.tva_pct > 0:
        rows.append(InvoiceSummaryRow(element=f"TVA {calc.tva_pct}%", montant=f"(+) {calc.tva_amount:,.2f}"))
    rows.append(InvoiceSummaryRow(element="**Net à payer TTC**", montant=f"**{calc.ttc:,.2f}**"))
    if calc.retenue_a_la_source_pct > 0:
        rows.append(InvoiceSummaryRow(
            element=f"Retenue à la source {calc.retenue_a_la_source_pct}%",
            montant=f"(-) {calc.retenue_a_la_source_amount:,.2f}",
        ))
    if calc.droits_de_timbre > 0:
        rows.append(InvoiceSummaryRow(
            element="Droits de timbre",
            montant=f"(+) {calc.droits_de_timbre:,.2f}",
        ))
    if calc.retenue_a_la_source_amount > 0 or calc.droits_de_timbre > 0:
        rows.append(InvoiceSummaryRow(
            element="**Net effectivement encaissé**",
            montant=f"**{calc.net_a_payer:,.2f}**",
        ))
    return rows


def format_report(data: ExtractedInvoiceData, calc: CalculationBreakdown, summary: list[InvoiceSummaryRow],
                  entries: list[JournalEntry], checks: list[ValidationCheck],
                  balances: list[BalanceCheck], verdict: str,
                  payment_status: PaymentStatusInfo | None = None) -> str:
    lines: list[str] = ["# Resultat — Moteur Comptable PCG Marocain\n"]
    lines.append("## Etape 1 — Identification")
    lines.append(f"- **Type:** {data.invoice_type}")
    lines.append(f"- **Categorie:** {data.invoice_category}")
    lines.append(f"- **Date:** {data.date or 'Non precisee'}")
    lines.append(f"- **N Facture:** {data.invoice_number or 'Non precise'}")
    lines.append(f"- **Mode paiement:** {data.payment_mode}\n")
    lines.append("## Etape 2 — Calculs")
    lines.append(f"Montant brut = {calc.montant_brut:,.2f} MAD")
    if calc.rabais_pct > 0:
        lines.append(f"Rabais {calc.rabais_pct}% = {calc.rabais_amount:,.2f} -> Net1 = {calc.net1:,.2f}")
    if calc.remise_pct > 0:
        lines.append(f"Remise {calc.remise_pct}% = {calc.remise_amount:,.2f} -> Net2 = {calc.net2:,.2f}")
    if calc.ristourne_pct > 0:
        lines.append(f"Ristourne {calc.ristourne_pct}% = {calc.ristourne_amount:,.2f} -> Net commercial = {calc.net_commercial:,.2f}")
    else:
        lines.append(f"Net commercial HT = {calc.net_commercial:,.2f}")
    if calc.escompte_pct > 0:
        lines.append(f"Escompte {calc.escompte_pct}% = {calc.escompte_amount:,.2f}")
    lines.append(f"Net financier HT = {calc.net_financier_ht:,.2f}")
    if calc.tva_pct > 0:
        lines.append(f"TVA {calc.tva_pct}% = {calc.tva_amount:,.2f}")
    lines.append(f"**TTC = {calc.ttc:,.2f} MAD**\n")
    lines.append("## Etape 3 — Tableau recapitulatif")
    lines.append("| Element | Montant (MAD) |")
    lines.append("|---------|---------------|")
    for row in summary:
        lines.append(f"| {row.element} | {row.montant} |")
    lines.append("")
    lines.append("## Etape 4 — Ecritures comptables")
    for entry in entries:
        lines.append(f"\n### {entry.perspective}")
        lines.append(f"**Date:** {entry.date}")
        lines.append(f"**Libelle:** {entry.libelle}\n")
        lines.append("| | Compte | Libelle | Montant (MAD) |")
        lines.append("|--|--------|---------|---------------|")
        for line in entry.lines:
            lines.append(f"| {line.side} | {line.account_number} | {line.account_label} | {line.amount:,.2f} |")
    lines.append("\n## Auto-Validation")
    for c in checks:
        mark = "V" if c.passed else "X"
        lines.append(f"{c.check_id}. {c.description} [{mark}]")
    lines.append("")
    for b in balances:
        status = "BALANCED" if b.balanced else "NOT BALANCED"
        lines.append(f"**{b.perspective}:** Debit = {b.total_debit:,.2f} | Credit = {b.total_credit:,.2f} -> {status}")
    if payment_status:
        lines.append("\n## Payment Status")
        lines.append(f"Payment Status     : {payment_status.status}")
        lines.append(f"Due Date           : {payment_status.due_date}")
        lines.append(f"Amount Outstanding : {payment_status.amount_outstanding:,.2f} MAD")
    if data.assumptions:
        lines.append("\n## Assumptions")
        for a in data.assumptions:
            lines.append(f"- {a}")
    lines.append(f"\n### Verdict: **{verdict}**")
    return "\n".join(lines)
