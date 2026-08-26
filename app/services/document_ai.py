"""AI document analysis for the GED: classification + full-text OCR.

Reuses the shared OpenAI async client and the data-URI convention of the
invoice extraction service (app/ai/extraction.py). One call per document,
best-effort: callers must treat failures as ocr_status='failed', never as
an upload failure.
"""
from typing import Literal

from pydantic import BaseModel

from app.ai.client import openai_client
from app.core.config import EXTRACTION_MODEL
from app.core.logging import logger

CATEGORIES = ["facture", "recu", "releve_bancaire", "contrat", "bon_commande",
              "bon_livraison", "paie", "fiscal", "divers"]


class DocumentAnalysis(BaseModel):
    category: Literal["facture", "recu", "releve_bancaire", "contrat", "bon_commande",
                      "bon_livraison", "paie", "fiscal", "divers"]
    text: str  # full readable text of the document (searchable OCR)


_SYSTEM = """Tu es l'assistant de gestion documentaire d'un cabinet comptable marocain.
Analyse le document fourni et retourne :
1. category — exactement une catégorie parmi : facture, recu, releve_bancaire, contrat,
   bon_commande, bon_livraison, paie, fiscal, divers.
2. text — la transcription complète du texte lisible (OCR), en préservant exactement
   les montants, dates, numéros et identifiants (ICE, IF, RIB…). Si le document est
   illisible, retourne un texte vide."""


async def analyze_document(data_uri: str) -> DocumentAnalysis:
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=EXTRACTION_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "Analyse ce document."},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]},
            ],
            response_format=DocumentAnalysis,
        )
    except Exception as e:
        logger.error(f"Document analysis error: {type(e).__name__}: {e}")
        raise
    return response.choices[0].message.parsed
