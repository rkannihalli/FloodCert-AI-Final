import re
import httpx
from typing import Optional

CENSUS_GEO_URL  = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_LOC_URL  = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
ARCGIS_GEO_URL  = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
    "/findAddressCandidates"
)
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
PHOTON_URL     = "https://photon.komoot.io/api/"

_LOT_PATTERN = re.compile(
    r",?\s+(?:Lot|Parcel|Tract)\s*[\w-]+\b",
    re.IGNORECASE,
)

_STREET_TYPE_RE = re.compile(
    r"\b(Drive|Dr|Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|"
    r"Court|Ct|Circle|Cir|Place|Pl|Way|Terrace|Ter|Trail|Trl|"
    r"Parkway|Pkwy|Highway|Hwy)\b\.?",
    re.IGNORECASE,
)

ESRI_FLOOD_ZONE_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"

SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
NFHL_BASE  = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"

CT_PLANNING_REGION_TO_COUNTY: dict[str, str] = {
    "capitol planning region":                       "Hartford County",
    "greater bridgeport planning region":            "Fairfield County",
    "lower connecticut river valley planning region":"Middlesex County",
    "naugatuck valley planning region":              "New Haven County",
    "northeastern connecticut planning region":      "Windham County",
    "northwest hills planning region":               "Litchfield County",
    "south central connecticut planning region":     "New Haven County",
    "south central connecticut":                     "New Haven County",
    "southeastern connecticut planning region":      "New London County",
    "western connecticut planning region":           "Fairfield County",
}

FEMA_CSB_URL = "https://www.fema.gov/api/open/v2/fimaNfipCommunities"

_X500_SUBTYPES = frozenset({
    "0.2 PCT ANNUAL CHANCE FLOOD HAZARD",
    "0.2 PCT ANNUAL CHANCE FLOOD",
    "0.2% ANNUAL CHANCE FLOOD HAZARD",
    "0.2 PERCENT ANNUAL CHANCE FLOOD HAZARD",
    "AREA OF 500-YEAR FLOOD HAZARD",
    "500-YEAR FLOOD HAZARD",
    "500 YEAR FLOOD HAZARD",
    "SHADED ZONE X",
    "0.2 PCT ANNUAL CHANCE",
    "0.2 PERCENT ANNUAL CHANCE",
    "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE",
    "REDUCED FLOOD RISK DUE TO LEVEE",
    "PROTECTED BY LEVEE",
    "AREA PROTECTED BY LEVEE",
    "AREA PROTECTED FROM 100-YEAR FLOOD BY LEVEE",
})


def _classify_x_zone(flood_zone: str, zone_subtype: str) -> str:
    if flood_zone in ("X500", "B"):
        return "X500"
    if flood_zone != "X":
        return flood_zone

    sub = zone_subtype.upper().strip()
    if not sub:
        return "X"

    if sub in _X500_SUBTYPES:
        return "X500"
    if "0.2" in sub:
        return "X500"
    if "500" in sub and ("ANNUAL" in sub or "YEAR" in sub or "CHANCE" in sub):
        return "X500"
    if "LEVEE" in sub:
        return "X500"

    return "X"

TIGERWEB_COUNTY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services"
    "/TIGERweb/State_County/MapServer/1/query"
)


def _geocode_plausible(input_address: str, matched_address: str) -> bool:
    if not matched_address:
        return True

    inp = input_address.upper()
    mat = matched_address.upper()

    num_m = re.match(r"\s*(\d+)", inp)
    house_num = num_m.group(1) if num_m else ""

    street_part = re.sub(r"^\s*\d+\s*", "", inp.split(",")[0])
    _STOP = {
        "DR", "ST", "AVE", "BLVD", "RD", "LN", "CT", "CIR", "PL", "WAY",
        "TER", "TRL", "PKWY", "HWY", "DRIVE", "STREET", "AVENUE",
        "BOULEVARD", "ROAD", "LANE", "COURT", "CIRCLE", "PLACE",
        "TERRACE", "TRAIL", "PARKWAY", "HIGHWAY", "UNIT", "APT", "STE",
    }
    sig_words = [w for w in re.findall(r"\b[A-Z]{2,}\b", street_part) if w not in _STOP]
    primary_word = sig_words[0] if sig_words else ""

    zip_m = re.search(r"\b(\d{5})\b", inp)
    zip_code = zip_m.group(1) if zip_m else ""

    house_ok  = bool(house_num)    and house_num    in mat
    street_ok = bool(primary_word) and primary_word in mat
    zip_ok    = bool(zip_code)     and zip_code     in mat

    if house_ok and (street_ok or zip_ok):
        return True
    if street_ok:
        return True
    if not house_num and not primary_word and not zip_code:
        return True
    if house_num and not house_ok and not street_ok:
        return False
    return True


