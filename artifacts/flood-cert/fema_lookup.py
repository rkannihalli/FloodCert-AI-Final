import httpx
from typing import Optional

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

FEMA_NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


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
    """Query FEMA NFHL Layer 28 (FLD_HAZ_AR) — the authoritative flood zone polygon layer."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID,STUDY_TYP,EFF_DATE",
        "returnGeometry": "false",
        "f": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            resp = await client.get(FEMA_NFHL_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {"FLD_ZONE": "UNDETERMINED", "SFHA_TF": "F"}
    except Exception as e:
        print(f"FEMA NFHL query error: {e}")
        return {"FLD_ZONE": "UNDETERMINED", "SFHA_TF": "F"}


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
    """Derive NFIP community context from the stored DFIRM community number.

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

SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}


def determine_flood_info(fema_data: dict) -> dict:
    """Derive flood zone details, SFHA status, and insurance requirement from FEMA NFHL attributes."""
    flood_zone = fema_data.get("FLD_ZONE") or "UNDETERMINED"
    zone_subty = fema_data.get("ZONE_SUBTY") or ""
    sfha_tf_raw = fema_data.get("SFHA_TF")
    dfirm_id = fema_data.get("DFIRM_ID") or ""
    eff_date_raw = fema_data.get("EFF_DATE")

    flood_zone_upper = flood_zone.upper()

    if zone_subty and "0.2" in zone_subty:
        zone_key = "X500"
    else:
        zone_key = flood_zone_upper

    description = FLOOD_ZONE_DESCRIPTIONS.get(
        zone_key,
        f"Flood Zone {flood_zone} — See FIRM panel for details"
    )

    if sfha_tf_raw is not None:
        sfha_bool = str(sfha_tf_raw).upper() in ("T", "TRUE", "YES", "1", "Y")
    else:
        sfha_bool = flood_zone_upper in SFHA_ZONES

    sfha_status = "Yes" if sfha_bool else "No"
    insurance_required = (
        "Yes — Federal mandatory purchase requirement applies"
        if sfha_bool
        else "No — Flood insurance is not federally required"
    )

    # Derive FIRM panel and community from DFIRM_ID
    if dfirm_id and len(dfirm_id) >= 6:
        community_number = dfirm_id[:6]
        panel_number = dfirm_id
    else:
        community_number = "Not Available"
        panel_number = "Not Available"

    community_name = "See Community FIRM" if dfirm_id else "Not Available"

    # EFF_DATE from Layer 28 is a Unix timestamp in milliseconds
    if eff_date_raw and isinstance(eff_date_raw, (int, float)) and eff_date_raw > 0:
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
