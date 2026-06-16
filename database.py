
def create_audit_log_table(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        determination_id INTEGER,
        user_id INTEGER,
        property_address TEXT,
        latitude REAL,
        longitude REAL,
        flood_zone TEXT,
        community_number TEXT,
        panel_number TEXT,
        geocode_source TEXT,
        geocode_confidence INTEGER,
        raw_fema_response TEXT,
        determination_timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
