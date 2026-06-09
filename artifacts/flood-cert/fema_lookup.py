import httpx
from typing import Optional

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

FEMA_NFHL_URL = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"


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
    """Query the FEMA NFHL via ArcGIS Online hosted feature service."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID,STUDY_TYP,SOURCE_CIT",
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
        return {}
    except Exception as e:
        print(f"FEMA NFHL query error: {e}")
        return {}


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
    source_cit = fema_data.get("SOURCE_CIT") or ""

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

    # Derive FIRM panel and community from DFIRM_ID / SOURCE_CIT
    if dfirm_id and len(dfirm_id) >= 6:
        community_number = dfirm_id[:6]
        panel_number = source_cit if source_cit else dfirm_id
    else:
        community_number = "Not Available"
        panel_number = source_cit if source_cit else "Not Available"

    community_name = "See Community FIRM" if dfirm_id else "Not Available"
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
