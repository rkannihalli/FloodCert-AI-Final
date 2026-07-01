"""
db.py  –  PostgreSQL version of the FloodCert database layer.

Drop-in replacement for the SQLite version.
All public function signatures are identical so no other file needs changing.

Dependencies (add to requirements.txt if missing):
    psycopg2-binary
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras  # RealDictCursor

# ── Connection ─────────────────────────────────────────────────────────────────

_raw = os.getenv("DATABASE_URL", "")
DATABASE_URL = _raw.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is not set")


def _dsn() -> str:
    """Return a psycopg2-compatible DSN (uses postgresql:// scheme)."""
    return DATABASE_URL


@contextmanager
def get_conn():
    """Yield a psycopg2 connection with RealDictCursor and auto-commit on success."""
    conn = psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


ADMIN_COMPANY_ID = 1
ADMIN_COMPANY_NAME = "Admin"


# ── Migration helper ───────────────────────────────────────────────────────────

def _run_migrations(conn, migrations: list[str]) -> None:
    """Run ALTER TABLE migrations, ignoring duplicate-column errors."""
    for sql in migrations:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()
        except Exception:
            conn.rollback()


# ── Core tables ────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS determinations (
                    id SERIAL PRIMARY KEY,
                    loan_id TEXT NOT NULL,
                    borrower_name TEXT NOT NULL,
                    lender_name TEXT NOT NULL,
                    lender_email TEXT NOT NULL DEFAULT '',
                    property_address TEXT NOT NULL,
                    matched_address TEXT NOT NULL,
                    lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION,
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
                    user_id INTEGER,
                    county TEXT NOT NULL DEFAULT '',
                    loma_case_number TEXT,
                    loma_amendment_type TEXT,
                    loma_effective_date TEXT,
                    loma_original_zone TEXT,
                    loma_note TEXT
                )
            """)
        _run_migrations(conn, [
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS county TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS loma_case_number TEXT",
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS loma_amendment_type TEXT",
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS loma_effective_date TEXT",
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS loma_original_zone TEXT",
            "ALTER TABLE determinations ADD COLUMN IF NOT EXISTS loma_note TEXT",
        ])


# ── Companies ──────────────────────────────────────────────────────────────────

def init_companies():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS companies (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    address TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            # Seed Admin company (id=1)
            cur.execute("SELECT id FROM companies WHERE id = 1")
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO companies (name, address, created_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (ADMIN_COMPANY_NAME, "", datetime.utcnow().isoformat()),
                )


def _normalize_company_name(name: str) -> str:
    """Normalize company name for fuzzy matching — remove punctuation, extra spaces, lowercase."""
    import re as _re
    n = name.lower().strip()
    n = _re.sub(r"[^a-z0-9 ]", " ", n)   # remove all punctuation
    n = _re.sub(r"\s+", " ", n).strip()   # collapse whitespace
    # Remove common suffixes that vary
    for suffix in [" llc", " inc", " corp", " ltd", " co", " isaoa", " atima"]:
        if n.endswith(suffix):
            n = n[:-len(suffix)].strip()
    return n

def get_or_create_company(name: str, address: str = "") -> int:
    norm = _normalize_company_name(name)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Try exact normalized match first
            cur.execute("SELECT id, name FROM companies ORDER BY id")
            rows = cur.fetchall()
            for row in rows:
                if _normalize_company_name(row["name"]) == norm:
                    return row["id"]
            # Not found — create new
            cur.execute(
                "INSERT INTO companies (name, address, created_at) VALUES (%s, %s, %s) RETURNING id",
                (name.strip(), address.strip(), datetime.utcnow().isoformat()),
            )
            return cur.fetchone()["id"]


def get_company_by_id(company_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_companies() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies ORDER BY name")
            return [dict(r) for r in cur.fetchall()]


# ── Determinations ─────────────────────────────────────────────────────────────

def save_determination(data: dict) -> int:
    params = {
        "lender_email": "",
        "company_id": None,
        "user_id": None,
        "county": "",
        "loma_case_number": None,
        "loma_amendment_type": None,
        "loma_effective_date": None,
        "loma_original_zone": None,
        "loma_note": None,
        **data,
        "created_at": datetime.utcnow().isoformat(),
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM determinations WHERE loan_id = %s AND company_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 1",
                (params.get("loan_id"), params.get("company_id")),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE determinations SET
                        borrower_name=%(borrower_name)s, lender_name=%(lender_name)s,
                        lender_email=%(lender_email)s, property_address=%(property_address)s,
                        matched_address=%(matched_address)s, lat=%(lat)s, lon=%(lon)s,
                        flood_zone=%(flood_zone)s, flood_zone_description=%(flood_zone_description)s,
                        sfha_status=%(sfha_status)s, insurance_required=%(insurance_required)s,
                        panel_number=%(panel_number)s, panel_effective_date=%(panel_effective_date)s,
                        community_number=%(community_number)s, community_name=%(community_name)s,
                        determination_date=%(determination_date)s,
                        determination_date_iso=%(determination_date_iso)s,
                        created_at=%(created_at)s, county=%(county)s,
                        loma_case_number=%(loma_case_number)s,
                        loma_amendment_type=%(loma_amendment_type)s,
                        loma_effective_date=%(loma_effective_date)s,
                        loma_original_zone=%(loma_original_zone)s,
                        loma_note=%(loma_note)s
                    WHERE id=%(existing_id)s
                """, {**params, "existing_id": existing["id"]})
                return existing["id"]
            else:
                cur.execute("""
                    INSERT INTO determinations (
                        loan_id, borrower_name, lender_name, lender_email,
                        property_address, matched_address, lat, lon,
                        flood_zone, flood_zone_description, sfha_status, insurance_required,
                        panel_number, panel_effective_date, community_number, community_name,
                        determination_date, determination_date_iso, created_at,
                        company_id, user_id, county,
                        loma_case_number, loma_amendment_type, loma_effective_date,
                        loma_original_zone, loma_note
                    ) VALUES (
                        %(loan_id)s, %(borrower_name)s, %(lender_name)s, %(lender_email)s,
                        %(property_address)s, %(matched_address)s, %(lat)s, %(lon)s,
                        %(flood_zone)s, %(flood_zone_description)s, %(sfha_status)s, %(insurance_required)s,
                        %(panel_number)s, %(panel_effective_date)s, %(community_number)s, %(community_name)s,
                        %(determination_date)s, %(determination_date_iso)s, %(created_at)s,
                        %(company_id)s, %(user_id)s, %(county)s,
                        %(loma_case_number)s, %(loma_amendment_type)s, %(loma_effective_date)s,
                        %(loma_original_zone)s, %(loma_note)s
                    ) RETURNING id
                """, params)
                return cur.fetchone()["id"]


