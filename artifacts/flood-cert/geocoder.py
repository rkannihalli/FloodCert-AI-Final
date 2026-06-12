import httpx, math, logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)
ARCGIS_MIN_SCORE = 85
MAX_FALLBACK_DIST_M = 500

@dataclass
class GeocodeResult:
    lat: float
    lon: float
    matched_address: str
    score: float
    source: str
    is_rooftop: bool

def _geocode_arcgis(address):
    url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
    params = {"SingleLine": address, "outFields": "Match_addr,Addr_type,Score",
              "maxLocations": 1, "f": "json", "countryCode": "USA"}
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        cands = data.get("candidates", [])
        if not cands: return None
        cand = cands[0]
        score = float(cand.get("score", 0))
        if score < ARCGIS_MIN_SCORE: return None
        loc = cand["location"]
        atype = cand.get("attributes", {}).get("Addr_type", "")
        return GeocodeResult(lat=loc["y"], lon=loc["x"],
            matched_address=cand.get("address", address), score=score,
            source="arcgis", is_rooftop=(atype in ("PointAddress","Subaddress")))
    except Exception as e:
        logger.error("ArcGIS error: %s", e); return None

def _geocode_census(address):
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": address, "benchmark": "Public_AR_Current", "format": "json"}
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            matches = r.json().get("result", {}).get("addressMatches", [])
        if not matches: return None
        m = matches[0]; coords = m["coordinates"]
        return GeocodeResult(lat=coords["y"], lon=coords["x"],
            matched_address=m.get("matchedAddress", address),
            score=95.0, source="census", is_rooftop=True)
    except Exception as e:
        logger.error("Census error: %s", e); return None

def _geocode_nominatim(address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "addressdetails": 1,
              "limit": 1, "countrycodes": "us"}
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": "FloodCertAI/1.0"}) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            results = r.json()
        if not results: return None
        res = results[0]
        score = min(float(res.get("importance", 0)) * 100, 70.0)
        return GeocodeResult(lat=float(res["lat"]), lon=float(res["lon"]),
            matched_address=res.get("display_name", address), score=score,
            source="nominatim", is_rooftop=(res.get("type") in ("house","building")))
    except Exception as e:
        logger.error("Nominatim error: %s", e); return None

def _dist_m(la1, lo1, la2, lo2):
    R = 6_371_000
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin(math.radians(la2-la1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(lo2-lo1)/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def geocode_address(address):
    result = _geocode_arcgis(address)
    if result and result.score >= ARCGIS_MIN_SCORE: return result
    plat = result.lat if result else None
    plon = result.lon if result else None
    result = _geocode_census(address)
    if result: return result
    nom = _geocode_nominatim(address)
    if nom and nom.score >= 50:
        if plat is not None and _dist_m(plat, plon, nom.lat, nom.lon) > MAX_FALLBACK_DIST_M:
            return None
        return nom
    return None
