import os
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

# Strip subdivision lot / parcel designations before geocoding.
# Geocoders can't resolve "Lot 1" / "Parcel 3B" and fall back to street-level
# interpolation, placing the point 30–100 m from the actual structure.
# Only strip Lot/Parcel/Tract — NOT Unit/Apt/Suite which are legitimate address parts.
_LOT_PATTERN = re.compile(
    r",?\s+(?:Lot|Parcel|Tract)\s*[\w-]+\b",
    re.IGNORECASE,
)

# Street-type suffixes to strip when retrying Nominatim with a simplified address.
_STREET_TYPE_RE = re.compile(
    r"\b(Drive|Dr|Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|"
    r"Court|Ct|Circle|Cir|Place|Pl|Way|Terrace|Ter|Trail|Trl|"
    r"Parkway|Pkwy|Highway|Hwy)\b\.?",
    re.IGNORECASE,
)

ESRI_FLOOD_ZONE_URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services"
    "/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)

SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
NFHL_BASE  = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"

# Connecticut abolished county government in 1960; Census/TIGERweb now return planning region
# names instead of traditional counties.  NFIP certificates require traditional county names.
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

# FEMA NFIP Communities Status Book (OpenFEMA).
# NOTE: www.fema.gov is TLS-blocked server-side on Replit (same as hazards.fema.gov).
# query_nfip_community_csb() will gracefully return empty on connection failure.
# The browser-side Layer 22 enrichment in result.html provides the correct CID at render time.
FEMA_CSB_URL = "https://www.fema.gov/api/open/v1/fimaNfipCommunities"

# ZONE_SUBTY values that map to X500 (shaded Zone X, 0.2% annual chance / 500-year floodplain).
# Includes levee-reduced-risk subtypes: per FEMA NFHL data model, "Area With Reduced Flood
# Risk Due To Levee" and similar values designate the SHADED Zone X on the FIRM = X500 on
# the SFHDF.  These areas ARE within the 500-year floodplain; the levee provides 100-year
# protection only, so the FIRM shows them as shaded (X500), not unshaded.
# Reference: CoreLogic and other flood determination companies correctly label these X500.
_X500_SUBTYPES = frozenset({
    # Standard 0.2% / 500-year flood hazard labels
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
    # Levee-reduced-risk labels (Esri Living Atlas / NFHL) — also shaded X = X500
    "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE",
    "REDUCED FLOOD RISK DUE TO LEVEE",
    "PROTECTED BY LEVEE",
    "AREA PROTECTED BY LEVEE",
    "AREA PROTECTED FROM 100-YEAR FLOOD BY LEVEE",
})


def _classify_x_zone(flood_zone: str, zone_subtype: str) -> str:
    """Classify Zone X into 'X500' or plain 'X'.

    FEMA NFHL uses FLD_ZONE='X' for both shaded (X500) and unshaded Zone X;
    ZONE_SUBTY differentiates them.  Levee-reduced-risk subtypes ("Area With
    Reduced Flood Risk Due To Levee", "Protected by Levee", etc.) are shaded
    Zone X on the FIRM and must be reported as X500 on the SFHDF — CoreLogic
    and all major flood-determination services confirm this.

    Some older datasets or reduced-set layers use FLD_ZONE='X500' or 'B'.
    """
    if flood_zone in ("X500", "B"):
        return "X500"
    if flood_zone != "X":
        return flood_zone

    sub = zone_subtype.upper().strip()
    if not sub:
        return "X"

    # Explicit X500 and levee-shaded matches
    if sub in _X500_SUBTYPES:
        return "X500"
    # Catch variations: "0.2 PCT ...", "0.2%...", etc.
    if "0.2" in sub:
        return "X500"
    # Catch "500 YEAR FLOOD HAZARD", "500-YEAR ANNUAL CHANCE", etc.
    if "500" in sub and ("ANNUAL" in sub or "YEAR" in sub or "CHANCE" in sub):
        return "X500"
    # Catch any remaining levee-related subtype — all indicate shaded Zone X = X500
    if "LEVEE" in sub:
        return "X500"

    return "X"

TIGERWEB_COUNTY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services"
    "/TIGERweb/State_County/MapServer/1/query"
)


