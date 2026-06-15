filepath = "/home/runner/workspace/artifacts/flood-cert/fema_lookup.py"
with open(filepath, "r") as f:
    content = f.read()

# ================================================================
# UNIVERSAL FIX 1: Community Number (CID)
# 
# Current logic constructs fake CIDs like "26161C" or "261610"
# from county FIPS when Layer 22 and CSB both fail.
# These are WRONG — they look like real CIDs but aren't.
#
# Fix: Only return a community number when we have authoritative
# data from Layer 22 or CSB API. Otherwise return "Not Available"
# so browser JS can fill it correctly.
# ================================================================
old_panel_number = '''    if community_id:
        panel_number = community_id
    elif csb_community_id:
        panel_number = csb_community_id
    elif state_fips and len(county_fips) == 3:
        # County-level NFIP CID approximation — may differ for incorporated municipalities
        panel_number = f"{state_fips}{county_fips}0"
    elif esri_dfirm and len(esri_dfirm) >= 5:
        # Use DFIRM_ID directly as 5-digit state+county prefix for CID
        # Append "0" for county-level community (never "C" — that's map number only)
        panel_number = f"{esri_dfirm[:5]}0"
    elif map_number and map_number != "Not Available":
        # Strip "C" suffix from map number to get community number prefix
        raw_panel = map_number.replace(" ", "")
        panel_number = raw_panel[:5] + "0" if raw_panel.endswith("C") else raw_panel[:6]
    else:
        panel_number = "Not Available"'''

new_panel_number = '''    # NFIP Community Number (CID) — only use authoritative sources
    # Never construct from county FIPS — that produces wrong numbers
    # that look real but don't match FEMA's actual community assignments.
    # Layer 22 and CSB API are the only authoritative sources.
    # Browser JS will fill this from Layer 22 when server-side is blocked.
    if community_id:
        # Layer 22 — most authoritative (exact NFIP-assigned 6-digit CID)
        panel_number = community_id
    elif csb_community_id:
        # FEMA CSB API — authoritative municipal community ID
        panel_number = csb_community_id
    else:
        # No authoritative source available — do NOT guess from county FIPS
        # Browser JS enrichment will populate this from Layer 22 at render time
        panel_number = "Not Available"'''

if old_panel_number in content:
    content = content.replace(old_panel_number, new_panel_number)
    print("FIX 1 APPLIED: Community Number no longer guessed from county FIPS")
else:
    print("FIX 1 SKIP: pattern not found — searching...")
    # Find what's there
    idx = content.find("panel_number = community_id")
    if idx > 0:
        print("Context:", repr(content[idx-100:idx+500]))

# ================================================================
# UNIVERSAL FIX 2: Map Number (Community-Panel Number)
#
# Current logic constructs "26161C" from DFIRM_ID or county FIPS.
# This gives only the PREFIX — missing the panel suffix (0243E).
# A prefix-only panel number like "26161C" is MISLEADING because:
# - It looks complete but is missing 5 critical characters
# - The suffix determines the exact map tile
# - Wrong suffix = wrong panel date, wrong flood zone reference
#
# Fix: Only show full panel when Layer 3 returns it.
# Show prefix + note when we only have DFIRM_ID.
# Browser JS fills the complete panel number at render time.
# ================================================================
old_map_number = '''    has_full_l3_panel = len(firm_panel_l3.replace(" ", "")) > 6

    if has_full_l3_panel:
        # Layer 3 returned a real panel number — trust it completely
        map_number = firm_panel_l3
    elif esri_dfirm and len(esri_dfirm) >= 5:
        # DFIRM_ID = state(2) + county(3) FIPS from spatial intersection.
        # Construct standard community-panel prefix: SSCCCС → e.g. "48091C".
        map_number = f"{esri_dfirm[:5]}C"
    elif state_fips and len(county_fips) == 3:
        # Construct prefix from Census geocoder county FIPS (fallback only)
        map_number = f"{state_fips}{county_fips}C"
    else:
        map_number = "Not Available"'''

new_map_number = '''    has_full_l3_panel = len(firm_panel_l3.replace(" ", "")) > 6

    # NFIP Map Number (Community-Panel Number)
    # Only Layer 3 returns the complete panel number with suffix (e.g. "26161C 0243E").
    # DFIRM_ID from Esri Living Atlas gives the county prefix only (e.g. "26161C").
    # Showing prefix-only is misleading — it looks complete but the suffix
    # determines the exact FIRM map panel and effective date.
    # Browser JS enrichment fills the complete panel at render time from Layer 3.
    if has_full_l3_panel:
        # Layer 3 returned complete panel — fully authoritative
        map_number = firm_panel_l3
    elif esri_dfirm and len(esri_dfirm) >= 5:
        # Only county prefix available — browser JS will complete this
        # Store prefix so browser can validate/replace with full panel
        map_number = f"{esri_dfirm[:5]}C"
    else:
        # No spatial data available — browser JS will fill entirely
        map_number = "Not Available"'''

if old_map_number in content:
    content = content.replace(old_map_number, new_map_number)
    print("FIX 2 APPLIED: Map Number no longer constructed from Census county FIPS")
