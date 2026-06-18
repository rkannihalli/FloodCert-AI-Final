"""
Patch to add better panel/community lookup to fema_lookup.py
Run this once to update fema_lookup.py in place
"""
import asyncio, httpx, os, re
from typing import Optional

# ── New: FEMA MSC Search API (not blocked, different host) ──────────────────
FEMA_MSC_SEARCH = "https://msc.fema.gov/arcgis/rest/services/MSC/Products/MapServer/0/query"
FEMA_NFIP_PANEL = "https://msc.fema.gov/arcgis/rest/services/MSC/Products/MapServer/1/query"
FCC_BLOCK_URL   = "https://geo.fcc.gov/api/census/block/find"
REGRID_URL      = "https://app.regrid.com/api/v2/parcels/point"

async def get_fips_from_fcc(lat: float, lon: float) -> dict:
    """FCC Census Block API — authoritative FIPS, free, no key, always reachable."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(FCC_BLOCK_URL, params={
                "latitude": lat, "longitude": lon,
                "censusYear": "2020", "format": "json"
            })
            r.raise_for_status()
            d = r.json()
        state_fips  = (d.get("State",  {}).get("FIPS") or "")[:2]
        county_fips = (d.get("County", {}).get("FIPS") or "")[2:5]
        county_name = (d.get("County", {}).get("name") or "").replace(" County","").strip()
        return {"state_fips": state_fips, "county_fips": county_fips, "county_name": county_name}
    except Exception as e:
        print(f"FCC FIPS error: {e}")
        return {}

async def get_panel_from_msc(lat: float, lon: float) -> dict:
    """
    Query FEMA MSC Products layer for FIRM panel number and effective date.
    Uses msc.fema.gov which is NOT blocked (different host from hazards.fema.gov).
    """
    # Try point query first, then small envelope
    queries = [
        {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint"},
        {"geometry": f"{lon-0.005},{lat-0.005},{lon+0.005},{lat+0.005}",
         "geometryType": "esriGeometryEnvelope"},
    ]
    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FIRM_PAN,EFF_DATE,CASE_NO,STATUS",
            "returnGeometry": "false",
            "resultRecordCount": "5",
            "f": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.get(FEMA_MSC_SEARCH, params=params)
                r.raise_for_status()
                data = r.json()
            features = data.get("features", [])
            if not features:
                continue
            # Prefer effective panels
            best = None
            for f in features:
                attrs = f.get("attributes", {})
                status = (attrs.get("STATUS") or "").upper()
                if "EFFECTIVE" in status or "CURRENT" in status:
                    best = attrs
                    break
            if best is None:
                best = features[0].get("attributes", {})

            raw = (best.get("FIRM_PAN") or "").strip()
            firm_pan = f"{raw[:6]} {raw[6:]}" if len(raw) >= 7 else raw
            eff = best.get("EFF_DATE")
            if isinstance(eff, (int, float)) and eff > 0:
                from datetime import datetime, timezone
                eff_str = datetime.fromtimestamp(eff/1000, tz=timezone.utc).strftime("%m/%d/%Y")
            else:
                eff_str = str(eff) if eff else ""

            if firm_pan:
                print(f"MSC panel found: {firm_pan} eff={eff_str}")
                return {"firm_panel_msc": firm_pan, "eff_date_msc": eff_str}
        except Exception as e:
            print(f"MSC panel query error: {e}")
    return {}

async def get_community_from_openFEMA(state_fips: str, county_fips: str, city: str, state_abbr: str) -> dict:
    """
    OpenFEMA NFIP Communities — already in fema_lookup.py as query_nfip_community_csb.
    This version adds better city matching and county fallback.
    """
    FEMA_CSB = "https://www.fema.gov/api/open/v1/fimaNfipCommunities"
    
    # Build state abbreviation mapping
    STATE_FIPS_MAP = {
        "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT",
        "10":"DE","11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL",
        "18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD",
        "25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE",
        "32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND",
        "39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD",
        "47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV",
        "55":"WI","56":"WY","72":"PR","78":"VI",
    }
    
    sa = state_abbr.upper().strip() or STATE_FIPS_MAP.get(state_fips, "")
    if not sa:
        return {}

    try:
        # Try county-level lookup first (most reliable)
        if county_fips and len(county_fips) == 3:
            params = {
                "$filter": f"stateAbbreviation eq '{sa}' and countyFips eq '{county_fips}'",
                "$select": "communityNumber,communityName,countyName,countyFips",
                "$top": "200",
            }
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(FEMA_CSB, params=params)
                r.raise_for_status()
                communities = r.json().get("fimaNfipCommunities", [])

            city_n = city.lower().strip()
            # 1. Exact city match
            for com in communities:
                nm = (com.get("communityName") or "").lower()
                if nm == city_n or nm.startswith(city_n + ",") or nm.startswith(city_n + " "):
                    return {"csb_community_id": com["communityNumber"],
                            "csb_community_name": com["communityName"]}
            # 2. City substring match
            for com in communities:
                nm = (com.get("communityName") or "").lower()
                if city_n and city_n in nm:
                    return {"csb_community_id": com["communityNumber"],
                            "csb_community_name": com["communityName"]}
            # 3. County/unincorporated fallback
            for com in communities:
                nm = (com.get("communityName") or "").lower()
                if "county" in nm or "unincorporated" in nm:
                    return {"csb_community_id": com["communityNumber"],
                            "csb_community_name": com["communityName"]}
            # 4. First result
            if communities:
                com = communities[0]
                return {"csb_community_id": com["communityNumber"],
                        "csb_community_name": com["communityName"]}
    except Exception as e:
        print(f"OpenFEMA community lookup error: {e}")
    return {}

async def enrich_with_apn(lat: float, lon: float, regrid_key: str = "") -> dict:
    """
    Get APN (parcel number) from Regrid API for cross-reference.
    Only used if REGRID_API_KEY env var is set — optional enhancement.
    Returns parcel data including community info from county assessor.
    """
    if not regrid_key:
        regrid_key = os.environ.get("REGRID_API_KEY", "")
    if not regrid_key:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(REGRID_URL, params={
                "lat": lat, "lon": lon,
                "token": regrid_key,
                "fields": "parcelnumb,address,owner,muni,usedesc,legaldesc"
            })
            r.raise_for_status()
            data = r.json()
        parcels = data.get("parcels", {}).get("features", [])
        if not parcels:
            return {}
        props = parcels[0].get("properties", {}).get("fields", {})
        return {
            "apn": props.get("parcelnumb", ""),
            "apn_address": props.get("address", ""),
            "apn_muni": props.get("muni", ""),
            "apn_owner": props.get("owner", ""),
        }
    except Exception as e:
        print(f"Regrid APN lookup error: {e}")
        return {}

print("Patch module loaded OK")