def _geocode_plausible(input_address: str, matched_address: str) -> bool:
    """Return False when matched_address is clearly wrong for input_address.

    Checks house number, primary street name, and ZIP code as signals.
    Accepts a match when:
    - house number matches AND (street OR zip matches), OR
    - all three signals are absent (nothing to compare)

    This relaxed logic handles short/ambiguous street names like "Duck Dr"
    that geocoders may match to a different street in the same ZIP.
    """
    if not matched_address:
        return True  # nothing to compare; pass through

    inp = input_address.upper()
    mat = matched_address.upper()

    # ── House number ──────────────────────────────────────────────────────────
    num_m = re.match(r"\s*(\d+)", inp)
    house_num = num_m.group(1) if num_m else ""

    # ── Primary street word ───────────────────────────────────────────────────
    street_part = re.sub(r"^\s*\d+\s*", "", inp.split(",")[0])
    _STOP = {
        "DR", "ST", "AVE", "BLVD", "RD", "LN", "CT", "CIR", "PL", "WAY",
        "TER", "TRL", "PKWY", "HWY", "DRIVE", "STREET", "AVENUE",
        "BOULEVARD", "ROAD", "LANE", "COURT", "CIRCLE", "PLACE",
        "TERRACE", "TRAIL", "PARKWAY", "HIGHWAY", "UNIT", "APT", "STE",
    }
    sig_words = [w for w in re.findall(r"\b[A-Z]{2,}\b", street_part) if w not in _STOP]
    primary_word = sig_words[0] if sig_words else ""

    # ── ZIP code ──────────────────────────────────────────────────────────────
    zip_m = re.search(r"\b(\d{5})\b", inp)
    zip_code = zip_m.group(1) if zip_m else ""

    house_ok  = bool(house_num)   and house_num   in mat
    street_ok = bool(primary_word) and primary_word in mat
    zip_ok    = bool(zip_code)    and zip_code    in mat

    # Accept if house number matches plus at least one other signal
    if house_ok and (street_ok or zip_ok):
        return True
    # Accept if street name matches (handles addresses without house numbers)
    if street_ok:
        return True
    # If no signals present at all, pass through
    if not house_num and not primary_word and not zip_code:
        return True
    # Reject only when house number is present but missing from match
    # AND street name is also missing — clearly wrong location
    if house_num and not house_ok and not street_ok:
        return False
    # Default: accept
    return True


# Photon OSM keys that represent actual buildings or streets.
# Keys like "amenity", "shop", "leisure", "tourism" are POIs (bus stops,
# pharmacies, golf courses, etc.) and must never be used as geocoding results.
_PHOTON_OK_KEYS = frozenset({"building", "highway", "place", "addr"})


