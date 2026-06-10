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
            created_at TEXT NOT NULL
        )
    """)
    # Migrate existing databases that pre-date the lender_email column
    try:
        conn.execute("ALTER TABLE determinations ADD COLUMN lender_email TEXT NOT NULL DEFAULT ''")
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
