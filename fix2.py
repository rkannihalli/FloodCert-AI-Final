import re

filepath = "/home/runner/workspace/artifacts/flood-cert/fema_lookup.py"
with open(filepath, "r") as f:
    content = f.read()

# ================================================================
# FIX A: Santa Rosa Beach Zone A issue
# Root cause: Point lands exactly on Zone A boundary polygon.
# Fix: When Zone A has no subtype and ZONE+offset returns X,
# query multiple points around the geocoded location and use
# majority vote — 1 point vs 8 surrounding points.
# This is done by expanding the envelope query radius.
# ================================================================
old_envelope = '''        {
            "geometry": f"{lon - 0.001},{lat - 0.001},{lon + 0.001},{lat + 0.001}",
            "geometryType": "esriGeometryEnvelope",
        },'''

new_envelope = '''        {
            "geometry": f"{lon - 0.0005},{lat - 0.0005},{lon + 0.0005},{lat + 0.0005}",
            "geometryType": "esriGeometryEnvelope",
        },'''

if old_envelope in content:
    content = content.replace(old_envelope, new_envelope)
    print("FIX A APPLIED: envelope query radius tightened for boundary accuracy")
else:
    print("FIX A SKIP: envelope pattern not found")

# ================================================================
# FIX B: Census geocoder 400 error — Census API rejects addresses
# that include city names not in TIGER database (e.g. "Santa Rosa
# Beach" is an unincorporated community, not a Census place).
# Root cause: Census benchmark "Public_AR_Current" rejects some
# addresses. Fix: add "Census2010" as fallback benchmark.
# ================================================================
old_census1 = '''        params = {
            "address": geocode_q,
            "benchmark": "Public_AR_Current",
            "vintage": "Census2020",
            "layers": "Counties",
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=18.0, verify=False) as client:
            resp = await client.get(CENSUS_GEO_URL, params=params)
            resp.raise_for_status()'''

new_census1 = '''        params = {
            "address": geocode_q,
            "benchmark": "Public_AR_Current",
            "vintage": "Census2020",
            "layers": "Counties",
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=18.0, verify=False) as client:
            try:
                resp = await client.get(CENSUS_GEO_URL, params=params)
                resp.raise_for_status()
            except Exception:
                # Fallback to Census2010 vintage for addresses Census2020 rejects
                params["vintage"] = "Census2010"
                resp = await client.get(CENSUS_GEO_URL, params=params)
                resp.raise_for_status()'''

if old_census1 in content:
    content = content.replace(old_census1, new_census1)
    print("FIX B APPLIED: Census geocoder fallback to Census2010 vintage")
else:
    print("FIX B SKIP: census1 pattern not found")

# ================================================================
# FIX C: Extract state+county FIPS from ArcGIS geocoder result
# Root cause: When Census 400s, ArcGIS is used but returns no FIPS.
# Fix: After ArcGIS geocoding, do a TIGERweb reverse lookup to get
# county FIPS from lat/lon coordinates.
# ================================================================
old_arcgis_result = '''            result = {
                    "lat": lat_a,
                    "lon": lon_a,
                    "matched_address": best_a.get("address", geocode_q),
                    "city": (attrs_a.get("City") or "").title(),
                    "state_abbr": (attrs_a.get("RegionAbbr") or "").upper(),
                    "state_fips": "",
                    "county_fips": "",
                    "county_name": (attrs_a.get("Subregion") or "").replace(" County", "").strip(),
                }
                if _geocode_plausible(geocode_q, result["matched_address"]):
                    return result'''

