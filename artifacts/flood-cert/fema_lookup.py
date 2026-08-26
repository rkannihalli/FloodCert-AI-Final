import re
import httpx
from typing import Optional

# Optional — used only to resolve genuine panel-tile-boundary ambiguity by
# testing real point-in-polygon containment against fetched FIRM panel
# geometry. If not installed, that specific disambiguation is skipped and
# the existing low-confidence flagging behavior is used instead (no crash,
# no regression — see query_firm_panel).
try:
    from shapely.geometry import Point as _ShapelyPoint, Polygon as _ShapelyPolygon
    _SHAPELY_AVAILABLE = True
except ImportError:
    _SHAPELY_AVAILABLE = False
    print("[PANEL-DEBUG] shapely not installed — panel tile-boundary geometry "
          "disambiguation disabled, falling back to confidence flagging only. "
          "Install with: pip install shapely --break-system-packages")

CENSUS_GEO_URL  = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_LOC_URL  = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

# ─────────────────────────────────────────────────────────────────────────
# Independent cities: the 41 U.S. jurisdictions that are their own
# county-equivalent, not part of any surrounding county. Used generically
# throughout jurisdiction/county matching — never hard-code an individual
# address's independent-city status; always resolve it through this table.
#
# Keys are bare, normalized city names (see _normalize_place_name) for the
# state that holds them. "Carson City" is the one entry where "city" is
# genuinely part of the jurisdiction's proper name (not a descriptive
# suffix), so it's stored with "city" included; every other entry's bare
# form omits "city"/"county" since those are added only as disambiguating
# descriptors on top of the true place name (e.g. "Richmond" -> "City of
# Richmond" to distinguish it from the separate, real Richmond County, VA).
# ─────────────────────────────────────────────────────────────────────────
INDEPENDENT_CITIES = {
    "MD": {"baltimore"},
    "MO": {"stlouis"},
    "NV": {"carsoncity"},
    "VA": {
        "alexandria", "bristol", "buenavista", "charlottesville", "chesapeake",
        "colonialheights", "covington", "danville", "emporia", "fairfax",
        "fallschurch", "franklin", "fredericksburg", "galax", "hampton",
        "harrisonburg", "hopewell", "lexington", "lynchburg", "manassas",
        "manassaspark", "martinsville", "newportnews", "norfolk", "norton",
        "petersburg", "poquoson", "portsmouth", "radford", "richmond",
        "roanoke", "salem", "staunton", "suffolk", "virginiabeach",
        "waynesboro", "williamsburg", "winchester",
    },
}

_PLACE_PREFIXES = ("city of ", "town of ", "village of ", "township of ")
_PLACE_SUFFIX_WORDS = ("county", "parish", "borough", "independentcity")


def _normalize_place_name(name: str) -> str:
    """Lowercase, strip prefixes/punctuation, and strip a trailing "city"
    or "county" descriptor UNLESS the bare result wouldn't be a real place
    on its own (see INDEPENDENT_CITIES docstring re: Carson City).
    Reusable across jurisdiction matching, county-field display, and
    independent-city detection — this is the one normalization function
    all of those should share, so a name is always compared the same way.
    """
    n = (name or "").lower().strip()
    for p in _PLACE_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
    if n.endswith(", city") or n.endswith(", town") or n.endswith(", village"):
        n = n.rsplit(",", 1)[0]
    n = re.sub(r"[^a-z0-9]", "", n)
    for suffix in _PLACE_SUFFIX_WORDS:
        if n.endswith(suffix) and n != suffix:
            n = n[: -len(suffix)]
            break
    if n.endswith("city") and n != "carsoncity":
        n = n[:-4]
    return n


def is_independent_city(name: str, state_abbr: str) -> bool:
    """True if `name` refers to a known U.S. independent city."""
    sa = (state_abbr or "").strip().upper()
    if sa not in INDEPENDENT_CITIES:
        return False
    if "county" in (name or "").lower():
        return False
    return _normalize_place_name(name) in INDEPENDENT_CITIES[sa]


def is_independent_city_county_collision(candidate_name: str, state_abbr: str) -> bool:
    """True if `candidate_name` is a namesake county of an independent city."""
    sa = (state_abbr or "").strip().upper()
    if sa not in INDEPENDENT_CITIES:
        return False
    n = (candidate_name or "").lower()
    if "county" not in n:
        return False
    bare = _normalize_place_name(candidate_name)
    return bare in INDEPENDENT_CITIES[sa]

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

