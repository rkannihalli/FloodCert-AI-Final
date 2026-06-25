async def check_loma_at_point(lat: float, lon: float) -> dict | None:
    """
    Check for LOMA/LOMR at the given coordinates.
    Strategy:
      1. Check local loma_records DB cache first (handles manually entered LOMAs
         and any previously fetched results) — zero latency, always wins.
      2. Fall back to FEMA NFHL Layer 4 API (catches newly issued amendments).
    Returns a dict if an effective removal from SFHA is found, None otherwise.
    """
    import httpx
    from datetime import datetime, timezone

    # ── 1. Local DB cache ────────────────────────────────────────────────────
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Match within ~111 metres (0.001 decimal degrees)
                cur.execute("""
                    SELECT case_number, outcome_zone, amendment_type,
                           effective_date, original_zone
                    FROM loma_records
                    WHERE ABS(lat - %s) < 0.001
                      AND ABS(lon - %s) < 0.001
                    ORDER BY looked_up_at DESC
                    LIMIT 1
                """, (lat, lon))
                row = cur.fetchone()
                if row:
                    print(f"[LOMA] Cache hit: {row['case_number']} at ({lat},{lon})")
                    return {
                        "case_number":    row["case_number"],
                        "status":         "Effective",
                        "outcome_zone":   row["outcome_zone"],
                        "effective_date": str(row["effective_date"]) if row["effective_date"] else None,
                        "amendment_type": row["amendment_type"],
                    }
    except Exception as e:
        print(f"[LOMA] DB cache check failed (non-fatal): {e}")

    # ── 2. FEMA NFHL Layer 4 API ─────────────────────────────────────────────
    url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/4/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "CASE_NO,STATUS,OUT_ZONE,EFF_DATE,AMEND_TYPE",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            data = r.json()

        features = data.get("features", [])
        if not features:
            return None

        attrs = features[0]["attributes"]

        # EFF_DATE arrives as Unix epoch milliseconds — convert to ISO date
        eff_ms = attrs.get("EFF_DATE")
        eff_date = None
        if eff_ms:
            try:
                eff_date = datetime.fromtimestamp(
                    eff_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except Exception:
                eff_date = None

        result = {
            "case_number":    attrs.get("CASE_NO"),
            "status":         attrs.get("STATUS"),       # "Effective"
            "outcome_zone":   attrs.get("OUT_ZONE"),     # "X" = removed from SFHA
            "effective_date": eff_date,
            "amendment_type": attrs.get("AMEND_TYPE"),  # LOMA / LOMR / LOMR-F
        }

        # Cache the API result in loma_records so future lookups are instant
        if result.get("case_number") and result.get("status") == "Effective":
            try:
                from db import get_conn
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO loma_records
                                (case_number, lat, lon, outcome_zone,
                                 amendment_type, effective_date, source)
                            VALUES (%s, %s, %s, %s, %s, %s, 'FEMA_API')
                            ON CONFLICT (case_number) DO NOTHING
                        """, (
                            result["case_number"], lat, lon,
                            result["outcome_zone"], result["amendment_type"],
                            result["effective_date"],
                        ))
            except Exception as e:
                print(f"[LOMA] Cache write failed (non-fatal): {e}")

        print(f"[LOMA] API result: {result.get('case_number')} status={result.get('status')} zone={result.get('outcome_zone')}")
        return result

    except Exception as e:
        print(f"[LOMA] FEMA API check failed: {e}")
        return None
        
