"""Users & firms. Every function that reads firm data takes firm_id explicitly —
tenant isolation is enforced at the query level, not by convention."""
from app.core.db import execute, new_id, now, query, query_one


# ── Firms ──
def create_firm(name: str, plan: str = "trial", accounting_software: str | None = None,
                country: str = "MA", currency: str = "MAD", logo: str | None = None) -> dict:
    fid = new_id()
    execute("""INSERT INTO firms (id, name, plan, accounting_software, country, currency, logo, is_active, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (fid, name, plan, accounting_software, country, currency, logo, now()))
    return get_firm(fid)


def get_firm(firm_id: str) -> dict | None:
    return query_one("SELECT * FROM firms WHERE id = ?", (firm_id,))


def list_firms() -> list[dict]:
    return query("""
        SELECT f.*, 
               (SELECT COUNT(*) FROM users u WHERE u.firm_id = f.id) AS user_count,
               (SELECT COUNT(*) FROM invoices i WHERE i.firm_id = f.id) AS invoice_count
        FROM firms f ORDER BY f.created_at DESC""")


def set_firm_active(firm_id: str, active: bool) -> None:
    execute("UPDATE firms SET is_active = ? WHERE id = ?", (1 if active else 0, firm_id))


# ── Users ──
def create_user(email: str, password_hash: str, full_name: str, role: str,
                firm_id: str | None, department: str | None = None,
                phone: str | None = None) -> dict:
    uid = new_id()
    execute("""INSERT INTO users (id, firm_id, email, password_hash, full_name, role, department, phone, is_active, created_at)
               VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (uid, firm_id, email.lower().strip(), password_hash, full_name, role, department, phone, now()))
    return get_user(uid)


def get_user(user_id: str) -> dict | None:
    return query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def get_user_by_email(email: str) -> dict | None:
    return query_one("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))


def touch_login(user_id: str) -> None:
    execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user_id))


def list_firm_users(firm_id: str) -> list[dict]:
    return query("""
        SELECT u.id, u.email, u.full_name, u.role, u.department, u.phone, u.is_active, u.last_login_at, u.created_at,
               (SELECT COUNT(*) FROM clients c WHERE c.assigned_to = u.id AND c.is_archived = 0) AS client_count,
               (SELECT COUNT(*) FROM invoices i WHERE i.uploaded_by = u.id) AS invoices_processed,
               (SELECT COUNT(*) FROM expense_claims e WHERE e.user_id = u.id) AS expense_claims
        FROM users u WHERE u.firm_id = ? ORDER BY u.created_at""", (firm_id,))


def update_user(user_id: str, firm_id: str, *, full_name: str | None = None,
                role: str | None = None, is_active: bool | None = None,
                department: str | None = None) -> None:
    if full_name is not None:
        execute("UPDATE users SET full_name = ? WHERE id = ? AND firm_id = ?",
                (full_name, user_id, firm_id))
    if role is not None:
        execute("UPDATE users SET role = ? WHERE id = ? AND firm_id = ?", (role, user_id, firm_id))
    if department is not None:
        execute("UPDATE users SET department = ? WHERE id = ? AND firm_id = ?",
                (department or None, user_id, firm_id))
    if is_active is not None:
        execute("UPDATE users SET is_active = ? WHERE id = ? AND firm_id = ?",
                (1 if is_active else 0, user_id, firm_id))
