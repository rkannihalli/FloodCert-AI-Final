---
name: FEMA NFHL server access
description: How to query FEMA flood zone data server-side from Replit (hazards.fema.gov and fema.gov APIs are blocked)
---

## The rule
Do NOT query `hazards.fema.gov`, `msc.fema.gov`, or `www.fema.gov/api` from server-side code.
All FEMA-hosted services are TLS-blocked on Replit. Use the Esri Living Atlas or Census services instead.

**Why:** Replit servers get `[SSL: UNEXPECTED_EOF_WHILE_READING]` from hazards.fema.gov — TLS connection is terminated immediately. Same affects `msc.fema.gov` and `www.fema.gov/api/open/...` (OpenFEMA API returns 404 HTML, not JSON).

## Working server-side endpoints
- **Flood zones:** Esri Living Atlas `USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query`
  - Returns: `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `DFIRM_ID`
- **County name:** Census TIGERweb `State_County/MapServer/1/query`
- **Geocoding:** Census geographies/locations, then Nominatim (OSM) fallback
- **NFIP Community number:** browser-side Layer 22 enrichment (Layer 22 of NFHL) — must be in JS

## DFIRM_ID usage (important priority rule)
The Esri Living Atlas `DFIRM_ID` comes from a **spatial intersection** of real NFHL flood zone polygons.
It carries the correct county FIPS even for cross-county municipalities (e.g. Summerville SC → 45035, not 45019).

**Priority for NFIP Map Number (panel prefix):**
1. Layer 3 full panel number (NFHL FIRM Panels, browser-side only)
2. **Esri DFIRM_ID first 5 chars + "C"** ← MOST RELIABLE server-side
3. Census geocoder county FIPS (fallback — may be wrong near county lines)

This order fixed: Summerville SC (45019→45035), Bulverde TX (48259→48091), Dalton GA, Peru IN.

## What does NOT work server-side
- `query_nfip_community` via hazards.fema.gov Layer 22 → blocked
- `query_firm_panel` via hazards.fema.gov Layer 3 → blocked
- `query_nfip_community_csb` via www.fema.gov/api → 404 (URL wrong or service blocked)
- `msc.fema.gov` → TLS blocked
- NFIP Community Number (CID) accuracy relies on browser-side Layer 22 enrichment

## Connecticut county fix
Connecticut uses planning regions in Census/TIGERweb data instead of traditional counties.
Use `CT_PLANNING_REGION_TO_COUNTY` dict in `fema_lookup.py` to map region names → county names.
Apply in `determine_flood_info()` after county_name is resolved.