async def geocode_address(address: str) -> Optional[dict]:
    """Geocode a US address to lat/lon + county FIPS.

    Pre-processing: strips subdivision lot designations (Lot 1, Parcel 3B, Tract A)
    which geocoders cannot resolve and which cause street-level interpolation,
    placing the point 30–100 m from the actual structure.

    Fallback chain (most → least authoritative):
    1. Census geographies — lat/lon + county FIPS + county name (most authoritative)
    2. Census locations  — lat/lon only (no FIPS)
    3. Photon (komoot)  — OSM building polygon centroids; rooftop-level precision; free, no key
    4. Nominatim (OSM)  — street-level interpolation fallback
    5. Nominatim (stripped street type) — last resort for unusual street names
    """
    # ── Pre-processing: strip lot/parcel designations that confuse geocoders ──
    # "301 Satinwood Drive Lot 1, City, FL" → "301 Satinwood Drive, City, FL"
    geocode_q = re.sub(r"\s{2,}", " ", _LOT_PATTERN.sub("", address)).strip()

    # ── Attempt 0: Geocodio (rooftop precision, free 2500/day) ─────────────────
    geocodio_key = os.environ.get("GEOCODIO_API_KEY", "")
    if geocodio_key:
        try:
            geocode_q_geo = re.sub(r"\s{2,}", " ", _LOT_PATTERN.sub("", address)).strip()
            params = {"q": geocode_q_geo, "api_key": geocodio_key, "limit": "1"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.geocod.io/v1.7/geocode", params=params
                )
                resp.raise_for_status()
                data = resp.json()
            results = data.get("results", [])
            if results:
                r = results[0]
                loc = r.get("location", {})
                components = r.get("address_components", {})
                accuracy_type = r.get("accuracy_type", "")
                accuracy = r.get("accuracy", 0)
                GOOD_ACCURACY = {"rooftop", "point", "range_interpolation", "nearest_rooftop_match", "street_center"}
                if accuracy_type in GOOD_ACCURACY and accuracy >= 0.7:
                    lat_g = loc.get("lat")
                    lon_g = loc.get("lng")
                    city_g = components.get("city", "").title()
                    state_g = components.get("state", "").upper()
                    county_g = components.get("county", "").replace(" County","").strip()
                    state_fips_g = ""
                    county_fips_g = ""
                    try:
                        fcc_params = {"latitude": lat_g, "longitude": lon_g, "format": "json"}
                        async with httpx.AsyncClient(timeout=8.0) as fc:
                            fr = await fc.get("https://geo.fcc.gov/api/census/block/find", params=fcc_params)
                            fr.raise_for_status()
                            fcc_data = fr.json()
                        state_fips_g  = (fcc_data.get("State", {}).get("FIPS") or "")[:2]
                        county_fips_g = (fcc_data.get("County", {}).get("FIPS") or "")[2:5]
                    except Exception as fe:
                        print(f"FCC FIPS lookup error: {fe}")
                    # Fix CT planning region → county name
                    if state_g == "CT":
                        county_g = CT_PLANNING_REGION_TO_COUNTY.get(
                            county_g.lower().strip(), county_g
                        )
                    result = {
                        "lat": lat_g, "lon": lon_g,
                        "matched_address": r.get("formatted_address", address),
                        "city": city_g, "state_abbr": state_g,
                        "state_fips": state_fips_g, "county_fips": county_fips_g,
                        "county_name": county_g,
                    }
                    if _geocode_plausible(geocode_q_geo, result["matched_address"]):
                        print(f"Geocodio ({accuracy_type}, score={accuracy}): {result['matched_address']}")
                        return result
        except Exception as e:
            print(f"Geocodio geocoding error: {e}")

    # ── Attempt 1: Census geographies (county FIPS + county name) ─────────────
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
                # Fallback to Census2010 vintage for addresses Census2020 rejects
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

    # ── Attempt 2: Census locations endpoint (no county FIPS) ─────────────────
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

    # ── Attempt 3: ArcGIS World Geocoder (free, no API key, score-gated) ────────
    # Covers addresses that Census TIGER and OSM both miss — private roads,
    # new developments, township addresses where the mailing city ≠ jurisdiction.
    # "forStorage=false" keeps it free per Esri's terms.
    # Only accept candidates with score ≥ 85 and a rooftop-level addr_type.
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
            loc      = best_a.get("location", {})
            lat_a    = float(loc.get("y", 0))
            lon_a    = float(loc.get("x", 0))
            attrs_a  = best_a.get("attributes") or {}
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

    # ── Attempt 4: Photon — OSM building-polygon centroids (rooftop precision) ─
    # Free public API, no key required.  Returns building-level OSM objects
    # rather than interpolated street points, fixing zone boundary edge cases.
    #
    # IMPORTANT: Photon results include POIs (bus stops, pharmacies, golf
    # courses) that share the same ZIP or neighbourhood as the target.  Only
    # accept results whose osm_key indicates a building, street, or place;
    # never a POI.  Additionally validate the matched address with
    # _geocode_plausible() before returning.
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
        # 1st pass: prefer rooftop building results
        best = None
        for f in features:
            osm_value = (f.get("properties", {}).get("osm_value") or "").lower()
            if osm_value in ("house", "residential", "detached", "apartments",
                             "yes", "building", "terrace", "semi", "bungalow"):
                best = f
                break
        # 2nd pass: fall back to street/road — but NEVER to POI types
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

    # ── Attempt 4: Nominatim / OSM street interpolation ───────────────────────
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

    # ── Attempt 5: Nominatim with street-type stripped ─────────────────────────
    # Helps unusual street names like "4248 Duck Dr" → retry as "4248 Duck".
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

    # ── Attempt 6: Minimal query — house number + city + state + ZIP ──────────
    # Last resort for addresses where all geocoders fail the plausibility check.
    # Strips street name entirely and queries by number + city + state + ZIP.
    # This ensures we get coordinates in the right area even if street is wrong.
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
                    "matched_address": address,  # use original address as matched
                    "city": city,
                    "state_abbr": state_abbr,
                    "state_fips": "",
                    "county_fips": "",
                    "county_name": county_raw,
                }
                # Accept if ZIP matches — we're in the right area
                if zip_r and zip_r in geocode_q:
                    print(f"Geocoding (minimal ZIP fallback) accepted for: {address!r}")
                    return result
    except Exception as e:
        print(f"Geocoding (minimal ZIP fallback) error: {e}")

    return None


