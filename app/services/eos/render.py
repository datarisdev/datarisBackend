"""Render an EOS index (e.g. NDVI) clipped to a parcel polygon.

EOS exposes imagery only as slippy-map XYZ tiles (256x256). To reuse the
existing Dataris satellite overlay (a single ``image_url`` + ``bounds``), we
stitch the tiles covering the parcel bounding box at a suitable zoom level,
crop to the bbox and mask everything outside the parcel polygon to transparent.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from app.core.config import settings
from app.services.eos import client
from app.services.eos.client import EOSApiError

TILE_SIZE = 256


def lonlat_to_world_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Convert lon/lat to global Web-Mercator pixel coordinates at ``zoom``."""
    n = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * n
    return x, y


def _polygon_rings(geometry: dict[str, Any]) -> list[tuple[list, list]]:
    """Return [(exterior, [holes...])] rings for a GeoJSON Polygon/MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [(coords[0], list(coords[1:]))] if coords else []
    if gtype == "MultiPolygon":
        rings: list[tuple[list, list]] = []
        for poly in coords:
            if poly:
                rings.append((poly[0], list(poly[1:])))
        return rings
    return []


def _choose_zoom(bbox: tuple[float, float, float, float], target_px: int, max_tiles: int) -> int:
    minx, miny, maxx, maxy = bbox
    for zoom in range(18, 6, -1):
        left, top = lonlat_to_world_pixel(minx, maxy, zoom)
        right, bottom = lonlat_to_world_pixel(maxx, miny, zoom)
        width = abs(right - left)
        height = abs(bottom - top)
        if max(width, height) > target_px * 1.6:
            continue
        tx0, tx1 = int(left // TILE_SIZE), int((right - 1e-9) // TILE_SIZE)
        ty0, ty1 = int(top // TILE_SIZE), int((bottom - 1e-9) // TILE_SIZE)
        n_tiles = (abs(tx1 - tx0) + 1) * (abs(ty1 - ty0) + 1)
        if n_tiles <= max_tiles:
            return zoom
    return 12


def render_clipped_index_png(
    *,
    view_id: str,
    band: str,
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, float], dict[str, Any]]:
    """Stitch + crop + mask EOS render tiles into a single clipped PNG.

    Returns (png_bytes, bounds, meta) where bounds is {south,north,west,east}.
    """
    minx, miny, maxx, maxy = bbox
    zoom = _choose_zoom(bbox, settings.EOS_RENDER_TARGET_PX, settings.EOS_RENDER_MAX_TILES)

    left, top = lonlat_to_world_pixel(minx, maxy, zoom)      # west/north corner
    right, bottom = lonlat_to_world_pixel(maxx, miny, zoom)  # east/south corner

    tx0, tx1 = int(left // TILE_SIZE), int((right - 1e-9) // TILE_SIZE)
    ty0, ty1 = int(top // TILE_SIZE), int((bottom - 1e-9) // TILE_SIZE)
    origin_x, origin_y = tx0 * TILE_SIZE, ty0 * TILE_SIZE

    canvas_w = (tx1 - tx0 + 1) * TILE_SIZE
    canvas_h = (ty1 - ty0 + 1) * TILE_SIZE
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    tiles = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]

    def _fetch(coord: tuple[int, int]):
        tx, ty = coord
        try:
            raw = client.fetch_render_tile(view_id, band, zoom, tx, ty)
        except EOSApiError:
            return coord, None
        return coord, raw

    fetched = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (tx, ty), raw in pool.map(_fetch, tiles):
            if not raw:
                continue
            try:
                tile_img = Image.open(BytesIO(raw)).convert("RGBA")
            except Exception:
                continue
            canvas.paste(tile_img, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE), tile_img)
            fetched += 1

    if fetched == 0:
        raise EOSApiError("No se pudieron obtener tiles de render de EOS para la escena seleccionada.")

    # Crop to the bbox pixel window (canvas coordinates = world_pixel - origin).
    crop_box = (
        max(0, int(math.floor(left - origin_x))),
        max(0, int(math.floor(top - origin_y))),
        min(canvas_w, int(math.ceil(right - origin_x))),
        min(canvas_h, int(math.ceil(bottom - origin_y))),
    )
    cropped = canvas.crop(crop_box)

    # Build the parcel mask in the cropped image coordinate space.
    mask = Image.new("L", cropped.size, 0)
    draw = ImageDraw.Draw(mask)

    def _to_local(lon: float, lat: float) -> tuple[float, float]:
        wx, wy = lonlat_to_world_pixel(lon, lat, zoom)
        return wx - origin_x - crop_box[0], wy - origin_y - crop_box[1]

    for exterior, holes in _polygon_rings(geometry):
        ext_pts = [_to_local(lon, lat) for lon, lat in exterior]
        if len(ext_pts) >= 3:
            draw.polygon(ext_pts, fill=255)
        for hole in holes:
            hole_pts = [_to_local(lon, lat) for lon, lat in hole]
            if len(hole_pts) >= 3:
                draw.polygon(hole_pts, fill=0)

    r, g, b, a = cropped.split()
    masked_alpha = ImageChops.multiply(a, mask)
    out = Image.merge("RGBA", (r, g, b, masked_alpha))

    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True)

    bounds = {"south": miny, "north": maxy, "west": minx, "east": maxx}
    meta = {"zoom": zoom, "tiles": len(tiles), "tiles_rendered": fetched, "size": list(out.size)}
    return buffer.getvalue(), bounds, meta
