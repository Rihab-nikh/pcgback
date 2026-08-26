"""Vision extraction: invoice image -> ExtractedInvoiceData.

The system prompt lives in prompts/invoice_extraction.md so it can be
reviewed, diffed, and versioned independently of the code.
"""
from functools import lru_cache
from pathlib import Path

from app.ai.client import openai_client
from app.core.config import EXTRACTION_MODEL
from app.core.logging import logger
from app.models.invoice import ExtractedInvoiceData

_PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


async def extract_invoice_data(
    invoice_image_b64: str,
    perspective: str | None = None,
    exercise_context: str | None = None,
) -> ExtractedInvoiceData:
    system_prompt = load_prompt("invoice_extraction")

    perspective_note = f"Perspective: {perspective}." if perspective else ""
    context_note = f"Exercise context: {exercise_context}." if exercise_context else ""
    user_text = " ".join(filter(None, [
        "Extract all invoice data from this image.",
        perspective_note,
        context_note,
    ]))

    if invoice_image_b64.startswith("data:image"):
        data_uri = invoice_image_b64
    else:
        data_uri = f"data:image/jpeg;base64,{invoice_image_b64}"

    try:
        response = await openai_client.beta.chat.completions.parse(
            model=EXTRACTION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]},
            ],
            response_format=ExtractedInvoiceData,
        )
    except Exception as e:
        logger.error(f"OpenAI API Error: {type(e).__name__}: {str(e)}")
        raise

    extracted = response.choices[0].message.parsed

    # Post-parse validation: catch the classic "TTC read as HT" misread
    if extracted.tva_pct > 0 and extracted.montant_ttc is not None:
        if extracted.montant_brut >= extracted.montant_ttc:
            raise ValueError(
                f"Invariant violated: montant_brut ({extracted.montant_brut}) "
                f">= montant_ttc ({extracted.montant_ttc}). "
                f"Model likely used TTC as HT."
            )

    expected_ttc = round(
        extracted.montant_brut
        * (1 - extracted.rabais_pct / 100)
        * (1 - extracted.remise_pct / 100)
        * (1 - extracted.ristourne_pct / 100)
        * (1 - extracted.escompte_pct / 100)
        * (1 + extracted.tva_pct / 100),
        2
    )
    if extracted.montant_ttc and abs(extracted.montant_ttc - expected_ttc) > 1.0:
        extracted.assumptions.append(
            f"Warning: montant_ttc ({extracted.montant_ttc}) differs from "
            f"computed TTC ({expected_ttc}). Possible rounding or extraction issue."
        )

    return extracted
