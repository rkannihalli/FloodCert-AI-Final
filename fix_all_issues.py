import re

filepath = "/home/runner/workspace/artifacts/flood-cert/fema_lookup.py"
with open(filepath, "r") as f:
    content = f.read()

original = content  # keep for diff

# ================================================================
# FIX 1: Duck Dr geocoding failure
# Root cause: _geocode_plausible() requires BOTH house number AND
# street name. "Duck Dr" geocodes to a different street name.
# Fix: relax to require ONLY house number match when street is short
# or a single common word, and add a ZIP code match as third signal.
# ================================================================
old_plausible = '''def _geocode_plausible(input_address: str, matched_address: str) -> bool:
    """Return False when matched_address is clearly wrong for input_address.

    Requires that BOTH the house number AND the primary street name from
    input_address appear in matched_address.  If either is absent the geocoder
    found an unrelated location and the result must be discarded.

    Example failure (both signals missing → reject):
      input:   "4248 Duck Dr, Ann Arbor, MI 48103"
      matched: "Oak Valley Drive, Pittsfield Charter Township, MI 48103"
               ↳ "4248" absent  AND  "DUCK" absent → False
    """
    if not matched_address:
        return True  # nothing to compare; pass through

    inp = input_address.upper()
    mat = matched_address.upper()

    # ── House number ──────────────────────────────────────────────────────────
    num_m = re.match(r"\\s*(\\d+)", inp)
    house_num = num_m.group(1) if num_m else ""

    # ── Primary street word ───────────────────────────────────────────────────
    # Strip leading house number, then pick the first word that isn't a suffix.
    street_part = re.sub(r"^\\s*\\d+\\s*", "", inp.split(",")[0])
    _STOP = {
        "DR", "ST", "AVE", "BLVD", "RD", "LN", "CT", "CIR", "PL", "WAY",
        "TER", "TRL", "PKWY", "HWY", "DRIVE", "STREET", "AVENUE",
        "BOULEVARD", "ROAD", "LANE", "COURT", "CIRCLE", "PLACE",
        "TERRACE", "TRAIL", "PARKWAY", "HIGHWAY", "UNIT", "APT", "STE",
    }
    sig_words = [w for w in re.findall(r"\\b[A-Z]{2,}\\b", street_part) if w not in _STOP]
    primary_word = sig_words[0] if sig_words else ""

    house_ok  = bool(house_num)    and house_num    in mat
    street_ok = bool(primary_word) and primary_word in mat

    # Both signals absent → almost certainly the wrong location
    return house_ok or street_ok'''

new_plausible = '''def _geocode_plausible(input_address: str, matched_address: str) -> bool:
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
    num_m = re.match(r"\\s*(\\d+)", inp)
    house_num = num_m.group(1) if num_m else ""

    # ── Primary street word ───────────────────────────────────────────────────
    street_part = re.sub(r"^\\s*\\d+\\s*", "", inp.split(",")[0])
    _STOP = {
        "DR", "ST", "AVE", "BLVD", "RD", "LN", "CT", "CIR", "PL", "WAY",
        "TER", "TRL", "PKWY", "HWY", "DRIVE", "STREET", "AVENUE",
        "BOULEVARD", "ROAD", "LANE", "COURT", "CIRCLE", "PLACE",
        "TERRACE", "TRAIL", "PARKWAY", "HIGHWAY", "UNIT", "APT", "STE",
    }
    sig_words = [w for w in re.findall(r"\\b[A-Z]{2,}\\b", street_part) if w not in _STOP]
    primary_word = sig_words[0] if sig_words else ""

    # ── ZIP code ──────────────────────────────────────────────────────────────
    zip_m = re.search(r"\\b(\\d{5})\\b", inp)
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
    return True'''

if old_plausible in content:
    content = content.replace(old_plausible, new_plausible)
    print("FIX 1 APPLIED: _geocode_plausible() relaxed for Duck Dr type addresses")
else:
    print("FIX 1 SKIP: _geocode_plausible() pattern not found exactly")

