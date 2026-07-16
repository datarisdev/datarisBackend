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


MIN_ZOOM = 7
# Techo absoluto de seguridad. El máximo REAL soportado por el render de EOS
# (Sentinel-2) es 16: pedir z>16 devuelve HTTP 422 "Max zoom exceed". El tope
# efectivo se controla con settings.EOS_RENDER_MAX_ZOOM.
MAX_ZOOM = 16


def _tiles_at_zoom(bbox: tuple[float, float, float, float], zoom: int) -> tuple[int, float]:
    """Return (tile_count, longest_side_px) for ``bbox`` at ``zoom``."""
    minx, miny, maxx, maxy = bbox
    left, top = lonlat_to_world_pixel(minx, maxy, zoom)
    right, bottom = lonlat_to_world_pixel(maxx, miny, zoom)
    width = abs(right - left)
    height = abs(bottom - top)
    tx0, tx1 = int(left // TILE_SIZE), int((right - 1e-9) // TILE_SIZE)
    ty0, ty1 = int(top // TILE_SIZE), int((bottom - 1e-9) // TILE_SIZE)
    n_tiles = (abs(tx1 - tx0) + 1) * (abs(ty1 - ty0) + 1)
    return n_tiles, max(width, height)


def _choose_zoom(
    bbox: tuple[float, float, float, float],
    target_px: int,
    tile_budget: int,
    max_tiles: int,
    max_zoom: int = MAX_ZOOM,
) -> int:
    """Pick the finest zoom that renders the WHOLE parcel within the tile budget.

    EOS throttles the render endpoint to ~10 req/min shared across the app, so a
    large parcel rendered at full zoom needs dozens of tiles: it either takes
    minutes or trips the rate limit and fails with "proveedor saturado". Both
    tile count and canvas pixels grow monotonically with zoom, so the valid zooms
    form a contiguous low range; we return the highest zoom whose tile grid still
    fits ``tile_budget`` and whose canvas stays within the detail cap
    (``target_px``). For a big parcel that means a deliberately coarser zoom: the
    whole field is shown at lower resolution and loads in seconds.

    ``max_zoom`` es el zoom máximo que soporta el render de EOS (16 para
    Sentinel-2): pedir uno mayor devuelve 422 "Max zoom exceed", y como el fallo
    de todos los tiles se acaba enmascarando como "proveedor saturado", NUNCA se
    debe superar. Para lotes pequeños este tope es el que manda (antes se pedía
    z17/z18 y fallaba siempre).
    """
    budget = max(1, min(int(tile_budget), int(max_tiles)))
    ceiling = max(MIN_ZOOM, min(int(max_zoom), MAX_ZOOM))

    chosen = MIN_ZOOM
    for zoom in range(MIN_ZOOM, ceiling + 1):
        n_tiles, longest_px = _tiles_at_zoom(bbox, zoom)
        if n_tiles <= budget and longest_px <= target_px * 1.6:
            chosen = zoom
        else:
            # Higher zooms only need more tiles / more pixels (monotonic): stop.
            break

    return chosen



# Caché de proceso para el PNG ya generado (stitched + recortado + con
# máscara). EOS limita el endpoint de render a ~10 solicitudes/minuto, y varias
# pantallas (Satélite, Comparación, Gráficos, Zonificación) suelen pedir
# exactamente la misma escena/índice/lote casi al mismo tiempo. Esta caché
# evita volver a pedir los tiles a EOS para esas repeticiones.
_RENDER_CACHE: dict[tuple, tuple[float, bytes, dict[str, float], dict[str, Any]]] = {}


def _render_cache_key(view_ids: tuple[str, ...], band: str, bbox: tuple[float, float, float, float]) -> tuple:
    return (view_ids, band, tuple(round(v, 6) for v in bbox))


def render_clipped_index_png(
    *,
    view_ids: list[str],
    band: str,
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, float], dict[str, Any]]:
    """Stitch + crop + mask EOS render tiles into a single clipped PNG.

    ``view_ids`` son las escenas (granules Sentinel-2) de una MISMA fecha que
    intersecan el lote. Un lote grande puede cruzar el borde de dos granules
    (p. ej. ``S2/15/P/XS`` y ``S2/15/P/XR``); cada tile se pide a la primera
    escena que devuelva datos, de modo que la NDVI cubra TODO el lote y no solo
    la mitad que cae en una escena (la "línea diagonal" que dejaba medio lote sin
    capa). La primera escena de la lista es la principal; las demás solo se
    consultan para los tiles que la principal deja vacíos.

    Returns (png_bytes, bounds, meta) where bounds is {south,north,west,east}.
    Cachea el resultado en memoria de proceso por ``EOS_RENDER_CACHE_TTL_SECONDS``.
    """
    view_ids = [v for v in view_ids if v]
    cache_key = _render_cache_key(tuple(view_ids), band, bbox)
    cached_entry = _RENDER_CACHE.get(cache_key)
    if cached_entry is not None:
        cached_at, png_bytes, bounds, meta = cached_entry
        if time.time() - cached_at < settings.EOS_RENDER_CACHE_TTL_SECONDS:
            return png_bytes, bounds, {**meta, "cache_hit": True}
        _RENDER_CACHE.pop(cache_key, None)

    png_bytes, bounds, meta = _render_clipped_index_png_uncached(view_ids=view_ids, band=band, geometry=geometry, bbox=bbox)
    # No se cachea un render que salió parcial por saturación (429): guardarlo
    # 6 h dejaría una imagen con huecos hasta que expire. Un render limpio
    # (aunque tenga tiles sin datos legítimos en los bordes) sí se cachea.
    if not meta.get("rate_limited"):
        _RENDER_CACHE[cache_key] = (time.time(), png_bytes, bounds, meta)
    return png_bytes, bounds, meta


def _render_clipped_index_png_uncached(
    *,
    view_ids: list[str],
    band: str,
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, float], dict[str, Any]]:
    minx, miny, maxx, maxy = bbox
    zoom = _choose_zoom(
        bbox,
        settings.EOS_RENDER_TARGET_PX,
        settings.EOS_RENDER_TILE_BUDGET,
        settings.EOS_RENDER_MAX_TILES,
        settings.EOS_RENDER_MAX_ZOOM,
    )
    # Resolución aproximada en el suelo (m/píxel) en Web-Mercator a la latitud
    # media del lote. Solo cuando supera la resolución nativa de Sentinel-2
    # (~10 m) la vista es realmente un resumen a menor detalle (lote grande);
    # por debajo el detalle es full aunque se limiten los tiles.
    mid_lat = (miny + maxy) / 2.0
    approx_m_per_px = 156543.03392 * math.cos(math.radians(mid_lat)) / (2 ** zoom)
    degraded = approx_m_per_px > settings.EOS_RENDER_NATIVE_M_PER_PX

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
        last_status = None
        # Se prueba cada escena en orden: la primera que devuelva datos para este
        # tile gana. Un lote a caballo entre dos granules necesita tiles de ambas
        # escenas; las escenas secundarias solo se consultan para los tiles que
        # la principal deja vacíos (fuera de su granule), así que un lote normal
        # (una sola escena) no genera peticiones extra.
        for vid in view_ids:
            try:
                raw = client.fetch_render_tile(vid, band, zoom, tx, ty)
            except EOSApiError as exc:
                # Se registra el motivo real de EOS (cuota/auth/404/etc.); el
                # fallo de un tile no debe abortar el stitching completo.
                logger.warning("EOS render tile %s/%s/%s/%s falló: %s", vid, band, tx, ty, exc)
                last_status = getattr(exc, "status_code", None)
                # Un 429 es límite global de la API: no tiene sentido probar más
                # escenas para este tile, comparten el mismo cupo.
                if last_status == 429:
                    return coord, None, 429
                continue
            if raw:
                return coord, raw, None
            # raw None = sin datos en este granule: probar la siguiente escena.
        return coord, None, last_status

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

    scenes_label = ", ".join(view_ids) if view_ids else "(sin escenas)"
    if fetched == 0:
        # Se propaga el 429 para que build_map_layer NO pruebe otras escenas
        # (comparten el mismo cupo): probar más solo agravaría la saturación.
        if rate_limited:
            raise EOSApiError(
                f"Límite de peticiones de EOS alcanzado al renderizar {scenes_label}.",
                status_code=429,
            )
        raise EOSApiError(f"No se pudieron obtener tiles de render de EOS para {scenes_label}.")

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
        "scenes_used": len(view_ids),
        "size": list(out.size),
        "rate_limited": rate_limited,
        # Vista reducida a propósito (lote grande): se muestra el campo completo
        # a menor resolución para caber en el cupo de tiles de EOS.
        "degraded": degraded,
        "approx_m_per_px": round(approx_m_per_px, 1),
        "tile_budget": settings.EOS_RENDER_TILE_BUDGET,
        # Parcial = faltan tiles por el límite de solicitudes (no por bordes sin
        # datos, que son transparentes de forma legítima).
        "partial": bool(rate_limited),
    }
    return buffer.getvalue(), bounds, meta
