"""Natural-language queries — "Show me all invoices from Dell above 20,000 MAD in March".

Architecture chosen for zero hallucination: the LLM's ONLY job is translating
the question into a constrained FilterSpec (structured output). The spec is
executed against the real tenant-scoped repository; results come from SQL,
never from the model. If the model can't map the question, we say so.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import accountant_scope, firm_member
from app.core.config import EXTRACTION_MODEL
from app.repositories import clients as clients_repo
from app.repositories import invoices as inv_repo

router = APIRouter(prefix="/assistant", tags=["assistant"])


class FilterSpec(BaseModel):
    """What the LLM is allowed to produce — nothing else."""
    supplier: str | None = Field(None, description="Supplier name fragment to search")
    client_name: str | None = Field(None, description="Client company name fragment")
    status: str | None = Field(None, description="One of: processing, needs_review, approved, rejected, failed")
    min_ttc: float | None = Field(None, description="Minimum TTC amount in MAD")
    max_ttc: float | None = Field(None, description="Maximum TTC amount in MAD")
    date_from: str | None = Field(None, description="ISO date lower bound, e.g. 2026-03-01")
    date_to: str | None = Field(None, description="ISO date upper bound, e.g. 2026-03-31")
    answerable: bool = Field(..., description="False if the question cannot be answered by filtering invoices")
    reason_if_not: str | None = Field(None, description="If not answerable, a one-sentence explanation in French")


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


async def _translate(question: str) -> FilterSpec:
    from app.ai.client import openai_client
    resp = await openai_client.beta.chat.completions.parse(
        model=EXTRACTION_MODEL,
        messages=[
            {"role": "system", "content":
                "Translate the user's accounting question (French or English) into an invoice "
                "FilterSpec. Today's year is 2026 — a bare month like 'mars' means 2026-03. "
                "Only set fields the question explicitly implies. If the question is not an "
                "invoice-filtering question (e.g. asks for advice, or about data that isn't "
                "invoices), set answerable=false with a short French reason."},
            {"role": "user", "content": question},
        ],
        response_format=FilterSpec,
    )
    return resp.choices[0].message.parsed


@router.post("/query")
async def nl_query(body: QueryRequest, user: dict = Depends(firm_member)):
    try:
        spec = await _translate(body.question)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Traduction de la question impossible: {e}")

    if not spec.answerable:
        return {"answerable": False,
                "message": spec.reason_if_not or
                "Je ne peux répondre qu'à des questions filtrant les factures (fournisseur, client, montant, date, statut).",
                "filters": None, "items": []}

    client_id = None
    if spec.client_name:
        matches = clients_repo.list_clients(user["firm_id"], assigned_to=accountant_scope(user),
                                            q=spec.client_name)
        if not matches:
            return {"answerable": True, "filters": spec.model_dump(),
                    "message": f"Aucun client ne correspond à « {spec.client_name} ».", "items": []}
        client_id = matches[0]["id"]

    result = inv_repo.list_invoices(
        user["firm_id"], client_id=client_id, status=spec.status, q=spec.supplier,
        date_from=spec.date_from, date_to=spec.date_to,
        accountant_id=accountant_scope(user), limit=100)
    items = [i for i in result["items"]
             if (spec.min_ttc is None or (i["ttc"] or 0) >= spec.min_ttc)
             and (spec.max_ttc is None or (i["ttc"] or 0) <= spec.max_ttc)]

    total = round(sum(i["ttc"] or 0 for i in items), 2)
    return {"answerable": True, "filters": spec.model_dump(),
            "message": f"{len(items)} facture(s) trouvée(s) — total TTC {total:,.2f} MAD.",
            "items": items}