# Matches addresses combining two house numbers into one ungeocodable string,
# e.g. "1108 and 1110 S Main St" or "1108 & 1110 S Main St". This isn't a
# geocoding precision issue — it's an invalid input string; no geocoder can
# resolve "two house numbers" to one point. Confirmed against test data
# (Reidsville, NC address returned completely empty across every lookup).
# Keep just the first house number, since that's what a person would
# reasonably expect the primary determination to be for.
_DUAL_ADDRESS_PATTERN = re.compile(
    r"^(\d+)\s*(?:and|&)\s*\d+([\w\-]*)(\s+.*)$",
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

    dual_match = _DUAL_ADDRESS_PATTERN.match(geocode_q)
    if dual_match:
        cleaned = f"{dual_match.group(1)}{dual_match.group(2)}{dual_match.group(3)}".strip()
        print(f"[GEOCODE] Dual house-number address detected — using first "
              f"address only: {geocode_q!r} -> {cleaned!r}")
        geocode_q = cleaned

    # ── Attempt 0: ArcGIS World Geocoder — rooftop precision only ────────────
    # Tried FIRST, ahead of Census. The Census geocoder below does street
    # address-range interpolation: it estimates a point along the street
    # segment rather than locating the actual structure, which can be off by
    # tens of meters. Near a flood zone boundary (creek, levee, floodplain
    # edge) that error is enough to flip the FEMA zone lookup (e.g. AE vs X).
    # ArcGIS's "PointAddress"/"Subaddress" match types are rooftop-level —
    # matched to the actual parcel/structure — so we prefer them when
    # available and only fall back to the coarser Census interpolation
    # otherwise.
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

        _ARCGIS_ROOFTOP = {"PointAddress", "Subaddress"}
        best_r = None
        for c in data.get("candidates", []):
            score     = c.get("score", 0)
            addr_type = (c.get("attributes") or {}).get("Addr_type", "")
            if score >= 90 and addr_type in _ARCGIS_ROOFTOP:
                best_r = c
                break

        if best_r:
            loc     = best_r.get("location", {})
            lat_r   = float(loc.get("y", 0))
            lon_r   = float(loc.get("x", 0))
            attrs_r = best_r.get("attributes") or {}
            result = {
                "lat": lat_r,
                "lon": lon_r,
                "matched_address": best_r.get("address", geocode_q),
                "city": (attrs_r.get("City") or "").title(),
                "state_abbr": (attrs_r.get("RegionAbbr") or "").upper(),
                "state_fips": "",
                "county_fips": "",
                "county_name": (attrs_r.get("Subregion") or "").replace(" County", "").strip(),
                "geocode_precision": "rooftop",
                "geocode_source": "arcgis_pointaddress",
            }
            if _geocode_plausible(geocode_q, result["matched_address"]):
                return result
            print(f"Geocoding (ArcGIS rooftop) plausibility rejected: {result['matched_address']!r}")
    except Exception as e:
        print(f"Geocoding (ArcGIS rooftop) error: {e}")

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
                "geocode_precision": "interpolated",
                "geocode_source": "census_geographies",
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
                "geocode_precision": "interpolated",
                "geocode_source": "census_locations",
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
                "geocode_precision": "rooftop" if addr_type in ("PointAddress", "Subaddress") else "interpolated",
                "geocode_source": "arcgis_broad",
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
        best_is_building = False
        for f in features:
            osm_value = (f.get("properties", {}).get("osm_value") or "").lower()
            if osm_value in ("house", "residential", "detached", "apartments",
                             "yes", "building", "terrace", "semi", "bungalow"):
                best = f
                best_is_building = True
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
                    "geocode_precision": "rooftop" if best_is_building else "approximate",
                    "geocode_source": "photon",
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
                "geocode_precision": "approximate",
                "geocode_source": "nominatim",
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
                    "geocode_precision": "approximate",
                    "geocode_source": "nominatim_normalized",
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
                    "geocode_precision": "approximate",
                    "geocode_source": "nominatim_minimal",
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
    D2 = 0.0006  # ~65m second ring
    offsets = [(0, 0), (D, 0), (-D, 0), (0, D), (0, -D),
               (D2, 0), (-D2, 0), (0, D2), (0, -D2)]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            results = []
            center_result = None
            for i, (dlat, dlon) in enumerate(offsets):
                r = await _query_zone_at_point(client, lon + dlon, lat + dlat)
                if r:
                    results.append(r)
                    if i == 0:  # (0, 0) — the actual rooftop-geocoded point
                        center_result = r

        if not results:
            # Every sample point came back with zero features from FEMA's
            # zone layer — this is a genuine data-coverage gap (confirmed
            # against real test data: rural Toole County, MT returned zero
            # features from both the jurisdiction and panel layers too),
            # not a real "Zone X" determination. Presenting this identically
            # to a confirmed Zone X — "Non-Special Flood Hazard, Not
            # Required" — silently asserts something we don't actually
            # know. Flag it honestly instead.
            print(f"[ZONE-DEBUG] lat={lat} lon={lon} — 0/9 sample points returned any zone "
                  f"data; FEMA has no NFHL coverage at this location.")
            return {"flood_zone": "", "in_sfha": False, "zone_subtype": "", "esri_dfirm_id": "",
                    "boundary_case": False, "zone_data_available": False}

        # Count SFHA vs non-SFHA votes
        sfha_results = [r for r in results if r.get("SFHA_TF") == "T"]
        non_sfha_results = [r for r in results if r.get("SFHA_TF") != "T"]
        print(f"[ZONE-DEBUG] lat={lat} lon={lon} per-point results: "
              f"{[(r.get('FLD_ZONE'), r.get('SFHA_TF')) for r in results]}")

        # "boundary_case" flags any address where the samples didn't agree —
        # i.e. the geocoded point sits close enough to a zone line that
        # nearby offsets land on different sides of it. Used only for the
        # confidence flag below, never to override the center point's own
        # determination (see note above center_result).
        boundary_case = bool(sfha_results) and bool(non_sfha_results)

        # The center point (the actual rooftop-geocoded location) already
        # got an exact ArcGIS point-in-polygon test — it's not an
        # approximation. The old logic below (majority vote across all 9
        # samples, weighted toward SFHA whenever >=40% of samples were
        # SFHA) could override this precise, correct center result with a
        # noisier reading from one of the ~30-65m offset points — which is
        # exactly what produced false AE results at Clinton, MA and Isle of
        # Palms, SC: the true rooftop point was genuinely Zone X, but a
        # minority of nearby offset samples crossed into an adjacent AE
        # zone and won the vote anyway. The center point's own answer must
        # be authoritative whenever we have it; the offsets exist only to
        # flag confidence, not to outvote the one sample we know is
        # precisely located.
        if center_result is not None:
            best = center_result
            if boundary_case:
                print(f"[NFHL] Boundary case near center point — trusting center's own "
                      f"determination ({center_result.get('FLD_ZONE')}) over "
                      f"{len(sfha_results)} SFHA / {len(non_sfha_results)} non-SFHA offset votes")
        else:
            # Center point itself returned no data (a genuine edge case,
            # e.g. sitting exactly on a shared boundary line) — fall back
            # to the offset-majority approach since there's no single
            # precise anchor point available.
            total = len(results)
            non_sfha_pct = len(non_sfha_results) / total if total > 0 else 0
            if non_sfha_pct >= 0.6:
                best = non_sfha_results[0]
                print(f"[NFHL] No center result; boundary vote: {len(sfha_results)} SFHA vs "
                      f"{len(non_sfha_results)} non-SFHA ({non_sfha_pct:.0%}) — using non-SFHA")
            elif sfha_results:
                best = sfha_results[0]
            else:
                best = results[0]

        return {
            "flood_zone": (best.get("FLD_ZONE") or "X").strip(),
            "zone_subtype": best.get("ZONE_SUBTY") or "",
            "esri_dfirm_id": best.get("DFIRM_ID") or "",
            "in_sfha": best.get("SFHA_TF", "F") == "T",
            "boundary_case": boundary_case,
            "zone_data_available": True,
        }
    except Exception as e:
        print(f"FEMA flood zone query error: {e}")

    return {"flood_zone": "", "in_sfha": False, "zone_subtype": "", "esri_dfirm_id": "",
            "boundary_case": False, "zone_data_available": False}


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


async def query_nfip_community(lat: float, lon: float, geocoded_city: str = "", state_abbr: str = "") -> dict:
    """Query NFHL Layer 22 (Political Jurisdictions) for NFIP community name and CID.
    
    Strategy:
    1. Point query — most precise
    2. Small envelope fallback — catches edge cases near boundaries
    3. Disambiguate using the geocoded city name when multiple overlapping
       jurisdictions are returned (see note below on why "prefer city" alone
       is not reliable)
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
    _JURIS_PREFIXES = ("city of ", "town of ", "village of ", "township of ")

    # Known, well-documented cases where the property's postal/geocoded city
    # name is itself a real, separately-incorporated city that nonetheless
    # is NOT the correct NFIP jurisdiction — because it sits inside a larger
    # consolidated city-county government as a legally "excluded" enclave.
    # Confirmed against test data: Beech Grove and Lawrence, IN are both
    # long-standing excluded municipalities within Indianapolis/Marion
    # County's 1970 Unigov consolidation — CoreLogic consistently assigns
    # these to "City of Indianapolis," not the enclave city, even though
    # the enclave city is the postal/geocoded city name. Ordinary
    # geocoded-city-name matching can never fix this (the postal name IS
    # the wrong answer), so this needs an explicit, verifiable override
    # rather than more heuristics. Keyed by (enclave, correct) pairs; only
    # fires when BOTH appear together as actual candidates at this point,
    # so it can't misfire on an unrelated "Lawrence" elsewhere in the US.
    _ENCLAVE_OVERRIDES = {
        "beechgrove": "indianapolis",
        "lawrence": "indianapolis",
        "speedway": "indianapolis",
        "southport": "indianapolis",
    }

    def _is_city_level(name: str) -> bool:
        """Return True if this is a city/town/village — not a county."""
        n = name.lower()
        return not any(w in n for w in _COUNTY_WORDS)

    def _normalize_juris_name(name: str) -> str:
        n = name.lower().strip()
        for p in _JURIS_PREFIXES:
            if n.startswith(p):
                n = n[len(p):]
        # Handle "Beech Grove, City of" style suffix ordering too
        if n.endswith(", city") or n.endswith(", town") or n.endswith(", village"):
            n = n.rsplit(",", 1)[0]
        return re.sub(r"[^a-z0-9]", "", n)
    
    all_features = []
    
    for q in queries:
        params = {
            **q,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "POL_NAME1,CID,ANI_TF,COMM_NO",
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

    community_confidence = "high"
    community_confidence_note = ""

    if len(unique) == 1:
        best = unique[0]
    else:
        # Multiple overlapping jurisdictions at this point — normal, since
        # FEMA's Layer 22 returns both a city polygon and its underlying
        # county polygon for any point inside an incorporated city. Blindly
        # preferring "any city-level result" here is wrong about as often as
        # it's right: for boundary/enclave properties (e.g. a city entirely
        # surrounded by a larger consolidated city, or an address just
        # outside a city's actual limits despite a nearby postal city name)
        # the correct answer can be the county, or a different neighboring
        # jurisdiction entirely.
        #
        # The geocoded city name is a real, independent signal for which
        # jurisdiction is correct — use it first. Only fall back to the
        # "prefer city" heuristic as a last resort, and flag that fallback
        # as low-confidence so it gets reviewed rather than silently trusted.
        # Check known enclave overrides first — these are cases where the
        # geocoded city name would otherwise "successfully" match the WRONG
        # candidate (see _ENCLAVE_OVERRIDES above), so ordinary name-matching
        # must not get first say here.
        enclave_matched = None
        for f in unique:
            cand_norm = _normalize_juris_name(f["attributes"].get("POL_NAME1") or "")
            correct_target = _ENCLAVE_OVERRIDES.get(cand_norm)
            if correct_target:
                for f2 in unique:
                    if correct_target in _normalize_juris_name(f2["attributes"].get("POL_NAME1") or ""):
                        enclave_matched = f2
                        break
            if enclave_matched:
                break

        # Independent cities (e.g. St. Louis city vs St. Louis County, MO;
        # Baltimore city vs Baltimore County, MD; Virginia's 38 independent
        # cities) are legally separate from any county -- including one
        # that happens to share their name. The substring-based name match
        # below ("stlouis" in "stlouiscounty") would otherwise select the
        # namesake COUNTY whenever the geocoded city is itself an
        # independent city, since the county's normalized name always
        # contains the city's name as a prefix. Filter those collision
        # candidates out first so the independent city itself gets picked.
        if enclave_matched is None and state_abbr and geocoded_city and is_independent_city(geocoded_city, state_abbr):
            _collision_filtered = [
                f for f in unique
                if not is_independent_city_county_collision(f["attributes"].get("POL_NAME1") or "", state_abbr)
            ]
            if _collision_filtered:
                unique = _collision_filtered

        geo_city_norm = _normalize_juris_name(geocoded_city) if geocoded_city else ""
        name_matched = None
        if enclave_matched is None and geo_city_norm:
            for f in unique:
                cand_name = _normalize_juris_name(f["attributes"].get("POL_NAME1") or "")
                if cand_name and (cand_name == geo_city_norm or
                                   cand_name in geo_city_norm or geo_city_norm in cand_name):
                    name_matched = f
                    break

        if enclave_matched is not None:
            best = enclave_matched
        elif name_matched is not None:
            best = name_matched
        else:
            city_features = [f for f in unique if _is_city_level(f["attributes"].get("POL_NAME1") or "")]
            best = city_features[0] if city_features else unique[0]
            candidate_names = ", ".join(
                (f["attributes"].get("POL_NAME1") or "?") for f in unique
            )
            community_confidence = "low"
            community_confidence_note = (
                f"Multiple overlapping jurisdictions found near this point "
                f"({candidate_names}) and none matched the geocoded city name "
                f"({geocoded_city or 'unknown'}). Defaulted to the incorporated "
                f"city result, but this may be incorrect — recommend verifying "
                f"the NFIP community against FEMA's Community Status Book "
                f"before relying on it."
            )

    # A non-numeric or non-standard CID (e.g. "48FED" for a federal military
    # reservation) is inherently a red flag: it means the resolved
    # "community" isn't a normal, independently-assigned NFIP jurisdiction.
    # A residential street address is very unlikely to genuinely sit on a
    # federal enclave (confirmed against test data: a normal city street
    # address in Killeen, TX resolved to "FORT HOOD" / CID "48FED" instead
    # of Killeen City) — flag it rather than presenting it with confidence.
    best_cid = (best["attributes"].get("CID") or "").strip()
    if best_cid and not re.fullmatch(r"\d{6}", best_cid):
        community_confidence = "low"
        community_confidence_note = (
            f"The resolved NFIP community ID ({best_cid!r}) is non-standard "
            f"— often indicating a federal enclave or reservation rather "
            f"than an ordinary incorporated city or county. If this "
            f"property is not actually located on federal land, recommend "
            f"verifying the correct civilian jurisdiction against FEMA's "
            f"Community Status Book."
        )
    
    attrs = best["attributes"]
    ani_tf = (attrs.get("ANI_TF") or "F").upper().strip()
    # ANI_TF = "T" means "Area Not Included" — community does NOT participate in NFIP
    nfip_participates = (ani_tf != "T")
    result = {
        "community_id": (attrs.get("CID") or "").strip(),
        "community_name": (attrs.get("POL_NAME1") or "").strip(),
        "nfip_participates": nfip_participates,
        "ani_tf": ani_tf,
        "community_confidence": community_confidence,
        "community_confidence_note": community_confidence_note,
    }
    print(f"NFIP community (Layer 22): {result['community_id']} / {result['community_name']} participates={nfip_participates} confidence={community_confidence}")
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
            "outFields": "*",
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
                print(f"[PANEL-DEBUG] Layer 3 query ({q['geometryType']}) returned 0 features "
                      f"for county_fips={county_fips!r} community_id={community_id!r}")
                continue

            print(f"[PANEL-DEBUG] Layer 3 query ({q['geometryType']}) returned "
                  f"{len(features)} feature(s) for county_fips={county_fips!r} community_id={community_id!r}:")
            for f in features:
                print(f"[PANEL-DEBUG]   ALL ATTRIBUTES: {f['attributes']}")

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
            dfirm = (attrs.get("DFIRM_ID") or "").strip()
            print(f"[PANEL-DEBUG] After filtering ({len(features)} candidate(s), matched={matched}): "
                  f"chosen FIRM_PAN={raw!r} DFIRM_ID={dfirm!r} PANEL_TYP={attrs.get('PANEL_TYP')!r}")

            # If more than one *distinct* FIRM panel number survived the
            # community/county filtering, the point is sitting near a panel
            # tile's grid boundary and the query genuinely returned adjacent
            # tiles as intersecting results — not just duplicate rows of the
            # same panel. We don't fetch full polygon geometry here
            # (returnGeometry=false), so we can't verify true point-in-polygon
            # containment among them. Rather than silently pick one (which is
            # a coin flip near the boundary), flag it so it can be verified.
            distinct_panels = {
                (f["attributes"].get("FIRM_PAN") or "").strip()
                for f in features
                if (f["attributes"].get("FIRM_PAN") or "").strip()
            }
            panel_confidence = "high"
            panel_confidence_note = ""
            if len(distinct_panels) > 1:
                # Genuine ambiguity: more than one FIRM panel tile actually
                # intersects this point. Attribute-only queries can't tell
                # us which one truly contains the point (vs. merely
                # touching its shared boundary edge) — that requires real
                # polygon geometry. Re-query the same location, this time
                # asking for geometry, and test strict point-in-polygon
                # containment directly instead of guessing.
                resolved = False
                if _SHAPELY_AVAILABLE:
                    try:
                        geom_params = {
                            **q,
                            "inSR": "4326",
                            "spatialRel": "esriSpatialRelIntersects",
                            "outFields": "FIRM_PAN,DFIRM_ID,PANEL_TYP",
                            "returnGeometry": "true",
                            "resultRecordCount": "10",
                            "f": "json",
                        }
                        async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
                            geom_resp = await client.get(f"{NFHL_BASE}/3/query", params=geom_params)
                            geom_resp.raise_for_status()
                            geom_data = geom_resp.json()
                        pt = _ShapelyPoint(lon, lat)
                        containing_attrs = None
                        for gf in geom_data.get("features", []):
                            rings = (gf.get("geometry") or {}).get("rings")
                            if not rings:
                                continue
                            try:
                                poly = _ShapelyPolygon(rings[0], rings[1:] if len(rings) > 1 else None)
                                if poly.is_valid and poly.contains(pt):
                                    containing_attrs = gf["attributes"]
                                    break
                            except Exception as poly_err:
                                print(f"[PANEL-DEBUG] Could not build polygon for one candidate: {poly_err}")
                                continue
                        if containing_attrs:
                            attrs = containing_attrs
                            print(f"[PANEL-DEBUG] Geometry check resolved tile-boundary ambiguity: "
                                  f"point-in-polygon confirms FIRM_PAN="
                                  f"{attrs.get('FIRM_PAN')!r}")
                            resolved = True
                        else:
                            print(f"[PANEL-DEBUG] Geometry check found no strict containment among "
                                  f"{len(distinct_panels)} candidates — point may sit exactly on a "
                                  f"shared tile boundary. Keeping low-confidence flag.")
                    except Exception as ge:
                        print(f"[PANEL-DEBUG] Geometry-based panel disambiguation failed: {ge}")

                if not resolved:
                    panel_confidence = "low"
                    panel_confidence_note = (
                        f"Property is near a FIRM panel tile boundary — {len(distinct_panels)} "
                        f"distinct panels ({', '.join(sorted(distinct_panels))}) intersect this "
                        f"location and the correct one could not be verified. Recommend "
                        f"confirming the panel number against the FEMA Map Service Center "
                        f"before relying on it."
                    )
                    # Recompute raw/dfirm from the (unresolved) attrs already
                    # selected above, unchanged from today's behavior.

            raw = (attrs.get("FIRM_PAN") or "").strip()
            dfirm = (attrs.get("DFIRM_ID") or "").strip()

            firm_pan = ""
            if raw:
                raw_clean = raw.replace(" ", "").strip().upper()
                dfirm_norm = (dfirm or "").strip().upper()
                # Compare only the county FIPS digits (first 5 characters) —
                # not the full 6-character string including the community
                # suffix letter. For some states' NFHL Layer 3 data (North
                # Carolina in particular — confirmed against test data where
                # every single NC address hit this path), the DFIRM_ID
                # attribute's suffix letter doesn't match FIRM_PAN's even
                # though both describe the same valid, correct panel. The
                # old 6-character exact-match check discarded the entire
                # FIRM_PAN in that case, which made determine_flood_info()
                # fall back to a fabricated "county-prefix + C" placeholder
                # that's missing the actual panel digits and suffix — i.e.
                # exactly the truncated community_number pattern seen across
                # ~25 NC test loans. The 5-digit county comparison still
                # catches genuine wrong-county mismatches (the real purpose
                # of this guard) without discarding valid same-county data.
                if dfirm_norm and raw_clean[:5] != dfirm_norm[:5]:
                    print(f"[PANEL] FIRM_PAN/DFIRM_ID county mismatch — discarding unreliable FIRM_PAN "
                          f"(FIRM_PAN={raw_clean!r} DFIRM_ID={dfirm_norm!r})")
                elif len(raw_clean) >= 7:
                    firm_pan = f"{raw_clean[:6]} {raw_clean[6:]}"
                else:
                    firm_pan = raw_clean

            # BEST-EFFORT, UNVERIFIED: some NC (and possibly other
            # "Statewide, Panel Printed") NFHL records use an internal
            # statewide FIRM_PAN numbering scheme that doesn't share a
            # prefix with DFIRM_ID at all (confirmed: Walkertown NC's
            # FIRM_PAN='3710686700J' vs DFIRM_ID='37067C' — not just a
            # suffix-letter mismatch, a genuinely different numbering
            # system). CoreLogic's format is DFIRM_ID + panel digits +
            # suffix (e.g. "37067C 6867J"). FEMA's NFHL schema commonly
            # carries the panel digits and suffix in separate PANEL/SUFFIX
            # attributes alongside FIRM_PAN. If present and populated, use
            # them to build the traditional format directly. This is safe
            # by construction: if these fields are absent or don't produce
            # a plausible value, firm_pan is left as computed above
            # (today's existing behavior) rather than being overwritten
            # with something worse.
            if dfirm:
                panel_num = str(attrs.get("PANEL") or attrs.get("PANEL_NO") or
                                 attrs.get("PANEL_NUM") or "").strip()
                suffix = str(attrs.get("SUFFIX") or attrs.get("SUFF") or
                             attrs.get("PANEL_SFX") or "").strip()
                if panel_num and suffix and panel_num.isdigit() and len(suffix) == 1 and suffix.isalpha():
                    candidate = f"{dfirm.strip().upper()} {panel_num.zfill(4)}{suffix.upper()}"
                    print(f"[PANEL-DEBUG] Constructed candidate from PANEL/SUFFIX fields: {candidate!r} "
                          f"(previous firm_pan was {firm_pan!r})")
                    firm_pan = candidate

            print(f"[PANEL-DEBUG] Final firm_panel_l3={firm_pan!r}")
            return {
                "firm_panel_l3": firm_pan,
                "eff_date": attrs.get("EFF_DATE"),
                "panel_confidence": panel_confidence,
                "panel_confidence_note": panel_confidence_note,
            }
        except Exception as e:
            print(f"FIRM panel query (Layer 3, {q['geometryType']}) error: {e}")

    # No features found at any query attempt (point or envelope fallback) —
    # a genuine FEMA data coverage gap (confirmed against real rural test
    # data: Toole County, MT had zero features here too). A bare {} would
    # let downstream code silently default panel_confidence back to "high"
    # even though we have no panel data at all.
    return {
        "firm_panel_l3": "",
        "eff_date": None,
        "panel_confidence": "low",
        "panel_confidence_note": (
            "No FIRM panel data is available from FEMA's live NFHL service "
            "for this location. This may be a rural or unmapped area not "
            "yet covered by FEMA's digital data. Verify manually via the "
            "FEMA Map Service Center (msc.fema.gov)."
        ),
    }


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
      1. Authoritative community_id_from_layer22 (from the actual FEMA NFHL
         spatial point-in-polygon query) — direct CID lookup, no guessing.
      2. Local nfip_communities_db.json, scoped to the correct county only.
      3. FEMA CSB API (fallback, may be unavailable)
    """
    import os, json as _json
    empty: dict = {"csb_community_id": "", "csb_community_name": ""}

    sa = state_abbr.upper().strip()
    if not sa and state_fips:
        _, sa = STATE_FIPS.get(state_fips, ("", ""))
    if not sa:
        return empty

    cid_wanted = (community_id_from_layer22 or "").strip()

    # ── 1. Local JSON DB ─────────────────────────────────────────────────────
    try:
        db_path = os.path.join(os.path.dirname(__file__), "nfip_communities_db.json")
        with open(db_path) as f:
            nfip_db = _json.load(f)

        # 1a. Authoritative CID match — the spatial query already told us
        # exactly which community polygon the point falls in. Trust that
        # over any name-based guessing. This must come first: it is the
        # only lookup here that is actually tied to the property's real
        # location rather than a text match on city name.
        if cid_wanted:
            for k, v in nfip_db.items():
                if not k.startswith(f"{sa}_"):
                    continue
                for c in v:
                    if (c.get("cid") or "").strip() == cid_wanted:
                        return {"csb_community_id": c.get("cid", ""),
                                "csb_community_name": c.get("name", ""),
                                "csb_panel": c.get("panel", ""),
                                "csb_panel_date": c.get("panel_date", "")}

        # Try exact county key: STATE_COUNTYCODE
        cfips = county_fips.zfill(3) if county_fips else ""
        key = f"{sa}_{cfips}" if cfips else None
        communities = nfip_db.get(key, []) if key else []
        # Whether this list is correctly scoped to the property's actual
        # county. Only a scoped list is safe input for the loose
        # "county/unincorporated" fallback below — an unscoped, merged,
        # state-wide list can match an unrelated county hundreds of miles
        # away just because its name contains "county".
        scoped_to_county = bool(communities)

        # If not found by exact county key, broaden the search — but only
        # for name matching, never for the loose county/unincorporated catch-all.
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
                            "csb_community_name": c.get("name", ""),
                            "csb_panel": c.get("panel", ""),
                            "csb_panel_date": c.get("panel_date", "")}
            # Partial match
            for c in communities:
                c_name = (c.get("name") or "").lower()
                if city_norm in c_name:
                    return {"csb_community_id": c.get("cid", ""),
                            "csb_community_name": c.get("name", ""),
                            "csb_panel": c.get("panel", ""),
                            "csb_panel_date": c.get("panel_date", "")}
        if communities and scoped_to_county:
            # County/unincorporated fallback — only safe when `communities`
            # is the correct county's own list (exact key match). If we had
            # to fall back to the whole state, skip this: picking "the first
            # entry with 'county' in its name" from a merged multi-county
            # list is a coin flip on the wrong county, not a real match.
            for c in communities:
                c_name = (c.get("name") or "").lower()
                if "county" in c_name or "unincorporated" in c_name:
                    return {"csb_community_id": c.get("cid", ""),
                            "csb_community_name": c.get("name", ""),
                            "csb_panel": c.get("panel", ""),
                            "csb_panel_date": c.get("panel_date", "")}
            # Single result in the correctly-scoped county list
            if len(communities) == 1:
                return {"csb_community_id": communities[0].get("cid", ""),
                        "csb_community_name": communities[0].get("name", ""),
                        "csb_panel": communities[0].get("panel", ""),
                        "csb_panel_date": communities[0].get("panel_date", "")}
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
        if cid_wanted:
            for c in communities_api:
                if (c.get("communityNumber") or "").strip() == cid_wanted:
                    return {"csb_community_id": c.get("communityNumber", ""),
                            "csb_community_name": c.get("communityName", "")}
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
    geocode_precision = (merged.get("geocode_precision") or "").strip()
    boundary_case   = bool(merged.get("boundary_case", False))

    # Zone confidence: a non-rooftop geocode near a zone boundary is exactly
    # the failure mode that produces a wrong AE/X call — the point can land
    # on either side of the line depending on interpolation error. Flag it
    # rather than silently reporting a single zone as if it were certain.
    if geocode_precision == "rooftop":
        zone_confidence = "high"
        zone_confidence_note = ""
    elif boundary_case:
        zone_confidence = "low"
        zone_confidence_note = (
            "Property is near a flood zone boundary and was geocoded with "
            "approximate (non-rooftop) precision. The zone call may be "
            "unreliable — recommend verifying against the FEMA Map Service "
            "Center or a rooftop-level geocode before relying on this "
            "determination."
        )
    else:
        zone_confidence = "medium"
        zone_confidence_note = (
            "Geocoded with approximate (non-rooftop) precision. Zone is not "
            "near a detected boundary, but verify for high-value or "
            "boundary-adjacent properties."
        )

    zone_data_available = merged.get("zone_data_available", True)

    if not zone_data_available:
        # FEMA's NFHL zone layer returned zero features at every sample
        # point — a genuine coverage gap (confirmed against real rural
        # test data), not a real "Zone X" determination. Asserting
        # "insurance not required" here would be presenting an unknown as
        # a negative determination, which is the one thing this app must
        # never silently do.
        flood_zone_out = "UNKNOWN"
        description = (
            "FEMA's live flood zone data has no coverage at this location. "
            "This does NOT mean the property is outside a flood hazard "
            "area — it means a determination could not be made from "
            "available digital data. Verify manually via the FEMA Map "
            "Service Center (msc.fema.gov) or FEMA's regional office "
            "before proceeding."
        )
        sfha_status = "Unknown"
        insurance_required = "Undetermined — manual verification required before proceeding"
        zone_confidence = "low"
        zone_confidence_note = (
            "No FEMA digital flood zone data is available for this "
            "location. This is a data coverage gap, not a confirmed "
            "determination — do not treat this as \"flood insurance not "
            "required\" without manual verification against FEMA's Map "
            "Service Center."
        )
    else:
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
    csb_panel = (merged.get("csb_panel") or "").strip()
    csb_panel_date = (merged.get("csb_panel_date") or "").strip()
    panel_effective_date_override = ""

    used_incomplete_placeholder = False

    # Prefer CSB historical community panel over Layer 3 countywide panel
    # when the CSB panel matches the community CID (community-specific panel)
    if csb_panel and community_id:
        cid_clean = community_id.replace(" ", "")
        pan_clean = csb_panel.replace(" ", "")
        if pan_clean.startswith(cid_clean[:6]):
            map_number = csb_panel
            if csb_panel_date:
                panel_effective_date_override = csb_panel_date
        elif has_full_l3_panel:
            map_number = firm_panel_l3
        elif esri_dfirm and len(esri_dfirm) >= 5:
            map_number = f"{esri_dfirm[:5]}C"
            used_incomplete_placeholder = True
        else:
            map_number = "Not Available"
    elif has_full_l3_panel:
        map_number = firm_panel_l3
    elif esri_dfirm and len(esri_dfirm) >= 5:
        map_number = f"{esri_dfirm[:5]}C"
        used_incomplete_placeholder = True
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

    # Independent cities are their own county-equivalent. FEMA/CoreLogic's
    # SFHDF convention uses "INDEPENDENT CITY" for the county field.
    county_name_raw = county_name
    state_abbr_for_indep = (merged.get("state_abbr") or "").strip()
    geocoded_city_for_indep = (merged.get("geocoded_city") or "").strip()
    if (is_independent_city(county_name, state_abbr_for_indep)
            or is_independent_city(geocoded_city_for_indep, state_abbr_for_indep)):
        county_name = "INDEPENDENT CITY"

    if panel_effective_date_override:
        panel_effective_date = panel_effective_date_override
    elif isinstance(eff_date_raw, str) and eff_date_raw:
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
        "county_raw": county_name_raw,
        "geocode_precision": geocode_precision or "unknown",
        "zone_confidence": zone_confidence,
        "zone_confidence_note": zone_confidence_note,
        "community_confidence": (merged.get("community_confidence") or "high"),
        "community_confidence_note": (merged.get("community_confidence_note") or ""),
        "panel_confidence": ("low" if used_incomplete_placeholder else (merged.get("panel_confidence") or "high")),
        "panel_confidence_note": (
            "The full FIRM panel number could not be retrieved for this "
            "property, so the community/county prefix is shown without the "
            "panel digits and suffix that CoreLogic and other providers "
            "include (e.g. '37067C' instead of '37067C 6867J'). Recommend "
            "confirming the complete panel number against the FEMA Map "
            "Service Center before relying on it."
        ) if used_incomplete_placeholder else (merged.get("panel_confidence_note") or ""),
    }


