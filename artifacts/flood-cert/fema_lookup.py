# ─────────────────────────────────────────────────────────────────────────────
# ADD THIS FUNCTION AT THE BOTTOM OF fema_lookup.py
# ─────────────────────────────────────────────────────────────────────────────

async def check_loma_at_point(lat: float, lon: float) -> dict | None:
    """
    Query FEMA's NFHL Map Amendments layer (Layer 4) for any effective
    LOMA or LOMR at the given coordinates.

    Returns a dict with amendment details if an effective removal is found,
    None if no amendment exists or the query fails.

    Example return value:
    {
        "case_number": "22-06-113A",
        "status": "Effective",
        "outcome_zone": "X",          # "X" means removed from SFHA
        "effective_date": "2022-03-17",
        "amendment_type": "LOMA"      # LOMA, LOMR, or LOMR-F
    }
    """
    import httpx
    from datetime import datetime, timezone

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

        # Take the most recent effective amendment
        attrs = features[0]["attributes"]

        # EFF_DATE is Unix epoch milliseconds — convert to ISO date string
        eff_ms = attrs.get("EFF_DATE")
        eff_date = None
        if eff_ms:
            try:
                eff_date = datetime.fromtimestamp(
                    eff_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except Exception:
                eff_date = None

        return {
            "case_number":     attrs.get("CASE_NO"),
            "status":          attrs.get("STATUS"),       # "Effective"
            "outcome_zone":    attrs.get("OUT_ZONE"),     # "X" = removed from SFHA
            "effective_date":  eff_date,
            "amendment_type":  attrs.get("AMEND_TYPE"),  # LOMA / LOMR / LOMR-F
        }
    except Exception as e:
        print(f"[LOMA] check_loma_at_point failed: {e}")
        return None
