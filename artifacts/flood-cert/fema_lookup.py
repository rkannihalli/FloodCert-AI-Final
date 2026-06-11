import httpx
from typing import Optional

CENSUS_GEO_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_LOC_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"

ESRI_FLOOD_ZONE_URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services"
    "/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)

SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
NFHL_BASE  = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"

TIGERWEB_COUNTY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services"
    "/TIGERweb/State_County/MapServer/1/query"
)


async def geocode_address(address: str) -> Optional[dict]:
    """Geocode a US address.

    Strategy:
    1. Census geographies endpoint → lat/lon + county FIPS + county name.
    2. Census locations endpoint fallback (no county FIPS).
    3. Nominatim (OSM) final fallback for addresses the Census geocoder misses.
    """
    # ── Attempt 1: Census geographies (county FIPS + county name) ─────────────
    try:
        params = {
            "address": address,
            "benchmark": "Public_AR_Current",
            "vintage": "Census2020",
            "layers": "Counties",
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=18.0, verify=False) as client:
            resp = await client.get(CENSUS_GEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        matches = data.get("result", {}).get("addressMatches", [])
        if matches:
            m = matches[0]
            coords = m.get("coordinates", {})
            components = m.get("addressComponents", {})
            counties = m.get("geographies", {}).get("Counties", [])
            geoid = counties[0].get("GEOID", "") if counties else ""
            county_raw = counties[0].get("NAME", "") if counties else ""
            state_fips  = geoid[:2] if len(geoid) >= 5 else ""
            county_fips = geoid[2:] if len(geoid) >= 5 else ""
            return {
                "lat": coords.get("y"),
                "lon": coords.get("x"),
                "matched_address": m.get("matchedAddress", address),
                "city": components.get("city", "").title(),
                "state_abbr": components.get("state", ""),
                "state_fips": state_fips,
                "county_fips": county_fips,
                "county_name": county_raw.strip(),
            }
    except Exception as e:
        print(f"Geocoding (geographies) error: {e}")

    # ── Attempt 2: Census locations endpoint (no county FIPS) ─────────────────
    try:
        params = {
            "address": address,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=18.0, verify=False) as client:
            resp = await client.get(CENSUS_LOC_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        matches = data.get("result", {}).get("addressMatches", [])
        if matches:
            m = matches[0]
            coords = m.get("coordinates", {})
            components = m.get("addressComponents", {})
            return {
                "lat": coords.get("y"),
                "lon": coords.get("x"),
                "matched_address": m.get("matchedAddress", address),
                "city": components.get("city", "").title(),
                "state_abbr": components.get("state", ""),
                "state_fips": "",
                "county_fips": "",
                "county_name": "",
            }
    except Exception as e:
        print(f"Geocoding (locations) error: {e}")

    # ── Attempt 3: Nominatim / OSM fallback ───────────────────────────────────
    try:
        params = {
            "q": address,
            "format": "json",
            "limit": "1",
            "countrycodes": "us",
            "addressdetails": "1",
        }
        headers = {"User-Agent": "FEMA-FloodCert-Generator/1.0"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(NOMINATIM_URL, params=params, headers=headers)
            resp.raise_for_status()
            results = resp.json()

        if results:
            r = results[0]
            addr_detail = r.get("address", {})
            city = (
                addr_detail.get("city")
                or addr_detail.get("town")
                or addr_detail.get("village")
                or ""
            ).title()
            state_abbr = addr_detail.get("state_code", "").upper()
            county_raw = addr_detail.get("county", "").replace(" County", "").strip()
            display = r.get("display_name", address).split(",")[0].strip()
            matched = f"{display}, {addr_detail.get('city', '')}, {state_abbr}".strip(", ")
            return {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "matched_address": matched,
                "city": city,
                "state_abbr": state_abbr,
                "state_fips": "",
                "county_fips": "",
                "county_name": county_raw,
            }
    except Exception as e:
        print(f"Geocoding (Nominatim) error: {e}")

    return None


async def query_fema_nfhl(lat: float, lon: float) -> dict:
    """Query flood zone via Esri Living Atlas USA Flood Hazard layer.

    Returns FLD_ZONE, ZONE_SUBTY, SFHA_TF, DFIRM_ID — same fields as NFHL Layer 28.
    The DFIRM_ID here may reflect the wrong county at municipal/county boundaries;
    use county FIPS from the geocoder to build the correct prefix.
    """
    queries = [
        {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
        },
        {
            "geometry": f"{lon - 0.001},{lat - 0.001},{lon + 0.001},{lat + 0.001}",
            "geometryType": "esriGeometryEnvelope",
        },
    ]

    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID",
            "returnGeometry": "false",
            "resultRecordCount": "10",
            "f": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(ESRI_FLOOD_ZONE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

            features = data.get("features", [])
            if not features:
                continue

            best = None
            for f in features:
                a = f["attributes"]
                zone = (a.get("FLD_ZONE") or "").upper().strip()
                if zone in SFHA_ZONES:
                    best = a
                    break
            if best is None:
                best = features[0]["attributes"]

            return {
                "flood_zone": (best.get("FLD_ZONE") or "X").strip(),
                "zone_subtype": best.get("ZONE_SUBTY") or "",
                "esri_dfirm_id": best.get("DFIRM_ID") or "",
                "in_sfha": best.get("SFHA_TF", "F") == "T",
            }
        except Exception as e:
            print(f"FEMA flood zone query error ({q['geometryType']}): {e}")

    return {"flood_zone": "X", "in_sfha": False, "zone_subtype": "", "esri_dfirm_id": ""}


async def query_nfip_community(lat: float, lon: float) -> dict:
    """Query NFHL Layer 22 (Political Jurisdictions) for NFIP community name and CID.

    hazards.fema.gov is network-blocked server-side on Replit (connection reset).
    We try anyway (in case network rules change); the browser-side JS enrichment
    fetches this independently from Layer 22 and overrides stale values.
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "POL_NAME1,CID",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
            resp = await client.get(f"{NFHL_BASE}/22/query", params=params)
            resp.raise_for_status()
            data = resp.json()
        features = data.get("features", [])
        if not features:
            return {}
        attrs = features[0]["attributes"]
        return {
            "community_id": (attrs.get("CID") or "").strip(),
            "community_name": (attrs.get("POL_NAME1") or "").strip(),
        }
    except Exception as e:
        print(f"NFIP community query (Layer 22) error: {e}")
        return {}


async def query_firm_panel(lat: float, lon: float) -> dict:
    """Query NFHL Layer 3 (FIRM Panels) for full panel number and effective date.

    hazards.fema.gov is network-blocked server-side on Replit (connection reset).
    We try anyway; the browser-side JS enrichment is the reliable fallback.
    Returns the full FIRM_PAN (e.g. "48091C 0215F") and EFF_DATE when reachable.
    """
    queries = [
        {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint"},
        {
            "geometry": f"{lon - 0.002},{lat - 0.002},{lon + 0.002},{lat + 0.002}",
            "geometryType": "esriGeometryEnvelope",
        },
    ]

    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FIRM_PAN,EFF_DATE,DFIRM_ID,PANEL_TYP",
            "returnGeometry": "false",
            "resultRecordCount": "10",
            "f": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
                resp = await client.get(f"{NFHL_BASE}/3/query", params=params)
                resp.raise_for_status()
                data = resp.json()
            features = data.get("features", [])
            if not features:
                continue
            attrs = None
            for f in features:
                if "Panel Printed" in (f["attributes"].get("PANEL_TYP") or ""):
                    attrs = f["attributes"]
                    break
            if attrs is None:
                attrs = features[0]["attributes"]
            raw = (attrs.get("FIRM_PAN") or "").strip()
            firm_pan = f"{raw[:6]} {raw[6:]}" if len(raw) >= 7 else raw
            return {
                "firm_panel_l3": firm_pan or (attrs.get("DFIRM_ID") or ""),
                "eff_date": attrs.get("EFF_DATE"),
            }
        except Exception as e:
            print(f"FIRM panel query (Layer 3, {q['geometryType']}) error: {e}")

    return {}


async def query_county_name(lat: float, lon: float) -> dict:
    """TIGERweb county name — fallback only; Census geocoder geography is preferred."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "NAME",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(TIGERWEB_COUNTY_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        features = data.get("features", [])
        if not features:
            return {}
        raw = (features[0]["attributes"].get("NAME") or "").strip()
        return {"tigerweb_county": raw.title() if raw else ""}
    except Exception as e:
        print(f"County name query (TIGERweb) error: {e}")
        return {}


# State FIPS → (full name, abbreviation)
STATE_FIPS: dict[str, tuple[str, str]] = {
    "01": ("Alabama", "AL"), "02": ("Alaska", "AK"), "04": ("Arizona", "AZ"),
    "05": ("Arkansas", "AR"), "06": ("California", "CA"), "08": ("Colorado", "CO"),
    "09": ("Connecticut", "CT"), "10": ("Delaware", "DE"), "11": ("District of Columbia", "DC"),
    "12": ("Florida", "FL"), "13": ("Georgia", "GA"), "15": ("Hawaii", "HI"),
    "16": ("Idaho", "ID"), "17": ("Illinois", "IL"), "18": ("Indiana", "IN"),
    "19": ("Iowa", "IA"), "20": ("Kansas", "KS"), "21": ("Kentucky", "KY"),
    "22": ("Louisiana", "LA"), "23": ("Maine", "ME"), "24": ("Maryland", "MD"),
    "25": ("Massachusetts", "MA"), "26": ("Michigan", "MI"), "27": ("Minnesota", "MN"),
    "28": ("Mississippi", "MS"), "29": ("Missouri", "MO"), "30": ("Montana", "MT"),
    "31": ("Nebraska", "NE"), "32": ("Nevada", "NV"), "33": ("New Hampshire", "NH"),
    "34": ("New Jersey", "NJ"), "35": ("New Mexico", "NM"), "36": ("New York", "NY"),
    "37": ("North Carolina", "NC"), "38": ("North Dakota", "ND"), "39": ("Ohio", "OH"),
    "40": ("Oklahoma", "OK"), "41": ("Oregon", "OR"), "42": ("Pennsylvania", "PA"),
    "44": ("Rhode Island", "RI"), "45": ("South Carolina", "SC"), "46": ("South Dakota", "SD"),
    "47": ("Tennessee", "TN"), "48": ("Texas", "TX"), "49": ("Utah", "UT"),
    "50": ("Vermont", "VT"), "51": ("Virginia", "VA"), "53": ("Washington", "WA"),
    "54": ("West Virginia", "WV"), "55": ("Wisconsin", "WI"), "56": ("Wyoming", "WY"),
    "60": ("American Samoa", "AS"), "66": ("Guam", "GU"), "69": ("Northern Mariana Islands", "MP"),
    "72": ("Puerto Rico", "PR"), "78": ("U.S. Virgin Islands", "VI"),
}


def nfip_community_info(community_number: str, lat: float = 0.0, lon: float = 0.0) -> dict:
    """Derive NFIP community context from the stored DFIRM_ID / community number."""
    raw = (community_number or "").strip()
    valid = raw not in ("", "0", "N/A", "Not Available")

    state_fips = raw[:2] if valid else ""
    state_name, state_abbr = STATE_FIPS.get(state_fips, ("", ""))

    csb_url = (
        f"https://www.fema.gov/cis/{state_abbr}.html"
        if state_abbr else
        "https://www.fema.gov/flood-insurance/work-with-nfip/community-status"
    )
    msc_url = (
        f"https://msc.fema.gov/portal/search#lonlat={lon},{lat}"
        if lat and lon else
        "https://msc.fema.gov/portal/home"
    )

    return {
        "community_number": raw if valid else "Not Available",
        "state_name": state_name,
        "state_abbr": state_abbr,
        "csb_url": csb_url,
        "msc_url": msc_url,
        "has_state": bool(state_abbr),
    }


FLOOD_ZONE_DESCRIPTIONS = {
    "A": "Special Flood Hazard Area — Zone A (1% annual chance flood, no BFE determined)",
    "AE": "Special Flood Hazard Area — Zone AE (1% annual chance flood, BFE determined)",
    "AH": "Special Flood Hazard Area — Zone AH (1% annual chance shallow flooding, ponding)",
    "AO": "Special Flood Hazard Area — Zone AO (1% annual chance shallow flooding, sheet flow)",
    "AR": "Special Flood Hazard Area — Zone AR (being restored to BFE via flood control system)",
    "A99": "Special Flood Hazard Area — Zone A99 (protected by federal flood control system under construction)",
    "V": "Special Flood Hazard Area — Zone V (coastal flood with velocity/wave action hazard)",
    "VE": "Special Flood Hazard Area — Zone VE (coastal flood with velocity hazard, BFE determined)",
    "B": "Moderate Flood Hazard Area — Zone B (0.2% annual chance flood, between 100 and 500 year flood)",
    "C": "Minimal Flood Hazard Area — Zone C (outside 500-year floodplain)",
    "D": "Undetermined Flood Hazard — Zone D (possible but undetermined flood hazard)",
    "X": "Minimal Flood Hazard — Zone X (outside 500-year floodplain or protected by levee from 100-year flood)",
    "X500": "Moderate Flood Hazard — Zone X (Shaded) — 0.2% annual chance / 500-year floodplain",
}


def determine_flood_info(merged: dict) -> dict:
    """Derive flood zone details from merged NFHL query results.

    Expects: {**zone_data, **community_data, **firm_data, **county_data, geocoded_city, state_fips, county_fips}
    """
    flood_zone      = (merged.get("flood_zone") or "X").strip().upper()
    zone_subtype    = (merged.get("zone_subtype") or "").strip()
    in_sfha         = merged.get("in_sfha", False)
    esri_dfirm      = (merged.get("esri_dfirm_id") or "").strip()
    firm_panel_l3   = (merged.get("firm_panel_l3") or "").strip()  # from Layer 3 (may be empty)
    eff_date_raw    = merged.get("eff_date")
    state_fips      = (merged.get("state_fips") or "").strip()
    county_fips     = (merged.get("county_fips") or "").strip()
    community_id    = (merged.get("community_id") or "").strip()
    community_nm    = (merged.get("community_name") or "").strip()
    geocoded_city   = (merged.get("geocoded_city") or "").strip()

    # ── Zone designation ──────────────────────────────────────────────────────
    # Shaded Zone X (0.2% annual chance): stored and displayed as "X500"
    is_x500 = flood_zone == "X" and bool(zone_subtype and "0.2" in zone_subtype)
    flood_zone_out = "X500" if is_x500 else flood_zone
    zone_key = flood_zone_out
    description = FLOOD_ZONE_DESCRIPTIONS.get(
        zone_key,
        f"Flood Zone {flood_zone_out} — See FIRM panel for details",
    )

    sfha_bool = in_sfha if isinstance(in_sfha, bool) else flood_zone in SFHA_ZONES
    sfha_status = "Yes" if sfha_bool else "No"
    insurance_required = (
        "Yes — Federal mandatory purchase requirement applies"
        if sfha_bool
        else "No — Flood insurance is not federally required"
    )

    # ── NFIP Map Number (Community-Panel Number) ──────────────────────────────
    # Priority:
    # 1. Layer 3 full panel (e.g. "48091C 0215F") — most accurate; has correct DFIRM_ID + suffix
    # 2. County FIPS from geocoder → construct correct community prefix (e.g. "48091C")
    # 3. DFIRM_ID from Esri Living Atlas — may reflect wrong county near boundaries
    # 4. "Not Available"
    has_full_l3_panel = len(firm_panel_l3.replace(" ", "")) > 6

    if has_full_l3_panel:
        # Layer 3 returned a real panel number — trust it completely
        map_number = firm_panel_l3
    elif state_fips and len(county_fips) == 3:
        # Construct correct prefix from Census geocoder county FIPS
        # Note: the panel suffix (e.g. "0215F") requires Layer 3;
        # without it we show just the community prefix and let JS enrich the suffix.
        map_number = f"{state_fips}{county_fips}C"
    elif esri_dfirm:
        map_number = esri_dfirm[:6] if len(esri_dfirm) >= 6 else esri_dfirm
    else:
        map_number = "Not Available"

    # ── NFIP Community Number (CID) ───────────────────────────────────────────
    # Priority:
    # 1. CID from Layer 22 — the authoritative NFIP-assigned 6-digit community ID
    # 2. County FIPS-derived approximation for county jurisdiction (state + county FIPS padded)
    #    Note: for incorporated cities this will be the county CID, not the city CID;
    #    the client-side JS Layer 22 enrichment provides the correct municipal CID.
    # 3. "Not Available"
    if community_id:
        panel_number = community_id
    elif state_fips and len(county_fips) == 3:
        # County-level NFIP CID approximation: state(2) + county(3) + "0" = 6 digits
        # This is a best-effort value for county jurisdictions; municipal CIDs differ.
        panel_number = f"{state_fips}{county_fips}0"
    elif map_number and map_number != "Not Available":
        # Fall back to map number prefix (first 5-6 chars)
        panel_number = map_number.replace(" ", "")[:6]
    else:
        panel_number = "Not Available"

    # ── NFIP Community Name ───────────────────────────────────────────────────
    # Priority: Layer 22 POL_NAME1 → geocoded city → "Not Available"
    community_name_out = community_nm or geocoded_city or "Not Available"

    # ── County name ───────────────────────────────────────────────────────────
    # Priority: Census geocoder geography → TIGERweb fallback
    county_name = (merged.get("county_name") or "").strip()  # from geocoder geography
    if not county_name:
        county_name = (merged.get("tigerweb_county") or "").strip()

    # ── NFIP Map Panel Effective/Revised Date ─────────────────────────────────
    if isinstance(eff_date_raw, str) and eff_date_raw:
        panel_effective_date = eff_date_raw
    elif isinstance(eff_date_raw, (int, float)) and eff_date_raw > 0:
        from datetime import datetime, timezone
        panel_effective_date = datetime.fromtimestamp(
            eff_date_raw / 1000, tz=timezone.utc
        ).strftime("%m/%d/%Y")
    else:
        panel_effective_date = "See FEMA Map Service Center"

    return {
        "flood_zone": flood_zone_out,
        "flood_zone_description": description,
        "sfha_status": sfha_status,
        "insurance_required": insurance_required,
        "panel_number": panel_number,          # NFIP Community Number (CID)
        "panel_effective_date": panel_effective_date,
        "community_number": map_number,        # NFIP Map Number (Community-Panel Number)
        "community_name": community_name_out,
        "county": county_name,
    }
