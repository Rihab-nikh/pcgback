"""Clients. Accountants see only clients assigned to them; firm admins see the firm."""
from app.core.db import execute, new_id, now, query, query_one


def create_client(firm_id: str, name: str, ice: str | None = None, if_number: str | None = None,
                  address: str | None = None, assigned_to: str | None = None) -> dict:
    cid = new_id()
    execute("""INSERT INTO clients (id, firm_id, name, ice, if_number, address, assigned_to, is_archived, created_at)
               VALUES (?,?,?,?,?,?,?,0,?)""",
            (cid, firm_id, name, ice, if_number, address, assigned_to, now()))
    return get_client(cid, firm_id)


def get_client(client_id: str, firm_id: str) -> dict | None:
    return query_one("SELECT * FROM clients WHERE id = ? AND firm_id = ?", (client_id, firm_id))


def list_clients(firm_id: str, *, assigned_to: str | None = None,
                 include_archived: bool = False, q: str | None = None) -> list[dict]:
    sql = """
        SELECT c.*, u.full_name AS accountant_name,
               (SELECT COUNT(*) FROM invoices i WHERE i.client_id = c.id) AS invoice_count,
               (SELECT COUNT(*) FROM invoices i WHERE i.client_id = c.id AND i.status = 'needs_review') AS pending_count
        FROM clients c LEFT JOIN users u ON u.id = c.assigned_to
        WHERE c.firm_id = ?"""
    params: list = [firm_id]
    if assigned_to:
        sql += " AND c.assigned_to = ?"
        params.append(assigned_to)
    if not include_archived:
        sql += " AND c.is_archived = 0"
    if q:
        sql += " AND (c.name LIKE ? OR c.ice LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    return query(sql + " ORDER BY c.name", tuple(params))


def set_assigned_clients(firm_id: str, user_id: str, client_ids: list[str]) -> None:
    """Replace the set of clients assigned to an accountant (unassign the rest)."""
    execute("UPDATE clients SET assigned_to = NULL WHERE firm_id = ? AND assigned_to = ?",
            (firm_id, user_id))
    for cid in client_ids:
        execute("UPDATE clients SET assigned_to = ? WHERE id = ? AND firm_id = ?",
                (user_id, cid, firm_id))


def update_client(client_id: str, firm_id: str, **fields) -> None:
    allowed = {"name", "ice", "if_number", "address", "assigned_to", "is_archived"}
    sets, params = [], []
    nullable = {"assigned_to", "ice", "if_number", "address"}  # empty string = clear
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            if k in nullable and v == "":
                params.append(None)
            else:
                params.append(int(v) if k == "is_archived" else v)
    if sets:
        params += [client_id, firm_id]
        execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = ? AND firm_id = ?", tuple(params))