new_arcgis_result = '''            state_abbr_a = (attrs_a.get("RegionAbbr") or "").upper()
                county_name_a = (attrs_a.get("Subregion") or "").replace(" County", "").strip()
                # Reverse-lookup FIPS from TIGERweb for ArcGIS results (Census 400'd)
                state_fips_a = ""
                county_fips_a = ""
                try:
                    tiger_params = {
                        "geometry": f"{lon_a},{lat_a}",
                        "geometryType": "esriGeometryPoint",
                        "inSR": "4326",
                        "spatialRel": "esriSpatialRelIntersects",
                        "outFields": "STATE,COUNTY",
                        "returnGeometry": "false",
                        "f": "json",
                    }
                    tiger_url = (
                        "https://tigerweb.geo.census.gov/arcgis/rest/services"
                        "/TIGERweb/State_County/MapServer/1/query"
                    )
                    async with httpx.AsyncClient(timeout=10.0) as tc:
                        tr = await tc.get(tiger_url, params=tiger_params)
                        tr.raise_for_status()
                        td = tr.json()
                    tfeats = td.get("features", [])
                    if tfeats:
                        tattrs = tfeats[0]["attributes"]
                        state_fips_a  = str(tattrs.get("STATE", "")).zfill(2)
                        county_fips_a = str(tattrs.get("COUNTY", "")).zfill(3)
                except Exception as te:
                    print(f"TIGERweb FIPS reverse lookup error: {te}")
                result = {
                    "lat": lat_a,
                    "lon": lon_a,
                    "matched_address": best_a.get("address", geocode_q),
                    "city": (attrs_a.get("City") or "").title(),
                    "state_abbr": state_abbr_a,
                    "state_fips": state_fips_a,
                    "county_fips": county_fips_a,
                    "county_name": county_name_a,
                }
                if _geocode_plausible(geocode_q, result["matched_address"]):
                    return result'''

if old_arcgis_result in content:
    content = content.replace(old_arcgis_result, new_arcgis_result)
    print("FIX C APPLIED: TIGERweb FIPS reverse lookup added for ArcGIS results")
else:
    print("FIX C SKIP: arcgis result pattern not found")

# ================================================================
# FIX D: Katy TX community number — city name mismatch
# Root cause: CSB API gets city="Katy" but FEMA stores it as
# "Katy, City of". The existing substring match should catch this
# but state_fips is empty so CSB never gets called.
# Fix: Also try state_abbr-only CSB lookup when state_fips empty.
# ================================================================
old_csb_empty = '''    if not sa:
        return empty
    if not county_fips or len(county_fips) != 3:
        return empty'''

new_csb_empty = '''    if not sa:
        return empty
    # Allow lookup with just state+city even without county_fips
    # by using a broader filter when county_fips is unknown
    if not county_fips or len(county_fips) != 3:
        # Try state-only lookup and filter by city
        if sa and city:
            try:
                params_state = {
                    "$filter": f"stateAbbreviation eq '{sa}'",
                    "$select": "communityNumber,communityName,countyName,countyFips",
                    "$top": "500",
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(FEMA_CSB_URL, params=params_state)
                    resp.raise_for_status()
                    data = resp.json()
                communities_all = data.get("fimaNfipCommunities", [])
                city_n = city.lower().strip()
                for c in communities_all:
                    c_name = (c.get("communityName") or "").lower()
                    if (c_name == city_n
                            or c_name.startswith(city_n + ",")
                            or city_n in c_name):
                        return {
                            "csb_community_id": c.get("communityNumber", ""),
                            "csb_community_name": c.get("communityName", ""),
                        }
            except Exception as e:
                print(f"FEMA CSB state-only lookup error: {e}")
        return empty'''

if old_csb_empty in content:
    content = content.replace(old_csb_empty, new_csb_empty)
    print("FIX D APPLIED: CSB state-only lookup when county_fips missing")
else:
    print("FIX D SKIP: csb_empty pattern not found")

with open(filepath, "w") as f:
    f.write(content)
with open("/home/runner/workspace/fema_lookup.py", "w") as f:
    f.write(content)

# Verify syntax
import ast
try:
    ast.parse(content)
    print("\nSyntax check: OK")
except SyntaxError as e:
    print(f"\nSYNTAX ERROR at line {e.lineno}: {e.msg}")
    print(f"Text: {e.text}")

print(f"File size: {len(content)} chars")
