"""The core processing pipeline, importable by any router:
extract -> compute -> journal -> validate -> report."""
from app.ai.extraction import extract_invoice_data
from app.core.logging import logger
from app.models.invoice import InvoiceResponse
from app.services.accounting import compute_invoice, determine_payment_status, generate_journal_entries
from app.services.reporting import build_summary_table, format_report
from app.services.validation import run_validation


async def process_invoice_pipeline(image_data_uri: str) -> InvoiceResponse:
    """Full pipeline for one invoice image."""
    logger.info("STEPLOG START extract")
    data = await extract_invoice_data(image_data_uri)
    logger.info("STEPLOG END extract", invoice_type=data.invoice_type, montant_brut=data.montant_brut)
    return await pipeline_from_data(data)


async def pipeline_from_data(data) -> InvoiceResponse:
    """Compute -> journal -> validate -> report from already-extracted (or
    human-corrected) data. Used by the inline-editing endpoint."""
    logger.info("STEPLOG START compute")
    calc = compute_invoice(data)
    logger.info("STEPLOG END compute", net_commercial=calc.net_commercial, ttc=calc.ttc)

    logger.info("STEPLOG START journal")
    entries = await generate_journal_entries(data, calc)
    summary = build_summary_table(calc)
    checks, balance_checks, verdict = run_validation(data, calc, entries)
    logger.info("STEPLOG END journal", verdict=verdict)

    payment_status = determine_payment_status(data, calc)
    report = format_report(data, calc, summary, entries, checks, balance_checks, verdict, payment_status)

    return InvoiceResponse(
        step1_identification=data, step2_calculations=calc,
        step3_summary_table=summary, step4_journal_entries=entries,
        payment_status=payment_status,
        validation_checks=checks, balance_checks=balance_checks,
        verdict=verdict, report=report,
    )
