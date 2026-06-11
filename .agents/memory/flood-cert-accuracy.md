---
name: Flood cert data accuracy fixes
description: Root causes and fixes for CoreLogic data mismatches in the FEMA Flood Certificate Generator.
---

## Wrong county FIPS in DFIRM_ID prefix (Map Number)

**Root cause:** Esri Living Atlas "Reduced Set" flood zone layer returns DFIRM_ID that reflects the wrong county for properties near county/city boundaries.

**Fix:** Switch `geocode_address` from Census `locations` endpoint to `geographies` endpoint (with `vintage=Census2020&layers=Counties`). This returns `geographies.Counties[0].GEOID` — the authoritative 5-digit state+county FIPS. Use that to construct DFIRM_ID prefix as `{state_fips}{county_fips_3digit}C`. Pass `state_fips`, `county_fips`, `county_name` from geocoder into `determine_flood_info`.

**Why:** Census geocoder's county FIPS is authoritative; Esri's spatial query can snap to the wrong polygon near boundaries (e.g. Bulverde TX → Kendall Co instead of Comal Co).

## hazards.fema.gov is TCP-blocked server-side

Layer 3 (FIRM panels) and Layer 22 (political jurisdictions/CID) are both at hazards.fema.gov which issues connection reset at the TCP level — `verify=False` does NOT help. The fix is client-side JS enrichment: the browser CAN reach hazards.fema.gov fine.

**Single certs:** JS in result.html fetches both Layer 3 (FIRM_PAN, EFF_DATE) and Layer 22 (CID, POL_NAME1) from the browser and updates display + hidden form inputs.

**Batch certs:** Server-side only — panel suffix and exact municipal CID may be missing; DFIRM_ID prefix is accurate via geocoder county FIPS.

## Zone X vs X500

When Esri layer returns `FLD_ZONE=X` with `ZONE_SUBTY` containing `"0.2"`, that is the shaded Zone X (500-year floodplain). Store and display as `"X500"`. CoreLogic uses `"X500"` for this designation. Not an SFHA zone.

## County name showing "Planning Region" (Connecticut etc.)

TIGERweb returns planning region names for some CT counties. Fix: use `NAME` from Census geocoder geography response as primary county name source. TIGERweb is fallback only (`tigerweb_county` key).

## Community Number (CID) fallback

When Layer 22 fails server-side, approximate with `{state_fips}{county_fips}0` (6 digits). This is the county-level CID format — municipal CIDs differ and require Layer 22. The client-side JS Layer 22 enrichment provides the correct municipal CID when the certificate page is viewed in a browser.

## Nominatim fallback geocoder

Added `https://nominatim.openstreetmap.org/search` as 3rd fallback after both Census endpoints fail (e.g. addresses with "Lot N" or unusual formats). Returns no county FIPS but provides lat/lon.

## Life-of-loan email notification

`send_redetermination_notification(record)` in `email_sender.py` sends lender email when FIRM panel change detected. Called from `check_fema_update` (single record) and `check_all_monitored` (bulk). Only fires when `record["lender_email"]` is set. Check result now includes `"notification_sent": bool`.

## Batch CSV county field

`county` was missing from `out_columns` in the `/batch` route. Added.

## Field name reminder (confusing naming)

- `panel_number` in DB/templates = NFIP **Community Number** (CID, e.g. "480301")
- `community_number` in DB/templates = NFIP **Map Number** / Community-Panel Number (e.g. "48091C 0215F")