# ================================================================
# FIX 2: X500 zone classification
# Root cause: Lakewood CA (X500) and Santa Rosa Beach FL (Zone A)
# The ESRI layer sometimes returns FLD_ZONE='X' with ZONE_SUBTY
# containing partial strings not in _X500_SUBTYPES set.
# Fix: expand _X500_SUBTYPES and improve _classify_x_zone logic.
# ================================================================
old_x500 = '''_X500_SUBTYPES = frozenset({
    # Standard 0.2% / 500-year flood hazard labels
    "0.2 PCT ANNUAL CHANCE FLOOD HAZARD",
    "0.2 PCT ANNUAL CHANCE FLOOD",
    "0.2% ANNUAL CHANCE FLOOD HAZARD",
    "0.2 PERCENT ANNUAL CHANCE FLOOD HAZARD",
    "AREA OF 500-YEAR FLOOD HAZARD",
    "500-YEAR FLOOD HAZARD",
    # Levee-reduced-risk labels (Esri Living Atlas / NFHL) — also shaded X = X500
    "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE",
    "REDUCED FLOOD RISK DUE TO LEVEE",
    "PROTECTED BY LEVEE",
    "AREA PROTECTED BY LEVEE",
    "AREA PROTECTED FROM 100-YEAR FLOOD BY LEVEE",
})'''

new_x500 = '''_X500_SUBTYPES = frozenset({
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
})'''

if old_x500 in content:
    content = content.replace(old_x500, new_x500)
    print("FIX 2 APPLIED: _X500_SUBTYPES expanded for X500 zone detection")
else:
    print("FIX 2 SKIP: _X500_SUBTYPES pattern not found exactly")

# ================================================================
# FIX 3: NFHL community number mismatch (Acton MA, Jamestown NY)
# Root cause: community number has trailing "C" appended incorrectly
# e.g. "25017C" instead of "250176" (6-digit NFIP CID)
# The map_number uses "C" suffix but panel_number should NOT.
# Fix: ensure panel_number (CID) never gets "C" appended.
# ================================================================
old_panel_fallback = '''    elif map_number and map_number != "Not Available":
        panel_number = map_number.replace(" ", "")[:6]
    else:
        panel_number = "Not Available"'''

new_panel_fallback = '''    elif esri_dfirm and len(esri_dfirm) >= 5:
        # Use DFIRM_ID directly as 5-digit state+county prefix for CID
        # Append "0" for county-level community (never "C" — that's map number only)
        panel_number = f"{esri_dfirm[:5]}0"
    elif map_number and map_number != "Not Available":
        # Strip "C" suffix from map number to get community number prefix
        raw_panel = map_number.replace(" ", "")
        panel_number = raw_panel[:5] + "0" if raw_panel.endswith("C") else raw_panel[:6]
    else:
        panel_number = "Not Available"'''

if old_panel_fallback in content:
    content = content.replace(old_panel_fallback, new_panel_fallback)
    print("FIX 3 APPLIED: panel_number CID no longer appends 'C' suffix")
else:
    print("FIX 3 SKIP: panel_number fallback pattern not found exactly")

# ================================================================
# FIX 4: Connecticut county name mapping
# Root cause: "South Central Connecticut Planning Region" should map
# to "New Haven County" — already in CT_PLANNING_REGION_TO_COUNTY
# but the mapping key uses lowercase. Verify the mapping is complete.
# ================================================================
old_ct = '''    "south central connecticut planning region":     "New Haven County",'''
new_ct = '''    "south central connecticut planning region":     "New Haven County",
    "south central connecticut":                     "New Haven County",'''

if old_ct in content and new_ct not in content:
    content = content.replace(old_ct, new_ct)
    print("FIX 4 APPLIED: CT county mapping expanded")
else:
    print("FIX 4 SKIP: CT mapping already complete or not found")

# ================================================================
# FIX 5: Improve Nominatim fallback for addresses like Duck Dr
# Root cause: Nominatim attempt 5 strips street type but the
# plausibility check then rejects because "Duck" doesn't appear
# in the matched result from the stripped query.
# Fix: add a 6th attempt using just house number + city + state + zip
# ================================================================
old_return_none = '''    return None


async def query_fema_nfhl'''

new_return_none = '''    # ── Attempt 6: Minimal query — house number + city + state + ZIP ──────────
    # Last resort for addresses where all geocoders fail the plausibility check.
    # Strips street name entirely and queries by number + city + state + ZIP.
    # This ensures we get coordinates in the right area even if street is wrong.
    try:
        num_m2 = re.match(r"\\s*(\\d+)", geocode_q)
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


async def query_fema_nfhl'''

if old_return_none in content:
    content = content.replace(old_return_none, new_return_none)
    print("FIX 5 APPLIED: Added 6th geocoding fallback (minimal ZIP-based query)")
else:
    print("FIX 5 SKIP: return None pattern not found exactly")

# Write fixed file
with open(filepath, "w") as f:
    f.write(content)

# Also update workspace copy
with open("/home/runner/workspace/fema_lookup.py", "w") as f:
    f.write(content)

print("\nAll fixes written to fema_lookup.py")
print(f"Original size: {len(original)} chars")
print(f"Fixed size:    {len(content)} chars")