def get_determination(record_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM determinations WHERE id = %s", (record_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_determinations(
    company_id: Optional[int] = None,
    user_id: Optional[int] = None,
    limit: int = 500,
    search: str = "",
) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            clauses, params = [], []
            if company_id is not None:
                clauses.append("company_id = %s"); params.append(company_id)
            if user_id is not None:
                clauses.append("user_id = %s"); params.append(user_id)
            if search:
                like = f"%{search}%"
                clauses.append("(loan_id ILIKE %s OR borrower_name ILIKE %s OR property_address ILIKE %s)")
                params.extend([like, like, like])
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(
                f"SELECT * FROM determinations {where} ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]


def search_determinations(query: str, company_id: Optional[int] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            like = f"%{query}%"
            if company_id is not None:
                cur.execute("""
                    SELECT * FROM determinations
                    WHERE company_id = %s AND (loan_id ILIKE %s OR borrower_name ILIKE %s OR property_address ILIKE %s)
                    ORDER BY created_at DESC LIMIT 100
                """, (company_id, like, like, like))
            else:
                cur.execute("""
                    SELECT * FROM determinations
                    WHERE loan_id ILIKE %s OR borrower_name ILIKE %s OR property_address ILIKE %s
                    ORDER BY created_at DESC LIMIT 100
                """, (like, like, like))
            return [dict(r) for r in cur.fetchall()]


def list_determinations(limit: int = 100, company_id: Optional[int] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if company_id is not None:
                cur.execute(
                    "SELECT * FROM determinations WHERE company_id = %s ORDER BY created_at DESC LIMIT %s",
                    (company_id, limit),
                )
            else:
                cur.execute("SELECT * FROM determinations ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


def list_determinations_admin(
    company_id: Optional[int] = None,
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    query: Optional[str] = None,
    flood_zone: Optional[str] = None,
    limit: int = 200,
) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            clauses, params = [], []
            if company_id is not None:
                clauses.append("company_id = %s"); params.append(company_id)
            if user_id is not None:
                clauses.append("user_id = %s"); params.append(user_id)
            if date_from:
                clauses.append("determination_date_iso >= %s"); params.append(date_from)
            if date_to:
                clauses.append("determination_date_iso <= %s"); params.append(date_to)
            if flood_zone:
                clauses.append("flood_zone = %s"); params.append(flood_zone)
            if query:
                like = f"%{query}%"
                clauses.append("(loan_id ILIKE %s OR borrower_name ILIKE %s OR property_address ILIKE %s)")
                params.extend([like, like, like])
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(
                f"SELECT * FROM determinations {where} ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]


def delete_determination(record_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM determinations WHERE id = %s", (record_id,))
            return cur.rowcount > 0


def bulk_delete_determinations(record_ids: list[int]) -> int:
    if not record_ids:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM determinations WHERE id = ANY(%s)", (record_ids,)
            )
            return cur.rowcount


def set_life_of_loan(record_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE determinations SET life_of_loan = %s WHERE id = %s",
                (1 if enabled else 0, record_id),
            )


def flag_redetermination(record_id: int, needs: bool, checked_date: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE determinations SET needs_redetermination = %s, last_checked_date = %s WHERE id = %s",
                (1 if needs else 0, checked_date, record_id),
            )


def list_monitored() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM determinations WHERE life_of_loan = 1 ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


def count_flagged() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM determinations WHERE needs_redetermination = 1")
            row = cur.fetchone()
            return row["n"] if row else 0


# ── Admin Audit / Deletion Log ─────────────────────────────────────────────────

def init_audit_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_deletion_log (
                    id SERIAL PRIMARY KEY,
                    admin_user_id INTEGER NOT NULL,
                    deleted_record_ids TEXT NOT NULL DEFAULT '[]',
                    deleted_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )
            """)


def log_admin_deletion(admin_user_id: int, record_ids: list[int], reason: str = "") -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_deletion_log (admin_user_id, deleted_record_ids, deleted_at, reason) VALUES (%s, %s, %s, %s)",
                (admin_user_id, json.dumps(record_ids), datetime.utcnow().isoformat(), reason),
            )


def list_admin_deletion_log(limit: int = 200) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM admin_deletion_log ORDER BY deleted_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


# ── LOL Monitoring ─────────────────────────────────────────────────────────────

def init_lol_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lol_monitoring (
                    id SERIAL PRIMARY KEY,
                    company_id INTEGER,
                    user_id INTEGER,
                    determination_id INTEGER,
                    loan_id TEXT NOT NULL DEFAULT '',
                    borrower_name TEXT NOT NULL DEFAULT '',
                    property_address TEXT NOT NULL,
                    lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lol_alerts (
                    id SERIAL PRIMARY KEY,
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


def upsert_lol_monitoring(det: dict) -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM lol_monitoring WHERE determination_id = %s", (det["id"],))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE lol_monitoring SET
                        baseline_panel_number=%s, baseline_effective_date=%s,
                        baseline_flood_zone=%s, baseline_community_number=%s,
                        lender_email=%s, lender_name=%s, status='Active', last_checked_at=%s
                    WHERE id=%s
                """, (
                    det.get("community_number", ""), det.get("panel_effective_date", ""),
                    det.get("flood_zone", ""), det.get("panel_number", ""),
                    det.get("lender_email", ""), det.get("lender_name", ""),
                    now, existing["id"],
                ))
                return existing["id"]
            else:
                cur.execute("""
                    INSERT INTO lol_monitoring (
                        company_id, user_id, determination_id, loan_id, borrower_name,
                        property_address, lat, lon, lender_name, lender_email,
                        baseline_panel_number, baseline_effective_date, baseline_flood_zone,
                        baseline_community_number, status, created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (
                    det.get("company_id"), det.get("user_id"), det.get("id"),
                    det.get("loan_id", ""), det.get("borrower_name", ""),
                    det.get("property_address", ""), det.get("lat"), det.get("lon"),
                    det.get("lender_name", ""), det.get("lender_email", ""),
                    det.get("community_number", ""), det.get("panel_effective_date", ""),
                    det.get("flood_zone", ""), det.get("panel_number", ""),
                    "Active", now,
                ))
                return cur.fetchone()["id"]


def get_lol_monitoring(monitoring_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM lol_monitoring WHERE id = %s", (monitoring_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_lol_monitoring(company_id: Optional[int] = None, status: str = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            clauses, params = [], []
            if company_id is not None:
                clauses.append("company_id = %s"); params.append(company_id)
            if status:
                clauses.append("status = %s"); params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(f"SELECT * FROM lol_monitoring {where} ORDER BY created_at DESC", params)
            return [dict(r) for r in cur.fetchall()]


def list_active_lol_monitoring() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM lol_monitoring WHERE status = 'Active' ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


def close_lol_monitoring(monitoring_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE lol_monitoring SET status = 'Closed' WHERE id = %s", (monitoring_id,))


def update_lol_last_checked(monitoring_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lol_monitoring SET last_checked_at = %s WHERE id = %s",
                (datetime.utcnow().isoformat(), monitoring_id),
            )


def create_lol_alert(
    monitoring_id: int, changed_fields: list, old_values: dict, new_values: dict,
    lender_email: str, email_status: str = "Sent",
) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lol_alerts (monitoring_id, changed_fields, old_values, new_values, alert_sent_at, lender_email, email_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                monitoring_id, json.dumps(changed_fields), json.dumps(old_values),
                json.dumps(new_values), datetime.utcnow().isoformat(), lender_email, email_status,
            ))
            return cur.fetchone()["id"]


def list_lol_alerts(company_id: Optional[int] = None, limit: int = 200) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if company_id is not None:
                cur.execute("""
                    SELECT a.*, m.property_address, m.loan_id, m.borrower_name, m.company_id
                    FROM lol_alerts a JOIN lol_monitoring m ON a.monitoring_id = m.id
                    WHERE m.company_id = %s ORDER BY a.alert_sent_at DESC LIMIT %s
                """, (company_id, limit))
            else:
                cur.execute("""
                    SELECT a.*, m.property_address, m.loan_id, m.borrower_name, m.company_id
                    FROM lol_alerts a JOIN lol_monitoring m ON a.monitoring_id = m.id
                    ORDER BY a.alert_sent_at DESC LIMIT %s
                """, (limit,))
            results = []
            for r in cur.fetchall():
                d = dict(r)
                for key in ("changed_fields", "old_values", "new_values"):
                    try:
                        d[key] = json.loads(d[key])
                    except Exception:
                        pass
                results.append(d)
            return results


def update_alert_email_status(alert_id: int, status: str, retry_count: int = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if retry_count is not None:
                cur.execute(
                    "UPDATE lol_alerts SET email_status = %s, retry_count = %s WHERE id = %s",
                    (status, retry_count, alert_id),
                )
            else:
                cur.execute("UPDATE lol_alerts SET email_status = %s WHERE id = %s", (status, alert_id))


def count_failed_lol_alerts() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM lol_alerts WHERE email_status = 'Failed'")
            row = cur.fetchone()
            return row["n"] if row else 0


# ── User / Auth tables ──────────────────────────────────────────────────────────

def init_auth_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
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
                    approved_at TEXT,
                    password_reset_token TEXT,
                    password_reset_expiry TEXT
                )
            """)
        _run_migrations(conn, [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expiry TEXT",
        ])


def create_user(
    email: str, name: str = "", first_name: str = "", last_name: str = "",
    company_name: str = "", company_address: str = "", contact_number: str = "",
    company_id: Optional[int] = None, reason: str = "", password_hash: str = None,
    status: str = "pending", is_admin: int = 0,
) -> int:
    if not name and (first_name or last_name):
        name = f"{first_name} {last_name}".strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users
                    (email, name, first_name, last_name, company_name, company_address,
                     contact_number, company_id, request_reason, password_hash, status, is_admin, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (email) DO NOTHING
                RETURNING id
            """, (
                email.lower().strip(), name.strip(), first_name.strip(), last_name.strip(),
                company_name.strip(), company_address.strip(), contact_number.strip(),
                company_id, reason, password_hash, status, is_admin,
                datetime.utcnow().isoformat(),
            ))
            row = cur.fetchone()
            return row["id"] if row else 0


def get_user_by_email(email: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_users_by_status(status: str = None, company_id: int = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            clauses, params = [], []
            if status:
                clauses.append("status = %s"); params.append(status)
            if company_id is not None:
                clauses.append("company_id = %s"); params.append(company_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(f"SELECT * FROM users {where} ORDER BY created_at DESC", params)
            return [dict(r) for r in cur.fetchall()]


def approve_user(user_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET status='active', password_hash=%s, approved_at=%s WHERE id=%s",
                (password_hash, datetime.utcnow().isoformat(), user_id),
            )


def set_user_company(user_id: int, company_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET company_id = %s WHERE id = %s", (company_id, user_id))


def reject_user(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status='rejected' WHERE id=%s", (user_id,))


def update_user_password(user_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, user_id))


def get_user_by_reset_token(token: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE password_reset_token = %s", (token,))
            row = cur.fetchone()
            return dict(row) if row else None


def set_reset_token(user_id: int, token: str, expiry: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_reset_token=%s, password_reset_expiry=%s WHERE id=%s",
                (token, expiry, user_id),
            )


def clear_reset_token(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_reset_token=NULL, password_reset_expiry=NULL WHERE id=%s",
                (user_id,),
            )


def set_user_status(user_id: int, status: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status=%s WHERE id=%s", (status, user_id))


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))


def count_pending_users() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE status='pending'")
            row = cur.fetchone()
            return row["n"] if row else 0


def list_users_by_company(company_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE company_id = %s ORDER BY created_at DESC", (company_id,)
            )
            return [dict(r) for r in cur.fetchall()]


# ── LOMA cache table ────────────────────────────────────────────────────────────

def init_loma_table():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loma_records (
                    id SERIAL PRIMARY KEY,
                    case_number TEXT UNIQUE,
                    address TEXT,
                    lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION,
                    original_zone TEXT,
                    outcome_zone TEXT,
                    amendment_type TEXT,
                    effective_date TEXT,
                    source TEXT DEFAULT 'FEMA_API',
                    looked_up_at TIMESTAMP DEFAULT NOW()
                )
            """)
