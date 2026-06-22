import json
import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "determinations.db")

ADMIN_COMPANY_ID = 1
ADMIN_COMPANY_NAME = "Admin"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema helpers ─────────────────────────────────────────────────────────────

def _run_migrations(conn: sqlite3.Connection, migrations: list[str]) -> None:
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass  # Column / table already exists


# ── Core tables ────────────────────────────────────────────────────────────────

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
            last_checked_date TEXT NOT NULL DEFAULT '',
            company_id INTEGER,
            user_id INTEGER
        )
    """)
    conn.commit()
    _run_migrations(conn, [
        "ALTER TABLE determinations ADD COLUMN lender_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE determinations ADD COLUMN life_of_loan INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE determinations ADD COLUMN needs_redetermination INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE determinations ADD COLUMN last_checked_date TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE determinations ADD COLUMN company_id INTEGER",
        "ALTER TABLE determinations ADD COLUMN user_id INTEGER",
        "ALTER TABLE determinations ADD COLUMN county TEXT NOT NULL DEFAULT ''",
    ])
    conn.close()


# ── Companies ──────────────────────────────────────────────────────────────────

def init_companies():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            address TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    # Seed the Admin internal company (id=1)
    row = conn.execute("SELECT id FROM companies WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            "INSERT OR IGNORE INTO companies (name, address, created_at) VALUES (?, ?, ?)",
            (ADMIN_COMPANY_NAME, "", datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def get_or_create_company(name: str, address: str = "") -> int:
    """Return the company_id, creating the company if it doesn't already exist."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM companies WHERE LOWER(name) = LOWER(?)", (name.strip(),)
    ).fetchone()
    if row:
        company_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO companies (name, address, created_at) VALUES (?, ?, ?)",
            (name.strip(), address.strip(), datetime.utcnow().isoformat()),
        )
        conn.commit()
        company_id = cur.lastrowid
    conn.close()
    return company_id


