"""
map_utils.py — Generate composite static map images for flood certificates.

Fetches ESRI World Imagery (base) + FEMA NFHL flood zone overlay, composites
with Pillow, draws a location pin, and returns a base64 JPEG string.
"""

import asyncio
import base64
import io
import logging
from typing import Optional

import httpx
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

ESRI_IMAGERY_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/export"
)
NFHL_EXPORT_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/export"
)

MAP_WIDTH = 800
MAP_HEIGHT = 480
BBOX_PAD = 0.006   # ~650m half-width — tighter zoom for precise property location

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BUILDING_SEARCH_RADIUS_M = 60  # search radius for nearest building footprint


def _bbox(lat: float, lon: float) -> str:
    return f"{lon - BBOX_PAD},{lat - BBOX_PAD},{lon + BBOX_PAD},{lat + BBOX_PAD}"


async def generate_map_image(lat: float, lon: float) -> Optional[str]:
    """
    Return base64-encoded JPEG of composited imagery + NFHL + pin, or None on failure.
    """
    bbox = _bbox(lat, lon)
    size_str = f"{MAP_WIDTH},{MAP_HEIGHT}"
    common = {"bbox": bbox, "bboxSR": "4326", "size": size_str, "imageSR": "4326", "f": "image"}

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # Fetch imagery and NFHL overlay concurrently; NFHL failure is non-fatal
            try:
                img_resp, nfhl_resp = await asyncio.gather(
                    client.get(ESRI_IMAGERY_URL, params={**common, "format": "png"}),
                    client.get(NFHL_EXPORT_URL,  params={**common, "format": "png32", "transparent": "true"}),
                )
                nfhl_bytes = nfhl_resp.content
            except Exception:
                # NFHL overlay failed — fetch imagery only
                img_resp = await client.get(ESRI_IMAGERY_URL, params={**common, "format": "png"})
                nfhl_bytes = None

        base_img = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
        base_img = base_img.resize((MAP_WIDTH, MAP_HEIGHT), Image.LANCZOS)

        if nfhl_bytes:
            try:
                nfhl_img = Image.open(io.BytesIO(nfhl_bytes)).convert("RGBA")
                nfhl_img = nfhl_img.resize((MAP_WIDTH, MAP_HEIGHT), Image.LANCZOS)
                composite = Image.alpha_composite(base_img, nfhl_img)
            except Exception:
                composite = base_img
        else:
            composite = base_img

        footprint = await _fetch_building_footprint(lat, lon)
        if footprint:
            _draw_footprint(composite, lat, lon, footprint)

        _draw_pin(composite, MAP_WIDTH // 2, MAP_HEIGHT // 2)
        _draw_coord_label(composite, lat, lon)

        out = io.BytesIO()
        composite.convert("RGB").save(out, format="JPEG", quality=88)
        return base64.b64encode(out.getvalue()).decode()

    except Exception as exc:
        logger.warning("Map image generation failed: %s", exc)
        return None


async def _fetch_building_footprint(lat: float, lon: float) -> "Optional[list]":
    """
    Query OpenStreetMap (via the Overpass API) for the building footprint
    nearest the given point, within a small search radius. Returns a list
    of (lat, lon) tuples forming the closed polygon, or None if no building
    is found nearby or the lookup fails.

    Non-fatal by design, matching the NFHL-overlay fallback already used in
    this module: OSM building coverage is inconsistent, especially in rural
    areas, so a None result here is common and expected, not an error -- the
    map still renders correctly without a footprint overlay.
    """
    query = (
        '[out:json][timeout:15];'
        'way["building"](around:%s,%s,%s);'
        'out geom;'
    ) % (BUILDING_SEARCH_RADIUS_M, lat, lon)
    try:
        async with httpx.AsyncClient(timeout=18.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            data = resp.json()
        elements = [el for el in data.get("elements", []) if el.get("geometry")]
        if not elements:
            return None

        def _centroid(geom):
            lats = [n["lat"] for n in geom]
            lons = [n["lon"] for n in geom]
            return (sum(lats) / len(lats), sum(lons) / len(lons))

        def _dist2(pt):
            return (pt[0] - lat) ** 2 + (pt[1] - lon) ** 2

        best = min(elements, key=lambda el: _dist2(_centroid(el["geometry"])))
        return [(n["lat"], n["lon"]) for n in best["geometry"]]
    except Exception as e:
        logger.info("Building footprint lookup failed (non-fatal): %s", e)
        return None


def _draw_footprint(img, center_lat, center_lon, footprint):
    """Draw the building footprint polygon outline (and light fill) on the
    composite image, using the same linear bbox-to-pixel mapping the ArcGIS
    export services already use for this image (bboxSR=4326, unprojected)."""
    draw = ImageDraw.Draw(img, "RGBA")
    half = BBOX_PAD
    pts = []
    for flat, flon in footprint:
        x = (flon - (center_lon - half)) / (2 * half) * MAP_WIDTH
        y = (half + center_lat - flat) / (2 * half) * MAP_HEIGHT
        pts.append((x, y))
    if len(pts) >= 3:
        draw.polygon(pts, fill=(255, 215, 0, 60), outline=(255, 215, 0, 255), width=3)


def _draw_pin(img: Image.Image, cx: int, cy: int) -> None:
    """Draw a precise property marker: crosshair + teardrop pin."""
    draw = ImageDraw.Draw(img)

    # Crosshair lines for precision
    cross_len = 18
    draw.line([(cx - cross_len, cy), (cx - 6, cy)], fill=(255, 50, 50, 220), width=2)
    draw.line([(cx + 6, cy), (cx + cross_len, cy)], fill=(255, 50, 50, 220), width=2)
    draw.line([(cx, cy - cross_len), (cx, cy - 6)], fill=(255, 50, 50, 220), width=2)
    draw.line([(cx, cy + 6), (cx, cy + cross_len)], fill=(255, 50, 50, 220), width=2)

    # White halo behind pin
    r = 12
    draw.ellipse([cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3], fill=(255, 255, 255, 220))
    # Red circle body
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(210, 35, 35, 255))
    # White inner dot
    draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(255, 255, 255, 255))
    # Stem
    stem_top_y = cy + r - 3
    stem_tip_y = cy + r + 20
    draw.polygon(
        [(cx - 5, stem_top_y), (cx + 5, stem_top_y), (cx, stem_tip_y)],
        fill=(210, 35, 35, 255),
    )
    draw.line([(cx - 5, stem_top_y), (cx, stem_tip_y)], fill=(255, 255, 255, 160), width=1)
    draw.line([(cx + 5, stem_top_y), (cx, stem_tip_y)], fill=(255, 255, 255, 160), width=1)

    # Subject property label
    label = "SUBJECT PROPERTY"
    lw = len(label) * 6
    lx, ly = cx - lw // 2, stem_tip_y + 4
    draw.rectangle([lx - 3, ly - 2, lx + lw + 3, ly + 13], fill=(210, 35, 35, 230))
    draw.text((lx, ly), label, fill=(255, 255, 255, 255))


def _draw_coord_label(img: Image.Image, lat: float, lon: float) -> None:
    draw = ImageDraw.Draw(img)
    text = f"{lat:.5f}, {lon:.5f}"
    cx = MAP_WIDTH // 2
    cy = MAP_HEIGHT // 2 + 40   # below the pin tip

    # Estimate text size (default font ~6×11 px per char)
    ch_w, ch_h = 6, 11
    tw = len(text) * ch_w
    pad = 5
    x0, y0 = cx - tw // 2 - pad, cy - pad
    x1, y1 = cx + tw // 2 + pad, cy + ch_h + pad

    draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 165))
    draw.text((cx - tw // 2, cy), text, fill=(255, 255, 255, 255))
