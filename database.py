def create_audit_log_table(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        determination_id INTEGER,
        user_id INTEGER,
        property_address TEXT,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
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