else:
    print("FIX 2 SKIP: pattern not found")
    idx = content.find("has_full_l3_panel")
    if idx > 0:
        print("Context:", repr(content[idx-50:idx+600]))

# ================================================================
# UNIVERSAL FIX 3: TIGERweb FIPS reverse lookup
#
# When Census geocoder returns 400, we have lat/lon but no FIPS.
# TIGERweb can reverse-geocode lat/lon to county FIPS accurately.
# This is the SPATIAL county (where the point actually is),
# not the mailing address county — matching how FEMA assigns panels.
#
# Add TIGERweb FIPS lookup after every geocoder that lacks FIPS.
# ================================================================
old_tigerweb = '''async def query_county_name(lat: float, lon: float) -> dict:
    """TIGERweb county name — fallback only; Census geocoder geography is preferred."""'''

new_tigerweb = '''async def query_tigerweb_fips(lat: float, lon: float) -> dict:
    """Reverse-geocode lat/lon to county FIPS using TIGERweb spatial query.
    
    Returns authoritative state_fips + county_fips based on actual location,
    not mailing address. This is critical for border cities where Census
    geocoder assigns wrong county (e.g. Summerville SC spans two counties).
    Used as universal fallback whenever Census geocoder returns 400.
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "STATE,COUNTY,NAME",
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
        attrs = features[0]["attributes"]
        state_fips  = str(attrs.get("STATE",  "")).zfill(2)
        county_fips = str(attrs.get("COUNTY", "")).zfill(3)
        county_name = (attrs.get("NAME") or "").strip()
        return {
            "state_fips":  state_fips,
            "county_fips": county_fips,
            "county_name": county_name,
        }
    except Exception as e:
        print(f"TIGERweb FIPS reverse lookup error: {e}")
        return {}


async def query_county_name(lat: float, lon: float) -> dict:
    """TIGERweb county name — fallback only; Census geocoder geography is preferred."""'''

if old_tigerweb in content:
    content = content.replace(old_tigerweb, new_tigerweb)
    print("FIX 3 APPLIED: TIGERweb FIPS reverse lookup function added")
else:
    print("FIX 3 SKIP: tigerweb pattern not found")

# ================================================================
# UNIVERSAL FIX 4: Use TIGERweb FIPS in main generate route
#
# After geocoding, if state_fips or county_fips are empty,
# do a TIGERweb reverse lookup to get authoritative FIPS.
# This ensures CSB API always has correct county to query.
# ================================================================
old_gather = '''        zone_data, community_data, firm_data, county_data, csb_data = await asyncio.gather(
            query_fema_nfhl(geo_result["lat"], geo_result["lon"]),
            query_nfip_community(geo_result["lat"], geo_result["lon"]),
            query_firm_panel(geo_result["lat"], geo_result["lon"]),
            query_county_name(geo_result["lat"], geo_result["lon"]),
            query_nfip_community_csb(
                geo_result.get("state_fips", ""),
                geo_result.get("county_fips", ""),
                geo_result.get("city", ""),
                geo_result.get("state_abbr", ""),
            ),
        )'''

new_gather = '''        # If Census geocoder returned no FIPS (400 error), get them from TIGERweb
        # TIGERweb uses spatial intersection — gives correct county for border cities
        if not geo_result.get("state_fips") or not geo_result.get("county_fips"):
            tiger_fips = await query_tigerweb_fips(
                geo_result["lat"], geo_result["lon"]
            )
            if tiger_fips.get("state_fips"):
                geo_result["state_fips"]  = tiger_fips["state_fips"]
                geo_result["county_fips"] = tiger_fips["county_fips"]
                if not geo_result.get("county_name"):
                    geo_result["county_name"] = tiger_fips.get("county_name", "")

        zone_data, community_data, firm_data, county_data, csb_data = await asyncio.gather(
            query_fema_nfhl(geo_result["lat"], geo_result["lon"]),
            query_nfip_community(geo_result["lat"], geo_result["lon"]),
            query_firm_panel(geo_result["lat"], geo_result["lon"]),
            query_county_name(geo_result["lat"], geo_result["lon"]),
            query_nfip_community_csb(
                geo_result.get("state_fips", ""),
                geo_result.get("county_fips", ""),
                geo_result.get("city", ""),
                geo_result.get("state_abbr", ""),
            ),
        )'''

if old_gather in content:
    content = content.replace(old_gather, new_gather)
    print("FIX 4 APPLIED: TIGERweb FIPS used universally when Census geocoder fails")
else:
    print("FIX 4 SKIP: gather pattern not found")
    idx = content.find("query_nfip_community_csb")
    if idx > 0:
        print("Context:", repr(content[idx-200:idx+200]))

# Write files
with open(filepath, "w") as f:
    f.write(content)
with open("/home/runner/workspace/fema_lookup.py", "w") as f:
    f.write(content)

# Syntax check
import ast
try:
    ast.parse(content)
    print("\nSyntax check: OK")
except SyntaxError as e:
    print(f"\nSYNTAX ERROR at line {e.lineno}: {e.msg}")
    print(f"Text: {e.text}")

print(f"File size: {len(content)} chars")
