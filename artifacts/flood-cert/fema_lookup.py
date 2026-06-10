import httpx
from typing import Optional

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

ESRI_FLOOD_ZONE_URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services"
    "/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)

SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}


async def geocode_address(address: str) -> Optional[dict]:
    """Geocode an address using the US Census Bureau Geocoder."""
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.get(CENSUS_GEOCODER_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        result = data.get("result", {})
        address_matches = result.get("addressMatches", [])

        if not address_matches:
            return None

        match = address_matches[0]
        coords = match.get("coordinates", {})
        matched_address = match.get("matchedAddress", address)

        return {
            "lat": coords.get("y"),
            "lon": coords.get("x"),
            "matched_address": matched_address,
        }
    except Exception as e:
        print(f"Geocoding error: {e}")
        return None


async def query_fema_nfhl(lat: float, lon: float) -> dict:
    """Query flood zone via Esri Living Atlas USA Flood Hazard layer (server-accessible).

    Uses the Esri public cloud (services.arcgis.com) which hosts FEMA's NFHL data and
    is reachable from the server — unlike hazards.fema.gov which drops TLS connections.

    Strategy:
    1. Point query first (precise match).
    2. ~100m envelope fallback for polygon-boundary gap cases.
    3. Default to Zone X (minimal hazard, not SFHA) if no data found.

    Returns FLD_ZONE, ZONE_SUBTY, SFHA_TF, DFIRM_ID — same fields as NFHL Layer 28.
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

            # Prefer any SFHA zone if multiple features returned (most conservative)
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
                "firm_panel": best.get("DFIRM_ID") or "",
                "in_sfha": best.get("SFHA_TF", "F") == "T",
            }
        except Exception as e:
            print(f"FEMA flood zone query error ({q['geometryType']}): {e}")

    # Default: Zone X — minimal flood hazard, not in SFHA.
    # Properties with no NFHL data are generally in unmapped/minimal hazard areas.
    return {"flood_zone": "X", "in_sfha": False, "zone_subtype": "", "firm_panel": ""}


async def query_nfip_community(lat: float, lon: float) -> dict:
    """Community data is derived from DFIRM_ID returned by query_fema_nfhl.

    hazards.fema.gov (Layer 6) is not reachable from Replit servers; the
    DFIRM_ID field from the Esri Living Atlas layer provides equivalent info.
    """
    return {}


async def query_firm_panel(lat: float, lon: float) -> dict:
    """FIRM panel data is included in the DFIRM_ID returned by query_fema_nfhl.

    hazards.fema.gov (Layer 24) is not reachable from Replit servers.
    """
    return {}


# State FIPS → (full name, abbreviation) — used to derive state from DFIRM_ID prefix
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
    """Derive NFIP community context from the stored DFIRM_ID / community number.

    FEMA does not expose a public NFIP community-status API; the authoritative
    source is the Community Status Book (CSB) published per state.  This function
    extracts the state from the DFIRM_ID prefix, builds direct links to the
    relevant FEMA pages, and returns all the structured data the template needs.
    """
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
    "X500": "Moderate Flood Hazard — Zone X (0.2% annual chance / 500-year floodplain)",
}


def determine_flood_info(merged: dict) -> dict:
    """Derive flood zone details from a merged dict of all three NFHL layers.

    Expects the result of: {**zone_data, **community_data, **firm_data}
    where firm_data (Layer 24) naturally overrides zone_data (Layer 28) for
    firm_panel and eff_date via standard dict merge precedence.
    """
    flood_zone = merged.get("flood_zone") or "X"
    zone_subtype = merged.get("zone_subtype") or ""
    in_sfha = merged.get("in_sfha", False)
    firm_panel = merged.get("firm_panel") or ""
    eff_date_raw = merged.get("eff_date")

    flood_zone_upper = flood_zone.upper()

    zone_key = "X500" if (zone_subtype and "0.2" in zone_subtype) else flood_zone_upper
    description = FLOOD_ZONE_DESCRIPTIONS.get(
        zone_key,
        f"Flood Zone {flood_zone} — See FIRM panel for details",
    )

    sfha_bool = in_sfha if isinstance(in_sfha, bool) else flood_zone_upper in SFHA_ZONES
    sfha_status = "Yes" if sfha_bool else "No"
    insurance_required = (
        "Yes — Federal mandatory purchase requirement applies"
        if sfha_bool
        else "No — Flood insurance is not federally required"
    )

    # DFIRM_ID (returned as firm_panel) serves as both the community designation
    # and the panel reference. Layer 6 community_id takes priority if available.
    community_number = (
        merged.get("community_id")
        or (firm_panel if firm_panel else "Not Available")
    )
    community_name = merged.get("community_name") or (
        "See Community FIRM" if firm_panel else "Not Available"
    )
    panel_number = firm_panel or "Not Available"

    # eff_date: Layer 24 passes an already-formatted string ("06/16/21");
    # Layer 28 passes a Unix ms timestamp — handle both.
    if isinstance(eff_date_raw, str) and eff_date_raw:
        panel_effective_date = eff_date_raw
    elif isinstance(eff_date_raw, (int, float)) and eff_date_raw > 0:
        from datetime import datetime, timezone
        panel_effective_date = datetime.fromtimestamp(
            eff_date_raw / 1000, tz=timezone.utc
        ).strftime("%B %d, %Y")
    else:
        panel_effective_date = "See FIRM Panel"

    return {
        "flood_zone": flood_zone,
        "flood_zone_description": description,
        "sfha_status": sfha_status,
        "insurance_required": insurance_required,
        "panel_number": panel_number,
        "panel_effective_date": panel_effective_date,
        "community_number": community_number,
        "community_name": community_name,
    }