async def query_fema_nfhl(lat: float, lon: float) -> dict:
    """Query flood zone via Esri Living Atlas USA Flood Hazard layer.

    Returns FLD_ZONE, ZONE_SUBTY, SFHA_TF, DFIRM_ID — same fields as NFHL Layer 28.
    DFIRM_ID comes from a true spatial intersection of NFHL flood zone polygons and
    is the correct county FIPS for that specific map tile.  It is more reliable than
    the Census geocoder county FIPS for properties near county / municipality boundaries.
    """
    queries = [
        {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
        },
        {
            "geometry": f"{lon - 0.0005},{lat - 0.0005},{lon + 0.0005},{lat + 0.0005}",
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

    Tries point query first, then progressively wider envelopes for consistency.
    """
    queries = [
        {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint"},
        {"geometry": f"{lon-0.001},{lat-0.001},{lon+0.001},{lat+0.001}", "geometryType": "esriGeometryEnvelope"},
        {"geometry": f"{lon-0.005},{lat-0.005},{lon+0.005},{lat+0.005}", "geometryType": "esriGeometryEnvelope"},
    ]
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
            if not features:
                continue
            # Prefer feature with valid CID
            best = None
            for f in features:
                cid = (f["attributes"].get("CID") or "").strip()
                if cid and cid != "0":
                    best = f["attributes"]
                    break
            if best is None:
                best = features[0]["attributes"]
            cid = (best.get("CID") or "").strip()
            name = (best.get("POL_NAME1") or "").strip()
            if cid or name:
                return {"community_id": cid, "community_name": name}
        except Exception as e:
            print(f"NFIP community query (Layer 22) error: {e}")
    return {}


async def query_firm_panel(lat: float, lon: float, county_fips: str = "") -> dict:
    """Query NFHL Layer 3 (FIRM Panels) for full panel number and effective date.

    hazards.fema.gov is network-blocked server-side on Replit (connection reset).
    We try anyway; the browser-side JS enrichment is the reliable fallback.
    Returns the full FIRM_PAN (e.g. "48091C 0215F") and EFF_DATE when reachable.
    
    county_fips: full 5-digit FIPS (e.g. "45035"). Used to validate panel prefix.
    """
    # Build envelope sizes to try - start small, expand if needed
    envelope_sizes = [0.002, 0.005, 0.01]
    queries = [{"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint"}]
    for size in envelope_sizes:
        queries.append({
            "geometry": f"{lon - size},{lat - size},{lon + size},{lat + size}",
            "geometryType": "esriGeometryEnvelope",
        })

    # Expected panel prefix from county FIPS (e.g. "45035" -> "45035C")
    expected_prefix = county_fips.upper() if county_fips else ""

    best_result = None

    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FIRM_PAN,EFF_DATE,DFIRM_ID,PANEL_TYP",
            "returnGeometry": "false",
            "resultRecordCount": "20",
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

            # Try to find a feature matching expected county prefix first
            matched_attrs = None
            fallback_attrs = None

            for f in features:
                raw = (f["attributes"].get("FIRM_PAN") or "").strip()
                panel_prefix = raw[:5] if len(raw) >= 5 else ""
                is_printed = "Panel Printed" in (f["attributes"].get("PANEL_TYP") or "")

                # Check if panel prefix matches expected county FIPS
                if expected_prefix and panel_prefix == expected_prefix:
                    if is_printed or matched_attrs is None:
                        matched_attrs = f["attributes"]
                elif fallback_attrs is None:
                    if is_printed:
                        fallback_attrs = f["attributes"]

            # Prefer county-matched result
            attrs = matched_attrs or fallback_attrs
            if attrs is None:
                attrs = features[0]["attributes"]

            raw = (attrs.get("FIRM_PAN") or "").strip()
            firm_pan = f"{raw[:6]} {raw[6:]}" if len(raw) >= 7 else raw

            result = {
                "firm_panel_l3": firm_pan or (attrs.get("DFIRM_ID") or ""),
                "eff_date": attrs.get("EFF_DATE"),
            }

            # If we got a county-matched result return immediately
            if matched_attrs is not None:
                print(f"FIRM panel matched county {expected_prefix}: {firm_pan}")
                return result

            # Otherwise save as best result and keep trying wider envelopes
            if best_result is None:
                best_result = result

        except Exception as e:
            print(f"FIRM panel query (Layer 3, {q['geometryType']}) error: {e}")

    return best_result or {}


async def query_tigerweb_fips(lat: float, lon: float) -> dict:
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


async def query_nfip_community_csb(
    state_fips: str,
    county_fips: str,
    city: str,
    state_abbr: str = "",
) -> dict:
    """Query FEMA NFIP Community Status Book API for the authoritative community number.

    Returns the official FEMA-assigned community number for the municipality
    (e.g. "060302" for City of Stockton, CA) instead of a county-FIPS placeholder.
    Prefers the city/municipality-level community over the county-level community.

    Requires either state_fips (2-digit) or state_abbr (e.g. "CA") plus
    county_fips (3-digit).  Both must be known; returns empty on failure.

    FEMA endpoint: https://www.fema.gov/api/open/v1/fimaNfipCommunities
    """
    empty: dict = {"csb_community_id": "", "csb_community_name": ""}

    # Resolve state abbreviation
    sa = state_abbr.upper().strip()
    if not sa and state_fips:
        _, sa = STATE_FIPS.get(state_fips, ("", ""))
    if not sa:
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
        return empty

    params = {
        "$filter": f"stateAbbreviation eq '{sa}' and countyFips eq '{county_fips}'",
        "$select": "communityNumber,communityName,countyName",
        "$top": "200",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(FEMA_CSB_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print(f"FEMA CSB API error: {e}")
        return empty

    communities = data.get("fimaNfipCommunities", [])
    if not communities:
        return empty

    city_norm = city.lower().strip()

    # 1. Exact / leading city match  ("Stockton, City of" starts with "stockton,")
    if city_norm:
        for c in communities:
            c_name = (c.get("communityName") or "").lower()
            if (
                c_name == city_norm
                or c_name.startswith(city_norm + ",")
                or c_name.startswith(city_norm + " ")
            ):
                return {
                    "csb_community_id": c.get("communityNumber", ""),
                    "csb_community_name": c.get("communityName", ""),
                }

    # 2. Substring match
    if city_norm:
        for c in communities:
            c_name = (c.get("communityName") or "").lower()
            if city_norm in c_name:
                return {
                    "csb_community_id": c.get("communityNumber", ""),
                    "csb_community_name": c.get("communityName", ""),
                }

    # 3. County-level / unincorporated community (fallback)
    for c in communities:
        c_name = (c.get("communityName") or "").lower()
        if "county" in c_name or "unincorporated" in c_name:
            return {
                "csb_community_id": c.get("communityNumber", ""),
                "csb_community_name": c.get("communityName", ""),
            }

    # 4. Single result — use it
    if len(communities) == 1:
        c = communities[0]
        return {
            "csb_community_id": c.get("communityNumber", ""),
            "csb_community_name": c.get("communityName", ""),
        }

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

# Human-readable display labels for each internal zone code.
# Used in PDF headers, result page badges, and filenames.
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
    """Derive flood zone details from merged NFHL query results.

    Expects keys from zone_data, community_data, firm_data, county_data,
    csb_data, plus: geocoded_city, state_fips, county_fips, county_name,
    state_abbr (optional — enriches CSB lookup when state_fips is absent).
    """
    flood_zone      = (merged.get("flood_zone") or "X").strip().upper()
    zone_subtype    = (merged.get("zone_subtype") or "").strip()
    in_sfha         = merged.get("in_sfha", False)
    esri_dfirm      = (merged.get("esri_dfirm_id") or "").strip()
    firm_panel_l3   = (merged.get("firm_panel_l3") or "").strip()
    eff_date_raw    = merged.get("eff_date")
    state_fips      = (merged.get("state_fips") or "").strip()
    county_fips     = (merged.get("county_fips") or "").strip()
    community_id    = (merged.get("community_id") or "").strip()    # Layer 22
    community_nm    = (merged.get("community_name") or "").strip()  # Layer 22
    csb_community_id   = (merged.get("csb_community_id") or "").strip()    # FEMA CSB API
    csb_community_name = (merged.get("csb_community_name") or "").strip()  # FEMA CSB API
    geocoded_city   = (merged.get("geocoded_city") or "").strip()

    # ── Zone designation ──────────────────────────────────────────────────────
    # _classify_x_zone handles all Zone X variants:
    #   X500   → shaded Zone X (0.2% annual chance / 500-year floodplain)
    #   X-LEVEE → Zone X protected from 100-year flood by USACE-certified levee
    #   X      → unshaded Zone X (truly outside 500-year floodplain)
    # Also maps old Zone B → X500, and passes explicit "X500" from FLD_ZONE through.
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

    # ── NFIP Map Number (Community-Panel Number) ──────────────────────────────
    # Priority:
    # 1. Layer 3 full panel (e.g. "48091C 0215F") — direct spatial intersection of FIRM Panels
    # 2. Esri Living Atlas DFIRM_ID — also from spatial intersection of NFHL flood zone polygons;
    #    knows the correct county FIPS even for cross-county municipalities and is more reliable
    #    than the Census geocoder county FIPS (which uses administrative address attribution).
    # 3. Census geocoder county FIPS — address-based; may be wrong near county boundaries.
    # 4. "Not Available"
    has_full_l3_panel = len(firm_panel_l3.replace(" ", "")) > 6

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
        map_number = "Not Available"

    # ── NFIP Community Number (CID) ───────────────────────────────────────────
    # Priority:
    # 1. CID from Layer 22 (hazards.fema.gov) — NFIP-assigned 6-digit ID (when server-reachable)
    # 2. CID from FEMA NFIP Community Status Book API — authoritative municipal community ID
    #    (e.g. "060302" for City of Stockton CA, rather than county placeholder "06077C")
    # 3. County-level approximation: state(2) + county(3) + "0" — county jurisdictions only
    # 4. Fallback to map number prefix
    # NFIP Community Number (CID) — only use authoritative sources
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
        panel_number = "Not Available"

    # ── NFIP Community Name ───────────────────────────────────────────────────
    # Priority: Layer 22 POL_NAME1 → FEMA CSB official name → geocoded city
    community_name_out = community_nm or csb_community_name or geocoded_city or "Not Available"

    # ── County name ───────────────────────────────────────────────────────────
    # Priority: Census geocoder geography → TIGERweb fallback
    county_name = (merged.get("county_name") or "").strip()
    if not county_name:
        county_name = (merged.get("tigerweb_county") or "").strip()

    # Connecticut: Census and TIGERweb return planning region names, not traditional counties.
    # NFIP certificates must use traditional county names — map planning regions → county.
    if county_name:
        ct_county = CT_PLANNING_REGION_TO_COUNTY.get(county_name.lower().strip())
        if ct_county:
            county_name = ct_county

    # Last-resort county flag: if county still blank after all lookups, flag it explicitly
    # so the record is not silently missing a county rather than using empty string.
    if not county_name:
        county_name = ""  # caller / template should display "Not Available" for empty

    # ── NFIP Map Panel Effective/Revised Date ─────────────────────────────────
    # Date must come from the specific matched panel polygon's EFF_DATE attribute.
    # Layer 3 (query_firm_panel) returns this; browser-side JS overrides with
    # the exact panel polygon EFF_DATE when Layer 3 is unreachable server-side.
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
