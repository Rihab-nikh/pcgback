-- PCG Maroc AI — v2 schema (SQLite; column types map 1:1 to Postgres)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS firms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'trial',      -- trial | pro | enterprise
    accounting_software TEXT,                       -- Sage | Cegid | Quadratus | ...
    country     TEXT NOT NULL DEFAULT 'MA',
    currency    TEXT NOT NULL DEFAULT 'MAD',
    logo        TEXT,                               -- data URL (base64), optional
    settings    TEXT NOT NULL DEFAULT '{}',         -- JSON: accounting defaults, numbering…
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    firm_id       TEXT REFERENCES firms(id),        -- NULL for super_admin
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    role          TEXT NOT NULL,                    -- super_admin | business_admin | firm_admin | accountant | reviewer | employee
    department    TEXT,
    phone         TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    name        TEXT NOT NULL,
    ice         TEXT,
    if_number   TEXT,
    address     TEXT,
    assigned_to TEXT REFERENCES users(id),          -- accountant
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clients_firm ON clients(firm_id);

CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    firm_id         TEXT NOT NULL REFERENCES firms(id),
    client_id       TEXT NOT NULL REFERENCES clients(id),
    uploaded_by     TEXT NOT NULL REFERENCES users(id),
    filename        TEXT NOT NULL,
    file_path       TEXT,
    status          TEXT NOT NULL,                  -- processing | needs_review | approved | rejected | failed
    verdict         TEXT,                           -- VALID | INVALID
    confidence      REAL,                           -- deprecated compatibility score
    extraction_confidence REAL,                    -- OCR/AI field confidence (0..1)
    validation_pass_rate REAL,                     -- deterministic checks passed / total
    accounting_rule_confidence REAL,                -- confidence in deterministic classification/rule match
    tax_risk_level  TEXT,                            -- low | medium | high | critical
    reviewer_confidence REAL,                        -- optional reviewer-confirmed confidence
    invoice_number  TEXT,
    supplier_name   TEXT,
    supplier_ice    TEXT,
    customer_name   TEXT,
    customer_ice    TEXT,
    invoice_date    TEXT,
    source_hash     TEXT,
    currency        TEXT NOT NULL DEFAULT 'MAD',
    exchange_rate   TEXT NOT NULL DEFAULT '1',
    document_direction TEXT NOT NULL DEFAULT 'purchase', -- purchase | sale | purchase_credit_note | sale_credit_note
    ttc             REAL,                           -- compatibility/display only
    ttc_cents       INTEGER,                        -- authoritative exact amount
    net_a_payer     REAL,                           -- compatibility/display only
    net_a_payer_cents INTEGER,                      -- authoritative exact amount
    is_duplicate_of TEXT REFERENCES invoices(id),
    model           TEXT,
    duration_ms     INTEGER,
    response_json   TEXT,                           -- full InvoiceResponse
    error           TEXT,
    reviewed_by     TEXT REFERENCES users(id),
    reviewed_at     TEXT,
    validation_override_note TEXT,                 -- required for explicit INVALID override
    posting_status  TEXT NOT NULL DEFAULT 'unposted', -- unposted | posted | reversed
    posting_date    TEXT,                          -- normalized accounting/posting date
    posted_by       TEXT REFERENCES users(id),
    posted_at       TEXT,
    is_archived     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_firm ON invoices(firm_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_invoices_dup ON invoices(firm_id, supplier_name, invoice_number);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    user_id    TEXT REFERENCES users(id),           -- NULL = whole firm
    kind       TEXT NOT NULL,                       -- invoice_processed | validation_failed | duplicate_detected | export_ready
    message    TEXT NOT NULL,
    invoice_id TEXT REFERENCES invoices(id),
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(firm_id, user_id, is_read);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT,
    user_id     TEXT,
    action      TEXT NOT NULL,                      -- login | invoice.approve | client.create ...
    entity_type TEXT,
    entity_id   TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_firm ON audit_logs(firm_id, created_at);

-- v3: intelligence layer
CREATE TABLE IF NOT EXISTS insights (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    kind       TEXT NOT NULL,      -- duplicate | vat_deviation | classification_drift | client_mismatch | ice_change | preference_conflict
    severity   TEXT NOT NULL,      -- info | warning
    message    TEXT NOT NULL,
    dismissed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insights_invoice ON insights(invoice_id);
CREATE INDEX IF NOT EXISTS idx_insights_firm ON insights(firm_id, dismissed);

-- Learning from corrections: firm-specific supplier priors
CREATE TABLE IF NOT EXISTS supplier_priors (
    firm_id            TEXT NOT NULL REFERENCES firms(id),
    supplier_norm      TEXT NOT NULL,
    invoice_category   TEXT,
    is_immobilisation  INTEGER,
    immobilisation_type TEXT,
    tva_pct            REAL,
    supplier_ice       TEXT,
    confirmations      INTEGER NOT NULL DEFAULT 1,   -- human corrections/approvals backing this prior
    locked             INTEGER NOT NULL DEFAULT 0,   -- 1 = firm policy, AI must not override
    rule_source        TEXT NOT NULL DEFAULT 'ai_learned', -- ai_learned | firm_policy | manual_override
    payment_account    TEXT,                          -- default PCG payment account (5141, 5161…)
    rule_description   TEXT,                          -- default line description for this supplier
    auto_publish       INTEGER NOT NULL DEFAULT 0,    -- 1 = VALID invoices auto-approved
    extract_line_items INTEGER NOT NULL DEFAULT 0,    -- 1 = always extract line items
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (firm_id, supplier_norm)
);

-- Fine-grained invoice edit history (complements audit_logs)
CREATE TABLE IF NOT EXISTS invoice_edits (
    id               TEXT PRIMARY KEY,
    invoice_id       TEXT NOT NULL REFERENCES invoices(id),
    firm_id          TEXT NOT NULL REFERENCES firms(id),
    user_id          TEXT NOT NULL REFERENCES users(id),
    edit_session_id  TEXT NOT NULL,   -- groups fields saved in the same action
    field            TEXT NOT NULL,
    old_value        TEXT,
    new_value        TEXT,
    comment          TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoice_edits ON invoice_edits(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_edits_firm ON invoice_edits(firm_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invoice_edits_session ON invoice_edits(edit_session_id);

-- GED: multi-category document management. Invoices stay in `invoices`
-- (their AI pipeline is unchanged); every other document lives here.
CREATE TABLE IF NOT EXISTS documents (
    id                TEXT PRIMARY KEY,
    firm_id           TEXT NOT NULL REFERENCES firms(id),
    client_id         TEXT REFERENCES clients(id),      -- NULL = firm-level document
    invoice_id        TEXT REFERENCES invoices(id),     -- set when routed through the invoice pipeline
    category          TEXT NOT NULL DEFAULT 'divers',   -- facture | recu | releve_bancaire | contrat | bon_commande | bon_livraison | paie | fiscal | divers
    tags              TEXT NOT NULL DEFAULT '[]',       -- JSON array of strings
    filename          TEXT NOT NULL,
    mime_type         TEXT,
    size_bytes        INTEGER,
    uploaded_by       TEXT NOT NULL REFERENCES users(id),
    ocr_status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    ai_classification TEXT,                             -- model-suggested category
    searchable_text   TEXT,                             -- OCR text, searched with LIKE
    retention_until   TEXT,                             -- legal retention deadline (10 years, Code de commerce)
    is_archived       INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    parent_id         TEXT REFERENCES documents(id),    -- previous version of this document
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_firm ON documents(firm_id, created_at);
CREATE INDEX IF NOT EXISTS idx_documents_client ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(firm_id, category);

-- Treasury: bank accounts, imported statements, transactions, reconciliation
CREATE TABLE IF NOT EXISTS bank_accounts (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    client_id   TEXT REFERENCES clients(id),        -- NULL = the firm's own account
    name        TEXT NOT NULL,
    bank_name   TEXT,
    rib         TEXT,                               -- 24-digit Moroccan RIB
    currency    TEXT NOT NULL DEFAULT 'MAD',
    pcg_account TEXT NOT NULL DEFAULT '5141',       -- Banques
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_firm ON bank_accounts(firm_id);

CREATE TABLE IF NOT EXISTS bank_statements (
    id                TEXT PRIMARY KEY,
    firm_id           TEXT NOT NULL REFERENCES firms(id),
    bank_account_id   TEXT NOT NULL REFERENCES bank_accounts(id),
    filename          TEXT NOT NULL,
    format            TEXT NOT NULL,                -- csv | camt053 | mt940
    imported_by       TEXT NOT NULL REFERENCES users(id),
    transaction_count INTEGER NOT NULL DEFAULT 0,
    opening_balance_cents INTEGER,
    closing_balance_cents INTEGER,
    debit_total_cents INTEGER NOT NULL DEFAULT 0,
    credit_total_cents INTEGER NOT NULL DEFAULT 0,
    control_difference_cents INTEGER,
    statement_hash TEXT,
    period_start TEXT,
    period_end TEXT,
    duplicate_count   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_statements_firm ON bank_statements(firm_id, created_at);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id                 TEXT PRIMARY KEY,
    firm_id            TEXT NOT NULL REFERENCES firms(id),
    bank_account_id    TEXT NOT NULL REFERENCES bank_accounts(id),
    statement_id       TEXT REFERENCES bank_statements(id),
    date               TEXT NOT NULL,
    value_date         TEXT,
    label              TEXT NOT NULL,
    reference          TEXT,
    amount             REAL NOT NULL,               -- compatibility/display
    amount_cents       INTEGER,                     -- authoritative signed amount in centimes
    currency           TEXT NOT NULL DEFAULT 'MAD',
    status             TEXT NOT NULL DEFAULT 'unmatched', -- unmatched | suggested | matched | excluded
    matched_invoice_id TEXT REFERENCES invoices(id),
    match_confidence   REAL,
    match_explanation  TEXT,
    matched_by         TEXT REFERENCES users(id),   -- NULL = automatic suggestion
    matched_at         TEXT,
    is_duplicate_of    TEXT REFERENCES bank_transactions(id),
    suggested_account  TEXT,                        -- suggested PCG counterpart
    suggested_label    TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_tx_account ON bank_transactions(bank_account_id, date);
CREATE INDEX IF NOT EXISTS idx_bank_tx_firm ON bank_transactions(firm_id, status);

-- v6: the pilot instrument — per-invoice feedback
CREATE TABLE IF NOT EXISTS feedback (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    user_id    TEXT NOT NULL REFERENCES users(id),
    rating     TEXT NOT NULL,            -- up | down
    reason     TEXT,                     -- wrong_account | wrong_vat | wrong_supplier | wrong_client | ocr_issue | other
    comment    TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (invoice_id, user_id)         -- one verdict per person per invoice; re-submitting updates it
);
CREATE INDEX IF NOT EXISTS idx_feedback_firm ON feedback(firm_id, rating, reason);

-- v7: user management, role permissions, expense claims
CREATE TABLE IF NOT EXISTS role_permissions (
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    role       TEXT NOT NULL,            -- business_admin | firm_admin | accountant | reviewer | employee
    permission TEXT NOT NULL,            -- e.g. invoices.upload, expenses.approve
    allowed    INTEGER NOT NULL,         -- overrides the code default for this firm
    PRIMARY KEY (firm_id, role, permission)
);

CREATE TABLE IF NOT EXISTS expense_claims (
    id           TEXT PRIMARY KEY,
    firm_id      TEXT NOT NULL REFERENCES firms(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    title        TEXT NOT NULL,
    description  TEXT,
    category     TEXT,                   -- transport | repas | hebergement | fournitures | autre
    amount       REAL NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'MAD',
    expense_date TEXT,
    status       TEXT NOT NULL DEFAULT 'draft',  -- draft | open | approved | rejected
    reviewed_by  TEXT REFERENCES users(id),
    reviewed_at  TEXT,
    review_note  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expense_claims_firm ON expense_claims(firm_id, status);
CREATE INDEX IF NOT EXISTS idx_expense_claims_user ON expense_claims(user_id, status);

CREATE TABLE IF NOT EXISTS expense_attachments (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    claim_id    TEXT NOT NULL REFERENCES expense_claims(id),
    filename    TEXT NOT NULL,
    uploaded_by TEXT NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expense_attachments_claim ON expense_attachments(claim_id);

-- v8: supplier connectors (marketplace). Connections are real records;
-- actual document sync is a stub until supplier integrations exist.
CREATE TABLE IF NOT EXISTS supplier_connections (
    id           TEXT PRIMARY KEY,
    firm_id      TEXT NOT NULL REFERENCES firms(id),
    supplier_key TEXT NOT NULL,           -- catalog key, e.g. 'maroc_telecom'
    connected_by TEXT NOT NULL REFERENCES users(id),
    last_sync_at TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (firm_id, supplier_key)
);

-- v9: integrations (API keys are real & usable server-side; webhooks are
-- stored config, not yet fired)
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    name       TEXT NOT NULL,
    prefix     TEXT NOT NULL,                -- first 8 chars, shown in the UI
    key_hash   TEXT NOT NULL,                -- PBKDF2, never the raw key
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_firm ON api_keys(firm_id);

CREATE TABLE IF NOT EXISTS webhooks (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    url        TEXT NOT NULL,
    events     TEXT NOT NULL DEFAULT '[]',   -- JSON array: invoice.approved, invoice.rejected…
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhooks_firm ON webhooks(firm_id);

-- v10: approval workflows (conditions -> approver chain). Workflows are
-- materialized per invoice and enforced in order before final approval.
CREATE TABLE IF NOT EXISTS approval_workflows (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    name        TEXT NOT NULL,
    conditions  TEXT NOT NULL DEFAULT '{}',  -- JSON: supplier, category, min_amount, max_amount
    approvers   TEXT NOT NULL DEFAULT '[]',  -- JSON: ordered user ids
    priority    INTEGER NOT NULL DEFAULT 0,  -- higher wins when several match
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_workflows_firm ON approval_workflows(firm_id);

-- v11: lettrage des comptes de tiers (3421 clients, 4411 fournisseurs, ...)
-- Un lettrage = un code (A, B, ... AA) posé sur un ensemble de lignes du
-- journal dont la somme des débits égale la somme des crédits.
CREATE TABLE IF NOT EXISTS lettrages (
    id             TEXT PRIMARY KEY,
    firm_id        TEXT NOT NULL REFERENCES firms(id),
    account_number TEXT NOT NULL,
    code           TEXT NOT NULL,
    total          REAL NOT NULL,               -- somme lettrée (débits = crédits)
    created_by     TEXT NOT NULL REFERENCES users(id),
    created_at     TEXT NOT NULL,
    UNIQUE (firm_id, account_number, code)
);
CREATE TABLE IF NOT EXISTS lettrage_lines (
    lettrage_id TEXT NOT NULL REFERENCES lettrages(id),
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    invoice_id  TEXT NOT NULL,   -- id de facture OU 'od-<id>' (OD manuelle)
    entry_idx   INTEGER NOT NULL,
    line_idx    INTEGER NOT NULL,
    side        TEXT NOT NULL,
    amount      REAL NOT NULL,
    UNIQUE (firm_id, invoice_id, entry_idx, line_idx)   -- une ligne = un seul lettrage
);
CREATE INDEX IF NOT EXISTS idx_lettrage_lines ON lettrage_lines(lettrage_id);

-- v12: révisions de comptes (fiche compte / centre de révision)
-- Un enregistrement = « ce compte a été revu par X à telle date, sur tel état ».
CREATE TABLE IF NOT EXISTS account_reviews (
    id             TEXT PRIMARY KEY,
    firm_id        TEXT NOT NULL REFERENCES firms(id),
    account_number TEXT NOT NULL,
    reviewed_by    TEXT NOT NULL REFERENCES users(id),
    entries_count  INTEGER NOT NULL,               -- écritures au moment de la revue
    etat           TEXT NOT NULL,                  -- revise | a_verifier | anomalies | bloque
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_reviews ON account_reviews(firm_id, account_number, created_at);

-- v13: OD manuelles (écritures de régularisation). Volontairement simples :
-- journal, date, libellé, lignes D/C, pièce jointe optionnelle — et un
-- contrôle d'équilibre bloquant à la saisie. Elles alimentent journal_rows,
-- donc Grand Livre / Balance / Lettrage / Fiche compte les voient sans code.
CREATE TABLE IF NOT EXISTS manual_entries (
    id         TEXT PRIMARY KEY,
    firm_id    TEXT NOT NULL REFERENCES firms(id),
    journal    TEXT NOT NULL DEFAULT 'OD',      -- OD | AN | BQ | CAI
    date       TEXT NOT NULL,
    libelle    TEXT NOT NULL,
    piece      TEXT,                            -- nom du justificatif joint
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_entries ON manual_entries(firm_id, date);
CREATE TABLE IF NOT EXISTS manual_entry_lines (
    entry_id       TEXT NOT NULL REFERENCES manual_entries(id),
    line_idx       INTEGER NOT NULL,
    account_number TEXT NOT NULL,
    account_label  TEXT NOT NULL,
    side           TEXT NOT NULL,               -- DEBIT | CREDIT
    amount         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_entry_lines ON manual_entry_lines(entry_id);


-- v14: authoritative posting ledger, period locks, tax registry, payment allocations
CREATE TABLE IF NOT EXISTS accounting_periods (
    id           TEXT PRIMARY KEY,
    firm_id      TEXT NOT NULL REFERENCES firms(id),
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',      -- OPEN | SOFT_CLOSED | CLOSED
    closed_by    TEXT REFERENCES users(id),
    closed_at    TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (firm_id, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_accounting_periods_firm ON accounting_periods(firm_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS posting_batches (
    id              TEXT PRIMARY KEY,
    firm_id         TEXT NOT NULL REFERENCES firms(id),
    client_id       TEXT REFERENCES clients(id),
    invoice_id      TEXT REFERENCES invoices(id),
    manual_entry_id TEXT REFERENCES manual_entries(id),
    bank_transaction_id TEXT REFERENCES bank_transactions(id),
    posting_date    TEXT NOT NULL,
    document_date   TEXT,
    journal_code    TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    entry_number    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'posted', -- posted | reversed
    posted_by       TEXT NOT NULL REFERENCES users(id),
    posted_at       TEXT NOT NULL,
    reversal_of     TEXT REFERENCES posting_batches(id),
    reversed_by     TEXT REFERENCES posting_batches(id),
    reversal_reason TEXT,
    UNIQUE (firm_id, fiscal_year, entry_number)
);
CREATE INDEX IF NOT EXISTS idx_posting_batches_firm_date ON posting_batches(firm_id, posting_date);
CREATE INDEX IF NOT EXISTS idx_posting_batches_invoice ON posting_batches(invoice_id);
CREATE INDEX IF NOT EXISTS idx_posting_batches_bank_tx ON posting_batches(bank_transaction_id);

CREATE TABLE IF NOT EXISTS posting_lines (
    batch_id          TEXT NOT NULL REFERENCES posting_batches(id),
    line_idx          INTEGER NOT NULL,
    account_number    TEXT NOT NULL,
    account_label     TEXT NOT NULL,
    side              TEXT NOT NULL,             -- DEBIT | CREDIT
    amount_cents      INTEGER NOT NULL CHECK(amount_cents > 0),
    entry_label       TEXT NOT NULL,
    source_perspective TEXT NOT NULL,
    aux_account       TEXT,
    partner_name      TEXT,
    tax_code          TEXT,
    PRIMARY KEY (batch_id, line_idx)
);
CREATE INDEX IF NOT EXISTS idx_posting_lines_account ON posting_lines(account_number);

CREATE TABLE IF NOT EXISTS tax_rules (
    id               TEXT PRIMARY KEY,
    firm_id          TEXT REFERENCES firms(id),   -- NULL = platform/default rule
    tax_type         TEXT NOT NULL,
    transaction_nature TEXT,
    party_type       TEXT,
    supplier_residency TEXT,
    payer_entity_type TEXT,
    certificate_state TEXT,
    effective_from   TEXT NOT NULL,
    effective_to     TEXT,
    rate             TEXT,                        -- decimal as text; no binary-float truth
    legal_basis      TEXT,
    account_number   TEXT,
    recoverability   TEXT,
    tax_treatment_code TEXT,
    required_evidence TEXT NOT NULL DEFAULT '[]', -- JSON
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tax_rules_effective ON tax_rules(tax_type, effective_from, effective_to);

CREATE TABLE IF NOT EXISTS payment_allocations (
    id                  TEXT PRIMARY KEY,
    firm_id             TEXT NOT NULL REFERENCES firms(id),
    invoice_id          TEXT NOT NULL REFERENCES invoices(id),
    bank_transaction_id TEXT NOT NULL REFERENCES bank_transactions(id),
    amount_cents        INTEGER NOT NULL CHECK(amount_cents > 0),
    allocated_by        TEXT REFERENCES users(id),
    allocated_at        TEXT NOT NULL,
    UNIQUE (invoice_id, bank_transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_payment_alloc_invoice ON payment_allocations(invoice_id);

CREATE TABLE IF NOT EXISTS invoice_approval_steps (
    id          TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL REFERENCES invoices(id),
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    workflow_id TEXT NOT NULL REFERENCES approval_workflows(id),
    step_index  INTEGER NOT NULL,
    approver_id TEXT NOT NULL REFERENCES users(id),
    approved_at TEXT,
    note        TEXT,
    UNIQUE(invoice_id, workflow_id, step_index)
);
CREATE INDEX IF NOT EXISTS idx_invoice_approval_steps ON invoice_approval_steps(invoice_id, step_index);


-- v15: versioned accounting/tax controls, asset register and observability
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_rules (
    id                 TEXT PRIMARY KEY,
    firm_id            TEXT REFERENCES firms(id),  -- NULL = platform Moroccan PCG rule
    rule_key           TEXT NOT NULL,
    account_number     TEXT NOT NULL,
    account_label      TEXT NOT NULL,
    effective_from     TEXT NOT NULL DEFAULT '1900-01-01',
    effective_to       TEXT,
    legal_basis        TEXT,
    approved_by        TEXT REFERENCES users(id),
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    UNIQUE(firm_id, rule_key, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_account_rules_lookup ON account_rules(rule_key, effective_from, effective_to);

CREATE TABLE IF NOT EXISTS fixed_assets (
    id                   TEXT PRIMARY KEY,
    firm_id              TEXT NOT NULL REFERENCES firms(id),
    client_id            TEXT REFERENCES clients(id),
    invoice_id           TEXT REFERENCES invoices(id),
    posting_batch_id     TEXT REFERENCES posting_batches(id),
    source_line_index    INTEGER,
    asset_type           TEXT NOT NULL,
    description          TEXT NOT NULL,
    account_number       TEXT NOT NULL,
    acquisition_date     TEXT NOT NULL,
    in_service_date      TEXT,
    acquisition_cost_cents INTEGER NOT NULL,
    residual_value_cents INTEGER NOT NULL DEFAULT 0,
    useful_life_months   INTEGER NOT NULL DEFAULT 60,
    depreciation_method  TEXT NOT NULL DEFAULT 'straight_line',
    accumulated_depreciation_cents INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'active',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixed_assets_firm ON fixed_assets(firm_id, status);

CREATE TABLE IF NOT EXISTS depreciation_runs (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    client_id   TEXT REFERENCES clients(id),
    period      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft', -- draft | posted
    created_by  TEXT REFERENCES users(id),
    created_at  TEXT NOT NULL,
    UNIQUE(firm_id, client_id, period)
);

CREATE TABLE IF NOT EXISTS depreciation_lines (
    run_id       TEXT NOT NULL REFERENCES depreciation_runs(id),
    asset_id     TEXT NOT NULL REFERENCES fixed_assets(id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    expense_account TEXT NOT NULL DEFAULT '6193',
    accumulated_account TEXT NOT NULL,
    PRIMARY KEY(run_id, asset_id)
);

CREATE TABLE IF NOT EXISTS accounting_rule_events (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT,
    invoice_id  TEXT,
    severity    TEXT NOT NULL,
    rule_code   TEXT NOT NULL,
    message     TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_events_firm ON accounting_rule_events(firm_id, created_at);

CREATE TABLE IF NOT EXISTS close_adjustment_checks (
    id          TEXT PRIMARY KEY,
    firm_id     TEXT NOT NULL REFERENCES firms(id),
    client_id   TEXT REFERENCES clients(id),
    month       TEXT NOT NULL,
    check_type  TEXT NOT NULL, -- accruals | prepaids | inventory | payroll | fx | prior_period
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | done | not_applicable
    note        TEXT,
    updated_by  TEXT REFERENCES users(id),
    updated_at  TEXT NOT NULL,
    UNIQUE(firm_id, client_id, month, check_type)
);
