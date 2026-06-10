import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "determinations.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS determinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id TEXT NOT NULL,
            borrower_name TEXT NOT NULL,
            lender_name TEXT NOT NULL,
            lender_email TEXT NOT NULL DEFAULT '',
            property_address TEXT NOT NULL,
            matched_address TEXT NOT NULL,
            lat REAL,
            lon REAL,
            flood_zone TEXT NOT NULL,
            flood_zone_description TEXT NOT NULL,
            sfha_status TEXT NOT NULL,
            insurance_required TEXT NOT NULL,
            panel_number TEXT NOT NULL,
            panel_effective_date TEXT NOT NULL,
            community_number TEXT NOT NULL,
            community_name TEXT NOT NULL,
            determination_date TEXT NOT NULL,
            determination_date_iso TEXT NOT NULL,
            created_at TEXT NOT NULL,
            life_of_loan INTEGER NOT NULL DEFAULT 0,
            needs_redetermination INTEGER NOT NULL DEFAULT 0,
            last_checked_date TEXT NOT NULL DEFAULT ''
        )
    """)
    # Migrations for databases created before these columns existed
    for migration in [
        "ALTER TABLE determinations ADD COLUMN lender_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE determinations ADD COLUMN life_of_loan INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE determinations ADD COLUMN needs_redetermination INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE determinations ADD COLUMN last_checked_date TEXT NOT NULL DEFAULT ''",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists
    conn.commit()
    conn.close()


def save_determination(data: dict) -> int:
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO determinations (
            loan_id, borrower_name, lender_name, lender_email,
            property_address, matched_address, lat, lon,
            flood_zone, flood_zone_description, sfha_status, insurance_required,
            panel_number, panel_effective_date, community_number, community_name,
            determination_date, determination_date_iso, created_at
        ) VALUES (
            :loan_id, :borrower_name, :lender_name, :lender_email,
            :property_address, :matched_address, :lat, :lon,
            :flood_zone, :flood_zone_description, :sfha_status, :insurance_required,
            :panel_number, :panel_effective_date, :community_number, :community_name,
            :determination_date, :determination_date_iso, :created_at
        )
    """, {**{"lender_email": ""}, **data, "created_at": datetime.utcnow().isoformat()})
    conn.commit()
    record_id = cur.lastrowid
    conn.close()
    return record_id


def get_determination(record_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM determinations WHERE id = ?", (record_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def search_determinations(query: str) -> list:
    conn = get_conn()
    like = f"%{query}%"
    rows = conn.execute("""
        SELECT * FROM determinations
        WHERE loan_id LIKE ? OR borrower_name LIKE ? OR property_address LIKE ?
        ORDER BY created_at DESC
        LIMIT 50
    """, (like, like, like)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_determinations(limit: int = 50) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM determinations ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_determination(record_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM determinations WHERE id = ?", (record_id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def set_life_of_loan(record_id: int, enabled: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE determinations SET life_of_loan = ? WHERE id = ?",
        (1 if enabled else 0, record_id),
    )
    conn.commit()
    conn.close()


def flag_redetermination(record_id: int, needs: bool, checked_date: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE determinations SET needs_redetermination = ?, last_checked_date = ? WHERE id = ?",
        (1 if needs else 0, checked_date, record_id),
    )
    conn.commit()
    conn.close()


def list_monitored() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM determinations WHERE life_of_loan = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_flagged() -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM determinations WHERE needs_redetermination = 1"
    ).fetchone()
    conn.close()
    return row[0] if row else 0


# ── User / Auth tables ──────────────────────────────────────────────────────

def init_auth_tables():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            password_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            is_admin INTEGER NOT NULL DEFAULT 0,
            request_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            approved_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def create_user(
    email: str,
    name: str,
    reason: str = "",
    password_hash: str = None,
    status: str = "pending",
    is_admin: int = 0,
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT OR IGNORE INTO users
           (email, name, request_reason, password_hash, status, is_admin, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            email.lower().strip(),
            name.strip(),
            reason,
            password_hash,
            status,
            is_admin,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return uid


def get_user_by_email(email: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users_by_status(status: str = None) -> list:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_user(user_id: int, password_hash: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET status='active', password_hash=?, approved_at=? WHERE id=?",
        (password_hash, datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def reject_user(user_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET status='rejected' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)
    )
    conn.commit()
    conn.close()


def set_user_status(user_id: int, status: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def count_pending_users() -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status='pending'"
    ).fetchone()
    conn.close()
    return row[0] if row else 0