# ── LOMA / LOMR lookup ────────────────────────────────────────────────────────

# Full-removal outcomes that are safe to auto-apply as an SFHA override --
# anything else (partial removal, unknown, or a supersession/reevaluation
# flag) must go to manual review instead. See REAL_LOMC field notes below.
_LOMA_AUTO_REMOVAL_OUTCOMES = {"Property removed", "Property out as shown"}

# REVAL_STAT values that mean this determination should NOT be trusted as
# the final word on its own -- confirmed against live FEMA Layer 34 data.
_LOMA_SUPERSEDED_REVAL_STATES = {"Superseded", "Reevaluated", "Contact Community"}


async def check_loma_at_point(lat: float, lon: float) -> dict | None:
    """
    Check for a FEMA map-change determination at the given coordinates.

    IMPORTANT -- confirmed against live FEMA NFHL data (Aug 2026): the field
    names and layer this function used previously (Layer 4 "Base Index",
    fields CASE_NO/STATUS/OUT_ZONE/EFF_DATE/AMEND_TYPE) do not exist on any
    real NFHL layer -- Layer 4 is the DFIRM base-map file index, unrelated
    to map changes. The correct layers, confirmed via FEMA's own MapServer
    metadata, are:
      - Layer 34 ("LOMAs"): actually covers LOMA, LOMR-F, LOMR-FW, and
        LOMR-VZ determinations, discriminated by the PROJECTCATEGORY field.
        Every sampled record has STATUS='Completed' -- CLOMRs (conditional,
        not-yet-effective) are NOT present in this dataset at all, since
        FEMA only publishes finalized determinations here. There is no
        structured "revised zone" field (no OUT_ZONE-equivalent) -- OUTCOME
        is free text, so this function never fabricates a specific zone
        code; it only recommends a binary in/out-of-SFHA override, and only
        for outcomes that are unambiguous.
      - Layer 1 ("LOMRs"): revises the base FIRM directly. Once effective,
        the current NFHL flood-zone layer (Layer 28, via query_fema_nfhl)
        already reflects it -- so a LOMR match is informational evidence
        only, never an override target here.

    Returns a dict shaped:
      {
        "loma": {...} | None,   # LOMA/LOMR-F/LOMR-FW/LOMR-VZ match, if any
        "lomr": {...} | None,   # LOMR match, if any (informational only)
      }
    or None if neither layer returned a match and the cache was empty.

    "loma" dict fields (all straight from FEMA, nothing inferred beyond the
    auto_removal_eligible flag):
      case_number, project_category, status, outcome, reval_stat,
      community_id, community_name, date_ended, pdf_link,
      auto_removal_eligible (bool), supersession_flag (bool)

    "lomr" dict fields:
      case_number, status, eff_date, dfirm_id, note
    """
    from datetime import datetime, timezone

    def _epoch_ms_to_iso(ms):
        if not ms:
            return None
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    def _build_loma_result(attrs: dict) -> dict:
        outcome = (attrs.get("OUTCOME") or "").strip()
        reval_stat = (attrs.get("REVAL_STAT") or "").strip()
        case_no = attrs.get("CASENUMBER")
        pdf_id = attrs.get("PDFHYPERLINKID")
        supersession_flag = reval_stat in _LOMA_SUPERSEDED_REVAL_STATES
        auto_eligible = (outcome in _LOMA_AUTO_REMOVAL_OUTCOMES) and not supersession_flag
        return {
            "case_number":       case_no,
            "project_category":  attrs.get("PROJECTCATEGORY"),
            "status":            attrs.get("STATUS"),
            "outcome":           outcome or None,
            "reval_stat":        reval_stat or None,
            "community_id":      attrs.get("CID"),
            "community_name":    attrs.get("COMMUNITYNAME"),
            "date_ended":        _epoch_ms_to_iso(attrs.get("DATEENDED")),
            "pdf_link": (
                f"https://msc.fema.gov/portal/downloadProduct?productID={pdf_id}"
                if pdf_id else None
            ),
            "auto_removal_eligible": auto_eligible,
            "supersession_flag":     supersession_flag,
        }

    def _build_lomr_result(attrs: dict) -> dict:
        return {
            "case_number": attrs.get("CASE_NO"),
            "status":      attrs.get("STATUS"),
            "eff_date":    _epoch_ms_to_iso(attrs.get("EFF_DATE")),
            "dfirm_id":    attrs.get("DFIRM_ID"),
            "note": (
                "This LOMR revises the base FIRM directly -- the current "
                "flood zone determination already reflects it. Shown here "
                "as supporting evidence, not applied as a separate override."
            ),
        }

    # ── 1. Local DB cache (manually entered / previously fetched) ────────────
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT case_number, project_category, status, outcome,
                           reval_stat, community_id, community_name,
                           date_ended, pdf_link, record_type
                    FROM loma_records
                    WHERE ABS(lat - %s) < 0.005
                      AND ABS(lon - %s) < 0.005
                    ORDER BY looked_up_at DESC
                    LIMIT 1
                """, (lat, lon))
                row = cur.fetchone()
                if row:
                    print(f"[LOMA] Cache hit: {row['case_number']} at ({lat},{lon})")
                    outcome = row.get("outcome") or ""
                    reval_stat = row.get("reval_stat") or ""
                    supersession_flag = reval_stat in _LOMA_SUPERSEDED_REVAL_STATES
                    auto_eligible = (outcome in _LOMA_AUTO_REMOVAL_OUTCOMES) and not supersession_flag
                    loma_result = {
                        "case_number":       row["case_number"],
                        "project_category":  row.get("project_category"),
                        "status":            row.get("status"),
                        "outcome":           outcome or None,
                        "reval_stat":        reval_stat or None,
                        "community_id":      row.get("community_id"),
                        "community_name":    row.get("community_name"),
                        "date_ended":        str(row["date_ended"]) if row.get("date_ended") else None,
                        "pdf_link":          row.get("pdf_link"),
                        "auto_removal_eligible": auto_eligible,
                        "supersession_flag":     supersession_flag,
                    }
                    return {"loma": loma_result, "lomr": None}
    except Exception as e:
        print(f"[LOMA] DB cache check failed (non-fatal): {e}")

    # ── 2. Live FEMA NFHL API -- Layer 34 (LOMA/LOMR-F/LOMR-FW/LOMR-VZ) ──────
    loma_result = None
    try:
        url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/34/query"
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "CASENUMBER,STATUS,PROJECTCATEGORY,DATEENDED,CID,"
                          "COMMUNITYNAME,REVAL_STAT,OUTCOME,PDFHYPERLINKID",
            "returnGeometry": "false",
            "f": "json",
        }
        async with httpx.AsyncClient(timeout=12, verify=False) as client:
            r = await client.get(url, params=params)
            data = r.json()
        features = data.get("features", [])
        if features:
            attrs = features[0]["attributes"]
            loma_result = _build_loma_result(attrs)
            print(f"[LOMA] Layer 34 result: {loma_result['case_number']} "
                  f"category={loma_result['project_category']} "
                  f"outcome={loma_result['outcome']!r} "
                  f"auto_eligible={loma_result['auto_removal_eligible']}")

            if loma_result.get("case_number"):
                try:
                    from db import get_conn
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO loma_records
                                    (case_number, lat, lon, project_category,
                                     status, outcome, reval_stat, community_id,
                                     community_name, date_ended, pdf_link,
                                     record_type, source)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'LOMA', 'FEMA_API')
                                ON CONFLICT (case_number) DO NOTHING
                            """, (
                                loma_result["case_number"], lat, lon,
                                loma_result["project_category"], loma_result["status"],
                                loma_result["outcome"], loma_result["reval_stat"],
                                loma_result["community_id"], loma_result["community_name"],
                                loma_result["date_ended"], loma_result["pdf_link"],
                            ))
                except Exception as e:
                    print(f"[LOMA] Cache write failed (non-fatal): {e}")
    except Exception as e:
        print(f"[LOMA] Layer 34 API check failed: {e}")

    # ── 3. Live FEMA NFHL API -- Layer 1 (LOMRs, informational only) ─────────
    lomr_result = None
    try:
        url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/1/query"
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "CASE_NO,STATUS,EFF_DATE,DFIRM_ID",
            "returnGeometry": "false",
            "f": "json",
        }
        async with httpx.AsyncClient(timeout=12, verify=False) as client:
            r = await client.get(url, params=params)
            data = r.json()
        features = data.get("features", [])
        if features:
            lomr_result = _build_lomr_result(features[0]["attributes"])
            print(f"[LOMA] Layer 1 (LOMR) result: {lomr_result['case_number']} "
                  f"eff_date={lomr_result['eff_date']}")
    except Exception as e:
        print(f"[LOMA] Layer 1 API check failed: {e}")

    if loma_result is None and lomr_result is None:
        return None
    return {"loma": loma_result, "lomr": lomr_result}
        