_PHOTON_OK_KEYS = frozenset({"building", "highway", "place", "addr"})


async def geocode_address(address: str) -> Optional[dict]:
    """Geocode a US address to lat/lon + county FIPS.

    Fallback chain:
    1. Census geographies — lat/lon + county FIPS + county name
    2. Census locations  — lat/lon only
    3. ArcGIS World Geocoder
    4. Photon (OSM building polygon centroids)
    5. Nominatim / OSM
    6. Nominatim with street type stripped
    7. Minimal query (house number + city + state + ZIP)
    """
    geocode_q = re.sub(r"\s{2,}", " ", _LOT_PATTERN.sub("", address)).strip()

    # ── Attempt 1: Census geographies ────────────────────────────────────────
    try:
        params = {
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
                params["vintage"] = "Census2010"
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
            result = {
                "lat": coords.get("y"),
                "lon": coords.get("x"),
                "matched_address": m.get("matchedAddress", address),
                "city": components.get("city", "").title(),
                "state_abbr": components.get("state", ""),
                "state_fips": state_fips,
                "county_fips": county_fips,
                "county_name": county_raw.strip(),
            }
            if _geocode_plausible(geocode_q, result["matched_address"]):
                return result
            print(f"Geocoding (geographies) plausibility rejected: {result['matched_address']!r}")
    except Exception as e:
        print(f"Geocoding (geographies) error: {e}")

    # ── Attempt 2: Census locations ───────────────────────────────────────────
    try:
        params = {
            "address": geocode_q,
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
            result = {
                "lat": coords.get("y"),
                "lon": coords.get("x"),
                "matched_address": m.get("matchedAddress", address),
                "city": components.get("city", "").title(),
                "state_abbr": components.get("state", ""),
                "state_fips": "",
                "county_fips": "",
                "county_name": "",
            }
            if _geocode_plausible(geocode_q, result["matched_address"]):
                return result
            print(f"Geocoding (locations) plausibility rejected: {result['matched_address']!r}")
    except Exception as e:
        print(f"Geocoding (locations) error: {e}")

    # ── Attempt 3: ArcGIS World Geocoder ─────────────────────────────────────
    try:
        params = {
            "SingleLine": geocode_q,
            "f": "json",
            "outFields": "Addr_type,Match_addr,RegionAbbr,Subregion,City",
            "maxLocations": "3",
            "forStorage": "false",
        }
        headers = {"User-Agent": "FEMA-FloodCert-Generator/1.0"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(ARCGIS_GEO_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        _ARCGIS_PRECISE = {"PointAddress", "Subaddress", "BuildingName", "StreetAddress"}
        best_a = None
        for c in data.get("candidates", []):
            score     = c.get("score", 0)
            addr_type = (c.get("attributes") or {}).get("Addr_type", "")
            if score >= 85 and addr_type in _ARCGIS_PRECISE:
                best_a = c
                break

        if best_a:
            loc     = best_a.get("location", {})
            lat_a   = float(loc.get("y", 0))
            lon_a   = float(loc.get("x", 0))
            attrs_a = best_a.get("attributes") or {}
            result = {
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
                return result
            print(f"Geocoding (ArcGIS) plausibility rejected: {result['matched_address']!r}")
    except Exception as e:
        print(f"Geocoding (ArcGIS) error: {e}")

    # ── Attempt 4: Photon ─────────────────────────────────────────────────────
    try:
        params = {
            "q": geocode_q,
            "limit": "5",
            "countrycode": "us",
            "lang": "en",
        }
        headers = {"User-Agent": "FEMA-FloodCert-Generator/1.0"}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(PHOTON_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        best = None
        for f in features:
            osm_value = (f.get("properties", {}).get("osm_value") or "").lower()
            if osm_value in ("house", "residential", "detached", "apartments",
                             "yes", "building", "terrace", "semi", "bungalow"):
                best = f
                break
        if best is None:
            for f in features:
                osm_key = (f.get("properties", {}).get("osm_key") or "").lower()
                if osm_key in _PHOTON_OK_KEYS:
                    best = f
                    break

        if best:
            props = best.get("properties", {})
            coords_p = best.get("geometry", {}).get("coordinates", [])
            if len(coords_p) >= 2:
                lon_p, lat_p = float(coords_p[0]), float(coords_p[1])
                city_p = (
                    props.get("city") or props.get("locality") or props.get("village") or ""
                ).title()
                state_p = props.get("state_code") or props.get("state") or ""
                state_abbr_p = (state_p[:2] if len(state_p) >= 2 else state_p).upper()
                county_p = props.get("county", "").replace(" County", "").strip()
                hn = props.get("housenumber", "")
                st = props.get("street", "")
                pc = props.get("postcode", "")
                matched_p = f"{hn} {st}, {city_p}, {state_abbr_p} {pc}".strip(", ")
                result = {
                    "lat": lat_p,
                    "lon": lon_p,
                    "matched_address": matched_p or address,
                    "city": city_p,
                    "state_abbr": state_abbr_p,
                    "state_fips": "",
                    "county_fips": "",
                    "county_name": county_p,
                }
                if _geocode_plausible(geocode_q, result["matched_address"]):
                    return result
                print(f"Geocoding (Photon) plausibility rejected: {result['matched_address']!r} "
                      f"(osm_key={props.get('osm_key')}, osm_value={props.get('osm_value')})")
    except Exception as e:
        print(f"Geocoding (Photon) error: {e}")

    # ── Attempt 5: Nominatim ──────────────────────────────────────────────────
    try:
        params = {
            "q": geocode_q,
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
            result = {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "matched_address": matched,
                "city": city,
                "state_abbr": state_abbr,
                "state_fips": "",
                "county_fips": "",
                "county_name": county_raw,
            }
            if _geocode_plausible(geocode_q, result["matched_address"]):
                return result
            print(f"Geocoding (Nominatim) plausibility rejected: {result['matched_address']!r}")
    except Exception as e:
        print(f"Geocoding (Nominatim) error: {e}")

    # ── Attempt 6: Nominatim with street type stripped ────────────────────────
    simplified = re.sub(r"\s{2,}", " ", _STREET_TYPE_RE.sub("", geocode_q)).strip()
    if simplified and simplified.lower() != geocode_q.lower():
        try:
            params = {
                "q": simplified,
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
                result = {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "matched_address": matched,
                    "city": city,
                    "state_abbr": state_abbr,
                    "state_fips": "",
                    "county_fips": "",
                    "county_name": county_raw,
                }
                if _geocode_plausible(geocode_q, result["matched_address"]):
                    return result
                print(f"Geocoding (Nominatim normalized) plausibility rejected: "
                      f"{result['matched_address']!r}")
        except Exception as e:
            print(f"Geocoding (Nominatim normalized) error: {e}")

    # ── Attempt 7: Minimal query ──────────────────────────────────────────────
    try:
        num_m2 = re.match(r"\s*(\d+)", geocode_q)
        parts  = [p.strip() for p in geocode_q.split(",")]
        if num_m2 and len(parts) >= 2:
            city_state_zip = ", ".join(parts[1:])
            minimal_q = f"{num_m2.group(1)} {city_state_zip}"
            params = {
                "q": minimal_q,
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
                zip_r = addr_detail.get("postcode", "")
                result = {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "matched_address": address,
                    "city": city,
                    "state_abbr": state_abbr,
                    "state_fips": "",
                    "county_fips": "",
                    "county_name": county_raw,
                }
                if zip_r and zip_r in geocode_q:
                    print(f"Geocoding (minimal ZIP fallback) accepted for: {address!r}")
                    return result
    except Exception as e:
        print(f"Geocoding (minimal ZIP fallback) error: {e}")

    return None


async def _query_zone_at_point(client: httpx.AsyncClient, lon: float, lat: float) -> dict:
    """Query NFHL flood zone at a single point."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID",
        "returnGeometry": "false",
        "resultRecordCount": "5",
        "f": "json",
    }
    try:
        resp = await client.get(ESRI_FLOOD_ZONE_URL, params=params)
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return {}
        # Prefer SFHA zone if present
        for f in features:
            if (f["attributes"].get("FLD_ZONE") or "").upper() in SFHA_ZONES:
                return f["attributes"]
        return features[0]["attributes"]
    except Exception:
        return {}


async def query_fema_nfhl(lat: float, lon: float) -> dict:
    """Query flood zone via FEMA NFHL Layer 28.
    
    Uses multi-point sampling to handle properties near zone boundaries.
    Samples the geocoded point plus 4 small offsets (~30m each).
    If majority of samples are non-SFHA, returns non-SFHA result.
    """
    # Small offset ~30 metres at mid-latitudes
    D = 0.0003
    offsets = [(0, 0), (D, 0), (-D, 0), (0, D), (0, -D)]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            results = []
            for dlat, dlon in offsets:
                r = await _query_zone_at_point(client, lon + dlon, lat + dlat)
                if r:
                    results.append(r)

        if not results:
            return {"flood_zone": "X", "in_sfha": False, "zone_subtype": "", "esri_dfirm_id": ""}

        # Count SFHA vs non-SFHA votes
        sfha_results = [r for r in results if r.get("SFHA_TF") == "T"]
        non_sfha_results = [r for r in results if r.get("SFHA_TF") != "T"]

        # Use majority vote — if more points are non-SFHA, use non-SFHA
        # This handles boundary cases where geocoded point is just inside SFHA
        if len(non_sfha_results) > len(sfha_results):
            best = non_sfha_results[0]
            print(f"[NFHL] Boundary detected: {len(sfha_results)} SFHA vs {len(non_sfha_results)} non-SFHA — using non-SFHA")
        elif sfha_results:
            best = sfha_results[0]
        else:
            best = results[0]

        return {
            "flood_zone": (best.get("FLD_ZONE") or "X").strip(),
            "zone_subtype": best.get("ZONE_SUBTY") or "",
            "esri_dfirm_id": best.get("DFIRM_ID") or "",
            "in_sfha": best.get("SFHA_TF", "F") == "T",
        }
    except Exception as e:
        print(f"FEMA flood zone query error: {e}")

    return {"flood_zone": "X", "in_sfha": False, "zone_subtype": "", "esri_dfirm_id": ""}


async def _query_nfhl_at_offset(lat: float, lon: float, dlat: float, dlon: float) -> dict:
    """Query NFHL at a specific offset from the geocoded point."""
    params = {
        "geometry": f"{lon+dlon},{lat+dlat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID",
        "returnGeometry": "false",
        "resultRecordCount": "5",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(ESRI_FLOOD_ZONE_URL, params=params)
            data = resp.json()
        features = data.get("features", [])
        if not features:
            return {"flood_zone": "X", "in_sfha": False}
        best = None
        for f in features:
            if (f["attributes"].get("FLD_ZONE") or "").upper() in SFHA_ZONES:
                best = f["attributes"]
                break
        if best is None:
            best = features[0]["attributes"]
        return {
            "flood_zone": (best.get("FLD_ZONE") or "X").strip(),
            "zone_subtype": best.get("ZONE_SUBTY") or "",
            "in_sfha": best.get("SFHA_TF", "F") == "T",
            "esri_dfirm_id": best.get("DFIRM_ID") or "",
        }
    except Exception:
        return {"flood_zone": "X", "in_sfha": False}


async def query_nfip_community(lat: float, lon: float) -> dict:
    """Query NFHL Layer 22 (Political Jurisdictions) for NFIP community name and CID.
    
    Strategy:
    1. Point query — most precise
    2. Small envelope fallback — catches edge cases near boundaries
    3. Prefer city/town/village over county results
    """
    queries = [
        {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
        },
        {
            "geometry": f"{lon-0.005},{lat-0.005},{lon+0.005},{lat+0.005}",
            "geometryType": "esriGeometryEnvelope",
        },
        {
            "geometry": f"{lon-0.02},{lat-0.02},{lon+0.02},{lat+0.02}",
            "geometryType": "esriGeometryEnvelope",
        },
    ]
    
    _COUNTY_WORDS = {"county", "parish", "borough", "unincorporated", "areas"}
    
    def _is_city_level(name: str) -> bool:
        """Return True if this is a city/town/village — not a county."""
        n = name.lower()
        return not any(w in n for w in _COUNTY_WORDS)
    
    all_features = []
    
    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "POL_NAME1,CID",
            "returnGeometry": "false",
            "resultRecordCount": "10",
            "f": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
                resp = await client.get(f"{NFHL_BASE}/22/query", params=params)
                resp.raise_for_status()
                data = resp.json()
            features = data.get("features", [])
            if features:
                all_features.extend(features)
                break  # Got results, stop trying wider queries
        except Exception as e:
            print(f"NFIP community query (Layer 22) error ({q['geometryType']}): {e}")
            continue
    
    if not all_features:
        return {}
    
    # Deduplicate by CID
    seen = set()
    unique = []
    for f in all_features:
        cid = (f["attributes"].get("CID") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(f)
    
    # Prefer city/town/village over county
    city_features = [f for f in unique if _is_city_level(f["attributes"].get("POL_NAME1") or "")]
    best = city_features[0] if city_features else unique[0]
    
    attrs = best["attributes"]
    result = {
        "community_id": (attrs.get("CID") or "").strip(),
        "community_name": (attrs.get("POL_NAME1") or "").strip(),
    }
    print(f"NFIP community (Layer 22): {result['community_id']} / {result['community_name']}")
    return result


async def query_firm_panel(lat: float, lon: float, county_fips: str = "", community_id: str = "") -> dict:
    """Query NFHL Layer 3 (FIRM Panels) for full panel number and effective date."""
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

            # Filter priority: community_id numeric match > county_fips > state prefix
            # DFIRM_ID format: SSCCCX (state+county FIPS + letter) e.g. 09015C
            # community_id format: SSCCCC (6 digit community number) e.g. 090119
            # They share first 2 digits (state FIPS) but differ after that
            matched = False
            if community_id and len(community_id) >= 6:
                # Try exact 6-char community CID match first (e.g. 240010)
                filtered = [
                    f for f in features
                    if (f["attributes"].get("DFIRM_ID") or "").startswith(community_id[:6])
                ]
                if filtered:
                    features = filtered
                    matched = True
                    print(f"FIRM panel matched community CID {community_id[:6]}: "
                          f"{features[0]['attributes'].get('FIRM_PAN','')}")

            if not matched and community_id and len(community_id) >= 2:
                # Use state prefix from community_id + county_fips for narrower match
                state_from_cid = community_id[:2]
                if county_fips and len(county_fips) >= 5:
                    county_prefix = county_fips[:5]
                    filtered = [
                        f for f in features
                        if (f["attributes"].get("DFIRM_ID") or "").startswith(county_prefix)
                    ]
                    if filtered:
                        features = filtered
                        matched = True
                        print(f"FIRM panel matched county {county_prefix}: "
                              f"{features[0]['attributes'].get('FIRM_PAN','')}")
                if not matched:
                    # State-only filter as last resort
                    filtered = [
                        f for f in features
                        if (f["attributes"].get("DFIRM_ID") or "").startswith(state_from_cid)
                    ]
                    if filtered:
                        features = filtered
                        matched = True

            if not matched and county_fips:
                county_prefix = county_fips[:5] if len(county_fips) >= 5 else county_fips
                filtered = [
                    f for f in features
                    if (f["attributes"].get("DFIRM_ID") or "").startswith(county_prefix)
                ]
                if not filtered and len(county_fips) >= 2:
                    filtered = [
                        f for f in features
                        if (f["attributes"].get("DFIRM_ID") or "").startswith(county_prefix[:2])
                    ]
                if filtered:
                    features = filtered
                    print(f"FIRM panel matched county_fips {county_prefix}: "
                          f"{features[0]['attributes'].get('FIRM_PAN','')}")

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


async def query_tigerweb_fips(lat: float, lon: float) -> dict:
    """Reverse-geocode lat/lon to county FIPS using TIGERweb spatial query."""
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
        print(f"TIGERweb FIPS: {state_fips}{county_fips} ({county_name})")
        return {
            "state_fips":  state_fips,
            "county_fips": county_fips,
            "county_name": county_name,
        }
    except Exception as e:
        print(f"TIGERweb FIPS reverse lookup error: {e}")
        return {}


async def query_county_name(lat: float, lon: float) -> dict:
    """TIGERweb county name — fallback only."""
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


async def query_nfip_community_csb(
    state_fips: str,
    county_fips: str,
    city: str,
    state_abbr: str = "",
    community_id_from_layer22: str = "",
) -> dict:
    """Look up NFIP community name and number.
    Priority:
      1. Local nfip_communities_db.json (fast, offline)
      2. FEMA CSB API (fallback, may be unavailable)
    """
    import os, json as _json
    empty: dict = {"csb_community_id": "", "csb_community_name": ""}

    sa = state_abbr.upper().strip()
    if not sa and state_fips:
        _, sa = STATE_FIPS.get(state_fips, ("", ""))
    if not sa:
        return empty

    # ── 1. Local JSON DB ─────────────────────────────────────────────────────
    try:
        db_path = os.path.join(os.path.dirname(__file__), "nfip_communities_db.json")
        with open(db_path) as f:
            nfip_db = _json.load(f)

        # Try exact county key first: STATE_COUNTYCODE
        cfips = county_fips.zfill(3) if county_fips else ""
        key = f"{sa}_{cfips}" if cfips else None
        communities = nfip_db.get(key, []) if key else []

        # If not found by county, search all state entries
        if not communities and sa:
            for k, v in nfip_db.items():
                if k.startswith(f"{sa}_"):
                    communities.extend(v)

        city_norm = city.lower().strip()

        if communities and city_norm:
            # Exact match first
            for c in communities:
                c_name = (c.get("name") or "").lower()
                if c_name == city_norm or c_name.startswith(city_norm + ","):
                    return {"csb_community_id": c.get("cid", ""),
                            "csb_community_name": c.get("name", "")}
            # Partial match
            for c in communities:
                c_name = (c.get("name") or "").lower()
                if city_norm in c_name:
                    return {"csb_community_id": c.get("cid", ""),
                            "csb_community_name": c.get("name", "")}
            # County/unincorporated fallback
            for c in communities:
                c_name = (c.get("name") or "").lower()
                if "county" in c_name or "unincorporated" in c_name:
                    return {"csb_community_id": c.get("cid", ""),
                            "csb_community_name": c.get("name", "")}
            # Single result
            if len(communities) == 1:
                return {"csb_community_id": communities[0].get("cid", ""),
                        "csb_community_name": communities[0].get("name", "")}
    except Exception as e:
        print(f"NFIP local DB lookup error: {e}")

    # ── 2. FEMA CSB API fallback ──────────────────────────────────────────────
    try:
        params = {
            "$filter": f"stateAbbreviation eq '{sa}' and countyFips eq '{county_fips}'",
            "$select": "communityNumber,communityName,countyName",
            "$top": "200",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(FEMA_CSB_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        communities_api = data.get("fimaNfipCommunities", [])
        city_norm = city.lower().strip()
        for c in communities_api:
            c_name = (c.get("communityName") or "").lower()
            if city_norm and (c_name == city_norm or city_norm in c_name):
                return {"csb_community_id": c.get("communityNumber", ""),
                        "csb_community_name": c.get("communityName", "")}
        if len(communities_api) == 1:
            return {"csb_community_id": communities_api[0].get("communityNumber", ""),
                    "csb_community_name": communities_api[0].get("communityName", "")}
    except Exception as e:
        print(f"FEMA CSB API fallback error: {e}")

    return empty


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
    "B": "Moderate Flood Hazard — Zone B (0.2% annual chance flood; between 100-year and 500-year floodplain)",
    "C": "Minimal Flood Hazard — Zone C (area outside 500-year floodplain)",
    "D": "Undetermined Flood Hazard — Zone D (possible but undetermined flood hazard)",
    "X": "Minimal Flood Hazard — Zone X (area outside 500-year floodplain; no base flood elevation determined)",
    "X500": "Moderate Flood Hazard — Zone X (Shaded) — 0.2% annual chance flood; property lies within the 500-year floodplain",
    "X-LEVEE": "Moderate Flood Hazard — Zone X (Protected by Levee) — area protected from 1% annual chance flood by a USACE-certified levee system; may be within the 500-year floodplain",
}

ZONE_DISPLAY_NAMES: dict[str, str] = {
    "X":           "Zone X",
    "X500":        "Zone X (Shaded)",
    "X-LEVEE":     "Zone X Levee",
    "A":           "Zone A",
    "AE":          "Zone AE",
    "AH":          "Zone AH",
    "AO":          "Zone AO",
    "AR":          "Zone AR",
    "A99":         "Zone A99",
    "V":           "Zone V",
    "VE":          "Zone VE",
    "B":           "Zone B",
    "C":           "Zone C",
    "D":           "Zone D",
    "UNDETERMINED": "Undetermined",
}


def determine_flood_info(merged: dict) -> dict:
    """Derive flood zone details from merged NFHL query results."""
    flood_zone      = (merged.get("flood_zone") or "X").strip().upper()
    zone_subtype    = (merged.get("zone_subtype") or "").strip()
    in_sfha         = merged.get("in_sfha", False)
    esri_dfirm      = (merged.get("esri_dfirm_id") or "").strip()
    firm_panel_l3   = (merged.get("firm_panel_l3") or "").strip()
    eff_date_raw    = merged.get("eff_date")
    state_fips      = (merged.get("state_fips") or "").strip()
    county_fips     = (merged.get("county_fips") or "").strip()
    community_id    = (merged.get("community_id") or "").strip()
    community_nm    = (merged.get("community_name") or "").strip()
    csb_community_id   = (merged.get("csb_community_id") or "").strip()
    csb_community_name = (merged.get("csb_community_name") or "").strip()
    geocoded_city   = (merged.get("geocoded_city") or "").strip()

    flood_zone_out = _classify_x_zone(flood_zone, zone_subtype)
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

    has_full_l3_panel = len(firm_panel_l3.replace(" ", "")) > 6

    if has_full_l3_panel:
        map_number = firm_panel_l3
    elif esri_dfirm and len(esri_dfirm) >= 5:
        map_number = f"{esri_dfirm[:5]}C"
    else:
        map_number = "Not Available"

    if community_id:
        panel_number = community_id
    elif csb_community_id:
        panel_number = csb_community_id
    else:
        panel_number = "Not Available"

    community_name_out = community_nm or csb_community_name or geocoded_city or "Not Available"

    county_name = (merged.get("county_name") or "").strip()
    if not county_name:
        county_name = (merged.get("tigerweb_county") or "").strip()

    if county_name:
        ct_county = CT_PLANNING_REGION_TO_COUNTY.get(county_name.lower().strip())
        if ct_county:
            county_name = ct_county

    if not county_name:
        county_name = ""

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
        "panel_number": panel_number,
        "panel_effective_date": panel_effective_date,
        "community_number": map_number,
        "community_name": community_name_out,
        "county": county_name,
    }


# ── LOMA / LOMR lookup ────────────────────────────────────────────────────────

async def check_loma_at_point(lat: float, lon: float) -> dict | None:
    """
    Check for LOMA/LOMR at the given coordinates.
    Strategy:
      1. Check local loma_records DB cache first (handles manually entered LOMAs
         and any previously fetched results) — zero latency, always wins.
      2. Fall back to FEMA NFHL Layer 4 API (catches newly issued amendments).
    Returns a dict if an effective removal from SFHA is found, None otherwise.
    """
    from datetime import datetime, timezone

    # ── 1. Local DB cache ─────────────────────────────────────────────────────
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Match within ~500 metres (0.005 decimal degrees)
                cur.execute("""
                    SELECT case_number, outcome_zone, amendment_type,
                           effective_date, original_zone
                    FROM loma_records
                    WHERE ABS(lat - %s) < 0.005
                      AND ABS(lon - %s) < 0.005
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

    # ── 2. FEMA NFHL Layer 4 API ──────────────────────────────────────────────
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
            "status":         attrs.get("STATUS"),
            "outcome_zone":   attrs.get("OUT_ZONE"),
            "effective_date": eff_date,
            "amendment_type": attrs.get("AMEND_TYPE"),
        }

        # Cache successful API results for future lookups
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

        print(f"[LOMA] API result: {result.get('case_number')} "
              f"status={result.get('status')} zone={result.get('outcome_zone')}")
        return result

    except Exception as e:
        print(f"[LOMA] FEMA API check failed: {e}")
        return None
        