def get_company_by_id(company_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_companies() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM companies ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Determinations ─────────────────────────────────────────────────────────────

def save_determination(data: dict) -> int:
    """Insert new determination or update existing one for same loan_id + company_id.
    
    When the same loan is re-searched, we update the existing record with fresh data
    rather than creating duplicates. This ensures history always shows current accurate
    data and prevents old incorrect results from persisting.
    """
    conn = get_conn()
    params = {
        "lender_email": "",
        "company_id": None,
        "user_id": None,
        "county": "",
        **data,
        "created_at": datetime.utcnow().isoformat(),
    }
    # Check if record already exists for same loan_id + company_id
    existing = conn.execute(
        "SELECT id FROM determinations WHERE loan_id = ? AND company_id IS ? ORDER BY created_at DESC LIMIT 1",
        (params.get("loan_id"), params.get("company_id"))
    ).fetchone()

    if existing:
        # Update existing record with fresh data — no duplicate history
        conn.execute("""
            UPDATE determinations SET
                borrower_name=:borrower_name, lender_name=:lender_name,
                lender_email=:lender_email, property_address=:property_address,
                matched_address=:matched_address, lat=:lat, lon=:lon,
                flood_zone=:flood_zone, flood_zone_description=:flood_zone_description,
                sfha_status=:sfha_status, insurance_required=:insurance_required,
                panel_number=:panel_number, panel_effective_date=:panel_effective_date,
                community_number=:community_number, community_name=:community_name,
                determination_date=:determination_date,
                determination_date_iso=:determination_date_iso,
                created_at=:created_at, county=:county
            WHERE id=:existing_id
        """, {**params, "existing_id": existing["id"]})
        conn.commit()
        record_id = existing["id"]
    else:
        cur = conn.execute("""
            INSERT INTO determinations (
                loan_id, borrower_name, lender_name, lender_email,
                property_address, matched_address, lat, lon,
                flood_zone, flood_zone_description, sfha_status, insurance_required,
                panel_number, panel_effective_date, community_number, community_name,
                determination_date, determination_date_iso, created_at,
                company_id, user_id, county
            ) VALUES (
                :loan_id, :borrower_name, :lender_name, :lender_email,
                :property_address, :matched_address, :lat, :lon,
                :flood_zone, :flood_zone_description, :sfha_status, :insurance_required,
                :panel_number, :panel_effective_date, :community_number, :community_name,
                :determination_date, :determination_date_iso, :created_at,
                :company_id, :user_id, :county
            )
        """, params)
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



def get_determinations(
    company_id: Optional[int] = None,
    user_id: Optional[int] = None,
    limit: int = 500,
    search: str = "",
) -> list:
    """Alias for filtered determination listing used by profile and history pages."""
    conn = get_conn()
    clauses, params = [], []
    if company_id is not None:
        clauses.append("company_id = ?"); params.append(company_id)
    if user_id is not None:
        clauses.append("user_id = ?"); params.append(user_id)
    if search:
        like = f"%{search}%"
        clauses.append("(loan_id LIKE ? OR borrower_name LIKE ? OR property_address LIKE ?)")
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM determinations {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_determinations(query: str, company_id: Optional[int] = None) -> list:
    conn = get_conn()
    like = f"%{query}%"
    if company_id is not None:
        rows = conn.execute("""
            SELECT * FROM determinations
            WHERE company_id = ? AND (loan_id LIKE ? OR borrower_name LIKE ? OR property_address LIKE ?)
            ORDER BY created_at DESC LIMIT 100
        """, (company_id, like, like, like)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM determinations
            WHERE loan_id LIKE ? OR borrower_name LIKE ? OR property_address LIKE ?
            ORDER BY created_at DESC LIMIT 100
        """, (like, like, like)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_determinations(limit: int = 100, company_id: Optional[int] = None) -> list:
    conn = get_conn()
    if company_id is not None:
        rows = conn.execute(
            "SELECT * FROM determinations WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM determinations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_determinations_admin(
    company_id: Optional[int] = None,
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    query: Optional[str] = None,
    flood_zone: Optional[str] = None,
    limit: int = 200,
) -> list:
    """Admin-only: flexible query across all companies."""
    conn = get_conn()
    clauses, params = [], []
    if company_id is not None:
        clauses.append("company_id = ?"); params.append(company_id)
    if user_id is not None:
        clauses.append("user_id = ?"); params.append(user_id)
    if date_from:
        clauses.append("determination_date_iso >= ?"); params.append(date_from)
    if date_to:
        clauses.append("determination_date_iso <= ?"); params.append(date_to)
    if flood_zone:
        clauses.append("flood_zone = ?"); params.append(flood_zone)
    if query:
        like = f"%{query}%"
        clauses.append("(loan_id LIKE ? OR borrower_name LIKE ? OR property_address LIKE ?)")
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM determinations {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
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


def bulk_delete_determinations(record_ids: list[int]) -> int:
    if not record_ids:
        return 0
    conn = get_conn()
    placeholders = ",".join("?" * len(record_ids))
    cur = conn.execute(
        f"DELETE FROM determinations WHERE id IN ({placeholders})", record_ids
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


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


# ── Admin Audit / Deletion Log ─────────────────────────────────────────────────

def init_audit_tables():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_deletion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id INTEGER NOT NULL,
            deleted_record_ids TEXT NOT NULL DEFAULT '[]',
            deleted_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def log_admin_deletion(admin_user_id: int, record_ids: list[int], reason: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO admin_deletion_log (admin_user_id, deleted_record_ids, deleted_at, reason) VALUES (?, ?, ?, ?)",
        (admin_user_id, json.dumps(record_ids), datetime.utcnow().isoformat(), reason),
    )
    conn.commit()
    conn.close()


def list_admin_deletion_log(limit: int = 200) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM admin_deletion_log ORDER BY deleted_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── LOL Monitoring ─────────────────────────────────────────────────────────────

def init_lol_tables():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lol_monitoring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            user_id INTEGER,
            determination_id INTEGER,
            loan_id TEXT NOT NULL DEFAULT '',
            borrower_name TEXT NOT NULL DEFAULT '',
            property_address TEXT NOT NULL,
            lat REAL,
            lon REAL,
            lender_name TEXT NOT NULL DEFAULT '',
            lender_email TEXT NOT NULL DEFAULT '',
            baseline_panel_number TEXT NOT NULL DEFAULT '',
            baseline_effective_date TEXT NOT NULL DEFAULT '',
            baseline_flood_zone TEXT NOT NULL DEFAULT '',
            baseline_community_number TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Active',
            created_at TEXT NOT NULL,
            last_checked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lol_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitoring_id INTEGER NOT NULL,
            changed_fields TEXT NOT NULL DEFAULT '[]',
            old_values TEXT NOT NULL DEFAULT '{}',
            new_values TEXT NOT NULL DEFAULT '{}',
            alert_sent_at TEXT NOT NULL,
            lender_email TEXT NOT NULL DEFAULT '',
            email_status TEXT NOT NULL DEFAULT 'Sent',
            retry_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def upsert_lol_monitoring(det: dict) -> int:
    """Create or refresh a lol_monitoring record from a determination dict."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM lol_monitoring WHERE determination_id = ?", (det["id"],)
    ).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        conn.execute("""
            UPDATE lol_monitoring SET
                baseline_panel_number = ?,
                baseline_effective_date = ?,
                baseline_flood_zone = ?,
                baseline_community_number = ?,
                lender_email = ?,
                lender_name = ?,
                status = 'Active',
                last_checked_at = ?
            WHERE id = ?
        """, (
            det.get("community_number", ""),
            det.get("panel_effective_date", ""),
            det.get("flood_zone", ""),
            det.get("panel_number", ""),
            det.get("lender_email", ""),
            det.get("lender_name", ""),
            now,
            existing["id"],
        ))
        conn.commit()
        mid = existing["id"]
    else:
        cur = conn.execute("""
            INSERT INTO lol_monitoring (
                company_id, user_id, determination_id, loan_id, borrower_name,
                property_address, lat, lon, lender_name, lender_email,
                baseline_panel_number, baseline_effective_date, baseline_flood_zone,
                baseline_community_number, status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            det.get("company_id"),
            det.get("user_id"),
            det.get("id"),
            det.get("loan_id", ""),
            det.get("borrower_name", ""),
            det.get("property_address", ""),
            det.get("lat"),
            det.get("lon"),
            det.get("lender_name", ""),
            det.get("lender_email", ""),
            det.get("community_number", ""),
            det.get("panel_effective_date", ""),
            det.get("flood_zone", ""),
            det.get("panel_number", ""),
            "Active",
            now,
        ))
        conn.commit()
        mid = cur.lastrowid
    conn.close()
    return mid


def get_lol_monitoring(monitoring_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM lol_monitoring WHERE id = ?", (monitoring_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_lol_monitoring(company_id: Optional[int] = None, status: str = None) -> list:
    conn = get_conn()
    clauses, params = [], []
    if company_id is not None:
        clauses.append("company_id = ?"); params.append(company_id)
    if status:
        clauses.append("status = ?"); params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM lol_monitoring {where} ORDER BY created_at DESC", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_active_lol_monitoring() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM lol_monitoring WHERE status = 'Active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_lol_monitoring(monitoring_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE lol_monitoring SET status = 'Closed' WHERE id = ?", (monitoring_id,))
    conn.commit()
    conn.close()


def update_lol_last_checked(monitoring_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE lol_monitoring SET last_checked_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), monitoring_id),
    )
    conn.commit()
    conn.close()


def create_lol_alert(
    monitoring_id: int,
    changed_fields: list,
    old_values: dict,
    new_values: dict,
    lender_email: str,
    email_status: str = "Sent",
) -> int:
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO lol_alerts (monitoring_id, changed_fields, old_values, new_values, alert_sent_at, lender_email, email_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        monitoring_id,
        json.dumps(changed_fields),
        json.dumps(old_values),
        json.dumps(new_values),
        datetime.utcnow().isoformat(),
        lender_email,
        email_status,
    ))
    conn.commit()
    alert_id = cur.lastrowid
    conn.close()
    return alert_id


def list_lol_alerts(company_id: Optional[int] = None, limit: int = 200) -> list:
    conn = get_conn()
    if company_id is not None:
        rows = conn.execute("""
            SELECT a.*, m.property_address, m.loan_id, m.borrower_name, m.company_id
            FROM lol_alerts a JOIN lol_monitoring m ON a.monitoring_id = m.id
            WHERE m.company_id = ?
            ORDER BY a.alert_sent_at DESC LIMIT ?
        """, (company_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.*, m.property_address, m.loan_id, m.borrower_name, m.company_id
            FROM lol_alerts a JOIN lol_monitoring m ON a.monitoring_id = m.id
            ORDER BY a.alert_sent_at DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        for key in ("changed_fields", "old_values", "new_values"):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
        results.append(d)
    return results


def update_alert_email_status(alert_id: int, status: str, retry_count: int = None) -> None:
    conn = get_conn()
    if retry_count is not None:
        conn.execute(
            "UPDATE lol_alerts SET email_status = ?, retry_count = ? WHERE id = ?",
            (status, retry_count, alert_id),
        )
    else:
        conn.execute("UPDATE lol_alerts SET email_status = ? WHERE id = ?", (status, alert_id))
    conn.commit()
    conn.close()


def count_failed_lol_alerts() -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM lol_alerts WHERE email_status = 'Failed'"
    ).fetchone()
    conn.close()
    return row[0] if row else 0


# ── User / Auth tables ──────────────────────────────────────────────────────────

def init_auth_tables():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            company_name TEXT NOT NULL DEFAULT '',
            company_address TEXT NOT NULL DEFAULT '',
            contact_number TEXT NOT NULL DEFAULT '',
            company_id INTEGER,
            password_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            is_admin INTEGER NOT NULL DEFAULT 0,
            request_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            approved_at TEXT
        )
    """)
    conn.commit()
    _run_migrations(conn, [
        "ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN company_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN company_address TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN contact_number TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN company_id INTEGER",
        "ALTER TABLE users ADD COLUMN password_reset_token TEXT",
        "ALTER TABLE users ADD COLUMN password_reset_expiry TEXT",
    ])
    conn.close()


def create_user(
    email: str,
    name: str = "",
    first_name: str = "",
    last_name: str = "",
    company_name: str = "",
    company_address: str = "",
    contact_number: str = "",
    company_id: Optional[int] = None,
    reason: str = "",
    password_hash: str = None,
    status: str = "pending",
    is_admin: int = 0,
) -> int:
    # Auto-derive name from first/last if not provided
    if not name and (first_name or last_name):
        name = f"{first_name} {last_name}".strip()
    conn = get_conn()
    cur = conn.execute(
        """INSERT OR IGNORE INTO users
           (email, name, first_name, last_name, company_name, company_address,
            contact_number, company_id, request_reason, password_hash, status, is_admin, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            email.lower().strip(), name.strip(),
            first_name.strip(), last_name.strip(),
            company_name.strip(), company_address.strip(),
            contact_number.strip(), company_id,
            reason, password_hash, status, is_admin,
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


def list_users_by_status(status: str = None, company_id: int = None) -> list:
    conn = get_conn()
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if company_id is not None:
        clauses.append("company_id = ?"); params.append(company_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM users {where} ORDER BY created_at DESC", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_user(user_id: int, password_hash: str) -> None:
    """Approve user; company assignment is handled separately by the route."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET status='active', password_hash=?, approved_at=? WHERE id=?",
        (password_hash, datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def set_user_company(user_id: int, company_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET company_id = ? WHERE id = ?", (company_id, user_id))
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


def get_user_by_reset_token(token: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE password_reset_token = ?", (token,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_reset_token(user_id: int, token: str, expiry: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_reset_token=?, password_reset_expiry=? WHERE id=?",
        (token, expiry, user_id)
    )
    conn.commit()
    conn.close()


def clear_reset_token(user_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_reset_token=NULL, password_reset_expiry=NULL WHERE id=?",
        (user_id,)
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


def list_users_by_company(company_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE company_id = ? ORDER BY created_at DESC", (company_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
