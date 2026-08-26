"""Notifications and the audit trail."""
from app.core.db import execute, new_id, now, query


# ── Notifications ──
def notify(firm_id: str, kind: str, message: str, *, user_id: str | None = None,
           invoice_id: str | None = None) -> None:
    execute("""INSERT INTO notifications (id, firm_id, user_id, kind, message, invoice_id, is_read, created_at)
               VALUES (?,?,?,?,?,?,0,?)""",
            (new_id(), firm_id, user_id, kind, message, invoice_id, now()))


def list_notifications(firm_id: str, user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
    sql = """SELECT * FROM notifications
             WHERE firm_id = ? AND (user_id IS NULL OR user_id = ?)"""
    if unread_only:
        sql += " AND is_read = 0"
    return query(sql + " ORDER BY created_at DESC LIMIT ?", (firm_id, user_id, limit))


def mark_read(notification_id: str, firm_id: str) -> None:
    execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND firm_id = ?",
            (notification_id, firm_id))


# ── Audit trail ──
def audit(action: str, *, user_id: str | None, firm_id: str | None,
          entity_type: str | None = None, entity_id: str | None = None,
          detail: str | None = None) -> None:
    execute("""INSERT INTO audit_logs (id, firm_id, user_id, action, entity_type, entity_id, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id(), firm_id, user_id, action, entity_type, entity_id, detail, now()))


def list_audit(firm_id: str | None, limit: int = 100) -> list[dict]:
    """firm_id=None => platform-wide (super admin only)."""
    if firm_id is None:
        return query("""SELECT a.*, u.email AS user_email FROM audit_logs a
                        LEFT JOIN users u ON u.id = a.user_id
                        ORDER BY a.created_at DESC LIMIT ?""", (limit,))
    return query("""SELECT a.*, u.email AS user_email FROM audit_logs a
                    LEFT JOIN users u ON u.id = a.user_id
                    WHERE a.firm_id = ? ORDER BY a.created_at DESC LIMIT ?""", (firm_id, limit))
