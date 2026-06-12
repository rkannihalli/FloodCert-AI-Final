import httpx, logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)
NFHL_BASE = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer"
LAYER_FLD_HAZ_AR = 28
LAYER_FIRM_PAN = 4
LAYER_POL_AR = 18
SR_WGS84 = 4326
SFHA_ZONES = {
    "A","AE","AH","AO","AR","A99","V","VE",
    "A1","A2","A3","A4","A5","A6","A7","A8","A9","A10",
    "A11","A12","A13","A14","A15","A16","A17","A18","A19","A20",
    "A21","A22","A23","A24","A25","A26","A27","A28","A29","A30",
    "V1","V2","V3","V4","V5","V6","V7","V8","V9","V10",
    "V11","V12","V13","V14","V15","V16","V17","V18","V19","V20",
    "V21","V22","V23","V24","V25","V26","V27","V28","V29","V30",
}

@dataclass
class FEMAResult:
    flood_zone: Optional[str] = None
    sfha: Optional[bool] = None
    insurance_required: Optional[bool] = None
    bfe_ft: Optional[float] = None
    nfip_community_name: Optional[str] = None
    nfip_community_number: Optional[str] = None
    firm_panel_number: Optional[str] = None
    firm_map_date: Optional[str] = None
    data_conflicts: list = field(default_factory=list)

def _query_layer(layer_id, lat, lon, out_fields="*"):
    url = f"{NFHL_BASE}/{layer_id}/query"
    params = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects", "inSR": SR_WGS84,
        "outFields": out_fields, "returnGeometry": "false", "f": "json"}
    try:
        with httpx.Client(timeout=15) as c:
            data = c.get(url, params=params).raise_for_status().json()
        features = data.get("features", [])
        return features[0].get("attributes", {}) if features else None
    except Exception as e:
        logger.error("NFHL layer %d error: %s", layer_id, e); return None

def get_flood_zone(lat, lon):
    attrs = _query_layer(LAYER_FLD_HAZ_AR, lat, lon, "FLD_ZONE,ZONE_SUBTY,STATIC_BFE")
    if attrs is None: return "X", None
    zone = (attrs.get("FLD_ZONE") or "").strip().upper()
    subty = (attrs.get("ZONE_SUBTY") or "").strip().upper()
    bfe_raw = attrs.get("STATIC_BFE")
    if zone == "X" and "500" in subty: zone = "X (Shaded)"
    bfe = None
    try: bfe = float(bfe_raw) if bfe_raw not in (None,-9999,"") else None
    except: pass
    return zone, bfe

def get_firm_panel(lat, lon):
    attrs = _query_layer(LAYER_FIRM_PAN, lat, lon, "FIRM_PAN,EFF_DATE")
    if attrs is None: return None, None
    panel = (attrs.get("FIRM_PAN") or "").strip()
    eff_date = attrs.get("EFF_DATE")
    if isinstance(eff_date, (int, float)) and eff_date > 0:
        from datetime import datetime, timezone
        eff_date = datetime.fromtimestamp(eff_date/1000, tz=timezone.utc).strftime("%m/%d/%Y")
    return panel, eff_date

def get_nfip_community(lat, lon):
    attrs = _query_layer(LAYER_POL_AR, lat, lon, "CID,PCOMM")
    if attrs is None: return None, None
    return (attrs.get("PCOMM") or "").strip(), (attrs.get("CID") or "").strip()

def _detect_conflicts(panel_number, community_id):
    conflicts = []
    if panel_number and community_id:
        if panel_number[:5].strip() != community_id[:5].strip():
            conflicts.append(f"Panel/CID mismatch: {panel_number[:5]} vs {community_id[:5]}. Manual review advised.")
    return conflicts

def lookup_fema_data(lat, lon):
    result = FEMAResult()
    result.flood_zone, result.bfe_ft = get_flood_zone(lat, lon)
    if result.flood_zone:
        result.sfha = result.flood_zone.split()[0].upper() in SFHA_ZONES
        result.insurance_required = result.sfha
    result.firm_panel_number, result.firm_map_date = get_firm_panel(lat, lon)
    result.nfip_community_name, result.nfip_community_number = get_nfip_community(lat, lon)
    result.data_conflicts = _detect_conflicts(result.firm_panel_number, result.nfip_community_number)
    return result


# ── Compatibility aliases for main.py ─────────────────────────────────────────
ZONE_DISPLAY_NAMES = {
    'A': 'Special Flood Hazard Area - Zone A',
    'AE': 'Special Flood Hazard Area - Zone AE',
    'AH': 'Special Flood Hazard Area - Zone AH',
    'AO': 'Special Flood Hazard Area - Zone AO',
    'AR': 'Special Flood Hazard Area - Zone AR',
    'A99': 'Special Flood Hazard Area - Zone A99',
    'V': 'Special Flood Hazard Area - Zone V',
    'VE': 'Special Flood Hazard Area - Zone VE',
    'X': 'Minimal Flood Hazard - Zone X',
    'X (Shaded)': 'Moderate Flood Hazard - Zone X (Shaded)',
    'D': 'Undetermined Flood Hazard - Zone D',
}

def _classify_x_zone(zone, subty=''):
    if zone == 'X' and '500' in str(subty).upper():
        return 'X (Shaded)'
    return zone

def geocode_address(address):
    from geocoder import geocode_address as _geo
    return _geo(address)

def query_fema_nfhl(lat, lon):
    return get_flood_zone(lat, lon)

def query_firm_panel(lat, lon):
    return get_firm_panel(lat, lon)

def query_nfip_community(lat, lon):
    return get_nfip_community(lat, lon)

def query_nfip_community_csb(lat, lon):
    return get_nfip_community(lat, lon)

def query_county_name(lat, lon):
    attrs = _query_layer(LAYER_POL_AR, lat, lon, 'PCOMM,CID')
    if attrs is None:
        return None
    return attrs.get('PCOMM', '')

def nfip_community_info(community_id):
    return {'cid': community_id, 'name': '', 'status': 'participating'}

def determine_flood_info(lat, lon):
    result = lookup_fema_data(lat, lon)
    return {
        'flood_zone': result.flood_zone,
        'sfha': result.sfha,
        'insurance_required': result.insurance_required,
        'bfe_ft': result.bfe_ft,
        'firm_panel': result.firm_panel_number,
        'firm_map_date': result.firm_map_date,
        'community_name': result.nfip_community_name,
        'community_id': result.nfip_community_number,
        'data_conflicts': result.data_conflicts,
    }
