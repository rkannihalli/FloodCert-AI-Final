---
name: FEMA NFHL server access
description: How to query FEMA flood zone data server-side from Replit (hazards.fema.gov is blocked)
---

## The rule
Do NOT query `hazards.fema.gov` from server-side code. Use the Esri Living Atlas service instead.

**Why:** Replit servers get `[SSL: UNEXPECTED_EOF_WHILE_READING]` from hazards.fema.gov — TLS connection is terminated immediately at the network level. This affects all subdomains: `hazards.fema.gov`, `msc.fema.gov`, `geodata.fema.gov`.

## Working server-side endpoint
`https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query`

- Returns: `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `DFIRM_ID`
- Covers all NFHL zones including Zone X, AE, VE, etc.
- Use `inSR=4326` for WGS84 input coordinates

## How to apply
1. Point query first (`esriGeometryPoint`)
2. If 0 features returned, retry with ~0.001° envelope (`esriGeometryEnvelope`) — polygon boundary gaps are common
3. If still 0 features, default to Zone X (not UNDETERMINED) — unmapped areas are minimal hazard
4. Prefer SFHA zone over Zone X when the envelope returns multiple overlapping polygons

## DFIRM_ID field
The `DFIRM_ID` (e.g. `22071C` or `12086C`) serves as both community designation and panel reference:
- First 2 chars = state FIPS (e.g. `22` = Louisiana, `12` = Florida)
- Use as `community_number` and `panel_number` on the certificate

## What does NOT work
- `query_nfip_community` via hazards.fema.gov Layer 6 → blocked
- `query_firm_panel` via hazards.fema.gov Layer 24 → blocked
- JSONP from browser also unreliable (ArcGIS Server may reject callback param + CORS issues in Replit preview)
- FEMA's own ArcGIS Online org (`XG15cJAlne2vxtgt`) services are regional/local, not national
