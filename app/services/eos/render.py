"""Render an EOS index (e.g. NDVI) clipped to a parcel polygon.

EOS exposes imagery only as slippy-map XYZ tiles (256x256). To reuse the
existing Dataris satellite overlay (a single ``image_url`` + ``bounds``), we
stitch the tiles covering the parcel bounding box at a suitable zoom level,
crop to the bbox and mask everything outside the parcel polygon to transparent.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from app.core.config import settings
from app.services.eos import client
from app.services.eos.client import EOSApiError

logger = logging.getLogger(__name__)

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



# Caché de proceso para el PNG ya generado (stitched + recortado + con
# máscara). EOS limita el endpoint de render a ~10 solicitudes/minuto, y varias
# pantallas (Satélite, Comparación, Gráficos, Zonificación) suelen pedir
# exactamente la misma escena/índice/lote casi al mismo tiempo. Esta caché
# evita volver a pedir los tiles a EOS para esas repeticiones.
_RENDER_CACHE: dict[tuple, tuple[float, bytes, dict[str, float], dict[str, Any]]] = {}


def _render_cache_key(view_id: str, band: str, bbox: tuple[float, float, float, float]) -> tuple:
    return (view_id, band, tuple(round(v, 6) for v in bbox))


def render_clipped_index_png(
    *,
    view_id: str,
    band: str,
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, float], dict[str, Any]]:
    """Stitch + crop + mask EOS render tiles into a single clipped PNG.

    Returns (png_bytes, bounds, meta) where bounds is {south,north,west,east}.
    Cachea el resultado en memoria de proceso por ``EOS_RENDER_CACHE_TTL_SECONDS``.
    """
    cache_key = _render_cache_key(view_id, band, bbox)
    cached_entry = _RENDER_CACHE.get(cache_key)
    if cached_entry is not None:
        cached_at, png_bytes, bounds, meta = cached_entry
        if time.time() - cached_at < settings.EOS_RENDER_CACHE_TTL_SECONDS:
            return png_bytes, bounds, {**meta, "cache_hit": True}
        _RENDER_CACHE.pop(cache_key, None)

    png_bytes, bounds, meta = _render_clipped_index_png_uncached(view_id=view_id, band=band, geometry=geometry, bbox=bbox)
    # No se cachea un render que salió parcial por saturación (429): guardarlo
    # 6 h dejaría una imagen con huecos hasta que expire. Un render limpio
    # (aunque tenga tiles sin datos legítimos en los bordes) sí se cachea.
    if not meta.get("rate_limited"):
        _RENDER_CACHE[cache_key] = (time.time(), png_bytes, bounds, meta)
    return png_bytes, bounds, meta


def _render_clipped_index_png_uncached(
    *,
    view_id: str,
    band: str,
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, float], dict[str, Any]]:
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
        except EOSApiError as exc:
            # Se registra el motivo real de EOS (cuota/auth/404/etc.) para que
            # quede en los logs; el fallo de un tile no debe abortar el
            # stitching completo si otros tiles sí responden.
            logger.warning("EOS render tile %s/%s/%s/%s falló: %s", view_id, band, tx, ty, exc)
            return coord, None, getattr(exc, "status_code", None)
        return coord, raw, None

    fetched = 0
    rate_limited = False  # algún tile agotó los reintentos por 429 (límite de EOS)
    # Concurrencia moderada: EOS limita el render a ~10 req/min, así que
    # disparar 8 tiles a la vez agotaba el cupo de inmediato ante cualquier
    # otra actividad simultánea (otra pestaña, otro submódulo, otro usuario).
    with ThreadPoolExecutor(max_workers=3) as pool:
        for (tx, ty), raw, err_status in pool.map(_fetch, tiles):
            if err_status == 429:
                rate_limited = True
            if not raw:
                continue
            try:
                tile_img = Image.open(BytesIO(raw)).convert("RGBA")
            except Exception:
                continue
            canvas.paste(tile_img, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE), tile_img)
            fetched += 1

    if fetched == 0:
        # Se propaga el 429 para que build_map_layer NO pruebe otras escenas
        # (comparten el mismo cupo): probar más solo agravaría la saturación.
        if rate_limited:
            raise EOSApiError(
                f"Límite de peticiones de EOS alcanzado al renderizar la escena {view_id}.",
                status_code=429,
            )
        raise EOSApiError(f"No se pudieron obtener tiles de render de EOS para la escena {view_id}.")

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
    meta = {
        "zoom": zoom,
        "tiles": len(tiles),
        "tiles_rendered": fetched,
        "size": list(out.size),
        "rate_limited": rate_limited,
    }
    return buffer.getvalue(), bounds, meta
