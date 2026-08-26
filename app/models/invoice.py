"""Pydantic models: extraction schema, journal structures, API request/response."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.dates import normalize_date


class InvoiceRequest(BaseModel):
    invoice_image: str = Field(
        ...,
        description="Invoice image to process.",
        json_schema_extra={"contentMediaType": "image/*"},
    )


class FieldConfidence(BaseModel):
    """Per-field extraction confidence, 0.0-1.0."""
    field: str = Field(..., description="Field name from the extraction schema, e.g. 'montant_brut', 'tva_pct', 'supplier_name'")
    confidence: float = Field(..., ge=0, le=1, description="How certain the extraction of this field is (1.0 = printed clearly, 0.5 = inferred/ambiguous)")


class FieldRegion(BaseModel):
    """Normalized bounding box locating a field on the source document."""
    field: str = Field(..., description="Field name this region corresponds to")
    page: int = Field(default=1, description="1-based page number")
    x: float = Field(..., ge=0, le=1, description="Left edge, fraction of page width")
    y: float = Field(..., ge=0, le=1, description="Top edge, fraction of page height")
    width: float = Field(..., ge=0, le=1)
    height: float = Field(..., ge=0, le=1)


AccountingNature = Literal[
    "merchandise", "raw_material", "consumable_supplies", "nonstocked_supplies",
    "rent", "maintenance", "insurance", "professional_fees", "studies_documentation",
    "transport", "advertising", "telecom", "banking_services", "royalties",
    "other_external_service", "unclassified"
]


class InvoiceLineItem(BaseModel):
    """Represents a single line item on a multi-line invoice."""
    description: str = Field(..., description="Line item description (e.g., 'Ordinateur portable')")
    quantity: float = Field(default=1.0, description="Quantity")
    unit_price: float = Field(..., description="Unit price HT (pre-tax)")
    line_total_ht: float = Field(..., description="Line total HT (quantity x unit_price)")
    tva_rate: float = Field(default=20.0, description="TVA rate for this line (0, 7, 10, 14, or 20). Allows mixed rates per invoice")
    is_immobilisation: bool = Field(
        default=False,
        description="True if this line item is a fixed asset"
    )
    immobilisation_type: Literal["equipment", "building", "vehicle", "it", "furniture", "installation", "other", None] = Field(
        default=None,
        description="Type of fixed asset if is_immobilisation=True"
    )
    accounting_nature: AccountingNature = Field(
        default="unclassified",
        description="Deterministic accounting nature used to select the purchase/expense account; never infer every non-asset line as a service.",
    )
    line_notes: str | None = Field(None, description="Notes specific to this line")


class ExtractedInvoiceData(BaseModel):
    invoice_type: Literal["DOIT", "AVOIR"] = Field(..., description="DOIT or AVOIR")
    invoice_category: Literal["facture_vente", "facture_achat", "facture_service", "avoir", "other"] = Field(
        ..., description="Invoice category"
    )
    date: str | None = Field(None, description="Normalized invoice date (YYYY-MM-DD) or None")
    invoice_number: str | None = Field(None, description="Invoice number")
    supplier_name: str | None = Field(None, description="Supplier/seller company name as printed on the invoice")
    supplier_ice: str | None = Field(None, description="Supplier ICE (Identifiant Commun de l'Entreprise, 15 digits) if printed")
    customer_name: str | None = Field(None, description="Customer/buyer company name as printed on the invoice")
    customer_ice: str | None = Field(None, description="Customer ICE if printed")
    document_direction: Literal["purchase", "sale", "purchase_credit_note", "sale_credit_note"] = Field(
        default="purchase", description="Direction from the book owner's perspective; determines the only legal ledger perspective."
    )
    transaction_nature: str | None = Field(None, description="Business/tax nature used by versioned rules")
    tax_treatment_code: str | None = Field(None, description="Versioned VAT/tax treatment code")
    vat_exemption_code: str | None = Field(None, description="Explicit exemption/non-taxation code when VAT is 0%")
    currency: str = Field(default="MAD", min_length=3, max_length=3)
    exchange_rate: float = Field(default=1.0, gt=0, description="Rate to MAD / book currency for foreign-currency documents")
    accounting_date: str | None = Field(None, description="Accounting date; defaults to invoice date and is normalized")
    journal_code: str | None = Field(None, description="Firm-approved journal code; defaults to AC/VE from direction")
    auxiliary_account: str | None = Field(None, description="Optional partner auxiliary account")
    montant_brut: float = Field(..., description="Gross amount (Montant brut)")
    montant_ttc: float | None = Field(None, description="Total amount with tax (Montant TTC)")
    rabais_pct: float = Field(default=0.0, description="Rabais percentage (0-100)")
    remise_pct: float = Field(default=0.0, description="Remise percentage (0-100)")
    ristourne_pct: float = Field(default=0.0, description="Ristourne percentage (0-100)")
    escompte_pct: float = Field(default=0.0, description="Escompte percentage (0-100)")
    tva_pct: float = Field(default=20.0, description="TVA percentage (0, 7, 10, 14, or 20)")
    retenue_a_la_source_pct: float = Field(default=0.0, description="Retenue a la source percentage (e.g. 10.0)")
    droits_de_timbre: float = Field(
        default=0.0,
        description="Stamp duty actually applicable to the invoice in MAD, excluding merely conditional mentions",
    )
    droits_de_timbre_mentionne: float = Field(
        default=0.0,
        description="Stamp duty amount printed on the document, including conditional or informational mentions",
    )
    droits_de_timbre_condition: str | None = Field(
        default=None,
        description="Condition printed next to a stamp-duty amount when it is not automatically applicable",
    )
    net_a_payer_document: float | None = Field(
        default=None,
        description="Final Net à payer/Total à payer explicitly printed on the document; never calculated",
    )
    payment_mode: Literal["banque", "caisse", "none"] = Field(
        default="none",
        description="Payment method printed/expected on the document. It is NOT proof of settlement.",
    )
    accounting_nature: AccountingNature = Field(
        default="unclassified",
        description="Invoice-level purchase/expense nature when line_items are absent.",
    )
    tva_legal_basis: str | None = Field(None, description="Legal basis/support for a non-standard or exempt VAT treatment")
    withholding_type: Literal["cit_iit", "vat_withholding", "other", None] = Field(
        default=None, description="Explicit withholding regime; required when retenue_a_la_source_pct > 0"
    )
    withholding_legal_basis: str | None = Field(None, description="Legal basis for withholding; required when a withholding rate is present")
    withholding_base: Literal["net_financier_ht", "tva_amount", "ttc", None] = Field(
        default=None, description="Explicit withholding base. CIT/IIT defaults to net_financier_ht; VAT withholding uses tva_amount."
    )
    withholding_account_number: str | None = Field(
        default=None, description="Firm-approved liability account for non-standard withholding. CIT/IIT uses 4452 by default."
    )
    payer_entity_type: Literal["individual", "company", "public_body", "unknown"] = "unknown"
    payer_turnover_mad: float | None = Field(None, ge=0, description="Optional payer turnover when a tax rule explicitly depends on it")
    supplier_residency: Literal["resident", "non_resident", "unknown"] = "unknown"
    supplier_entity_type: Literal["individual", "company", "unknown"] = "unknown"
    tax_compliance_certificate: Literal["present", "absent", "unknown"] = "unknown"
    capitalization_policy: Literal["expense", "capitalize", "review", None] = Field(default=None, description="Explicit capitalization decision for ambiguous durable purchases")
    credit_note_reason: Literal["return_goods", "cancel_invoice", "post_invoice_discount", "financial_adjustment", "other", None] = Field(
        default=None, description="Reason for an AVOIR; post-invoice discounts use dedicated RRR accounts."
    )
    raw_notes: str | None = Field(None, description="Any extra notes from the invoice")
    due_date: str | None = Field(None, description="Payment due date if stated")
    is_immobilisation: bool = Field(
        default=False,
        description="True if invoice describes fixed assets/equipment (immobilisations)"
    )
    immobilisation_type: Literal["equipment", "building", "vehicle", "it", "furniture", "installation", "other", None] = Field(
        default=None,
        description="Type of fixed asset if is_immobilisation=True (e.g., equipment, building, vehicle)"
    )
    line_items: list[InvoiceLineItem] = Field(
        default_factory=list,
        description="Multi-line invoice support: individual line items (if empty, treated as single-line)"
    )
    field_confidences: list[FieldConfidence] = Field(
        default_factory=list,
        description="Per-field confidence for every extracted field (0-1). Include one entry per populated field."
    )
    field_regions: list[FieldRegion] = Field(
        default_factory=list,
        description="Normalized bounding boxes (0-1, top-left origin) locating each extracted field on the document. Include one per field visible on the page."
    )
    assumptions: list[str] = Field(default_factory=list, description="Assumptions made during extraction")
    requested_perspective: Literal["both", "seller", "buyer"] = Field(
        default="buyer",
        description="Legal entity whose books are being prepared. 'both' is accepted only for legacy/read-only educational data; production posting selects one owner.",
    )

    @field_validator("date", "due_date", "accounting_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value):
        if value in (None, ""):
            return None
        normalized = normalize_date(str(value))
        # Preserve invalid OCR text so deterministic validation can flag it instead of
        # silently inventing a date.
        return normalized or str(value).strip()


class JournalLine(BaseModel):
    side: Literal["DEBIT", "CREDIT"]
    account_number: str
    account_label: str
    amount: float


class JournalEntry(BaseModel):
    perspective: str
    date: str
    libelle: str
    lines: list[JournalLine]


class ValidationCheck(BaseModel):
    check_id: int
    description: str
    passed: bool
    explanation: str | None = None  # human-readable, computed from the invoice's actual numbers


class BalanceCheck(BaseModel):
    perspective: str
    total_debit: float
    total_credit: float
    balanced: bool


class CalculationBreakdown(BaseModel):
    montant_brut: float
    rabais_pct: float
    rabais_amount: float
    net1: float
    remise_pct: float
    remise_amount: float
    net2: float
    ristourne_pct: float
    ristourne_amount: float
    net_commercial: float
    escompte_pct: float
    escompte_amount: float
    net_financier_ht: float
    tva_pct: float
    tva_amount: float
    ttc: float
    retenue_a_la_source_pct: float
    retenue_a_la_source_amount: float
    droits_de_timbre: float
    net_a_payer: float


class InvoiceSummaryRow(BaseModel):
    element: str
    montant: str


class PaymentStatusInfo(BaseModel):
    status: Literal["UNPAID", "PAID", "OVERDUE"]
    due_date: str
    amount_outstanding: float


class InvoiceResponse(BaseModel):
    step1_identification: ExtractedInvoiceData
    step2_calculations: CalculationBreakdown
    step3_summary_table: list[InvoiceSummaryRow]
    step4_journal_entries: list[JournalEntry]
    payment_status: PaymentStatusInfo
    validation_checks: list[ValidationCheck]
    balance_checks: list[BalanceCheck]
    verdict: Literal["VALID", "INVALID"]
    report: str = Field(
        ...,
        description="Full formatted report.",
        json_schema_extra={"contentMediaType": "text/markdown"},
    )
