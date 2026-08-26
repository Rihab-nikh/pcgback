"""Request bodies for the v2 API. Responses are built from repository dicts."""
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class RegisterFirmRequest(BaseModel):
    firm_name: str = Field(..., min_length=2, max_length=120)
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    accounting_software: str | None = Field(None, max_length=60)
    country: str = Field("MA", min_length=2, max_length=2)     # ISO 3166-1 alpha-2
    currency: str = Field("MAD", min_length=3, max_length=3)   # ISO 4217
    logo: str | None = Field(None, max_length=500_000)         # data URL (base64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


FirmRole = Literal["business_admin", "firm_admin", "accountant", "reviewer", "employee"]


class CreateAccountantRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    role: FirmRole = "accountant"
    department: str | None = Field(None, max_length=80)
    phone: str | None = Field(None, max_length=30)
    client_ids: list[str] = []  # clients to assign to the new accountant (optional)


class BulkInviteRequest(BaseModel):
    users: list[CreateAccountantRequest] = Field(..., min_length=1, max_length=100)


class UpdateUserRequest(BaseModel):
    full_name: str | None = Field(None, min_length=2, max_length=120)
    role: FirmRole | None = None
    department: str | None = None
    is_active: bool | None = None


class PermissionToggleRequest(BaseModel):
    role: FirmRole
    permission: str = Field(..., min_length=3, max_length=60)
    allowed: bool


class ExpenseCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: str | None = Field(None, max_length=2000)
    category: str | None = Field(None, max_length=60)
    amount: float = Field(..., gt=0)
    currency: str = Field("MAD", min_length=3, max_length=3)
    expense_date: str | None = None  # ISO date
    submit: bool = False             # True = file directly as open (skip draft)


class ExpenseUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=200)
    description: str | None = None
    category: str | None = None
    amount: float | None = Field(None, gt=0)
    expense_date: str | None = None


class AssignClientsRequest(BaseModel):
    client_ids: list[str]  # full replacement: clients not listed are unassigned


class ClientCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    ice: str | None = None
    if_number: str | None = None
    address: str | None = None
    assigned_to: str | None = None


class ClientUpdateRequest(BaseModel):
    name: str | None = None
    ice: str | None = None
    if_number: str | None = None
    address: str | None = None
    assigned_to: str | None = None
    is_archived: bool | None = None


class ReviewRequest(BaseModel):
    action: Literal["approve", "reject"]
    note: str | None = None
    override_invalid: bool = False
    post_now: bool = False
    posting_date: str | None = None
    reviewer_confidence: float | None = Field(None, ge=0, le=1)


class DocumentUpdateRequest(BaseModel):
    category: str | None = None
    tags: list[str] | None = None
    client_id: str | None = None  # empty string = detach from client


class BankAccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    bank_name: str | None = None
    rib: str | None = Field(None, max_length=30)
    currency: str = "MAD"
    pcg_account: str = "5141"
    client_id: str | None = None


class BankAccountUpdateRequest(BaseModel):
    name: str | None = None
    bank_name: str | None = None
    rib: str | None = None
    pcg_account: str | None = None
    is_archived: bool | None = None


class MatchRequest(BaseModel):
    invoice_id: str
    amount: float | None = Field(None, gt=0)
