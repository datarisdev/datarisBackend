"""High-level EOS satellite operations for the Dataris backend."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from time import sleep
from typing import Any, Optional

from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union

from app.core.config import settings
from app.services.eos import client
from app.services.eos import render as eos_render
from app.services.eos.client import EOSApiError
from app.services.eos.visualization import classify_index_value, legend_for_index

logger = logging.getLogger(__name__)

# Índices con render visual fiable en EOS (aliases documentados).
RENDER_BANDS: dict[str, str] = {
    "NDVI": "NDVI",
    "NDWI": "NDWI",
    "NDSI": "NDSI",
    "RGB": "B04,B03,B02",
}
DEFAULT_INDEX = "NDVI"

# Índices soportados por la API de estadísticas (mt_stats).
STATS_INDICES = ["NDVI", "NDMI", "EVI", "RECI", "NDWI", "SAVI", "MSAVI", "NDRE"]


def status() -> dict[str, Any]:
    return {
        "configured": client.is_configured(),
        "dataset": settings.EOS_DATASET,
        "render_indices": list(RENDER_BANDS.keys()),
        "stats_indices": STATS_INDICES,
    }


def _extract_shapely(raw_geometry: Any):
    if not raw_geometry:
        return None
    if isinstance(raw_geometry, str):
        try:
            raw_geometry = json.loads(raw_geometry)
        except Exception:
            return None
    if not isinstance(raw_geometry, dict):
        return None

    gtype = raw_geometry.get("type")
    try:
        if gtype == "FeatureCollection":
            geoms = [shape(f["geometry"]) for f in raw_geometry.get("features", []) if f.get("geometry")]
            if not geoms:
                return None
            return unary_union(geoms)
        if gtype == "Feature":
            return shape(raw_geometry["geometry"])
        return shape(raw_geometry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EOS: geometría de lote inválida: %s", exc)
        return None


def geometry_to_polygon(raw_geometry: Any) -> Optional[dict[str, Any]]:
    """Reduce any parcel geometry to a single GeoJSON Polygon (lon/lat)."""
    geom = _extract_shapely(raw_geometry)
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.geom_type != "Polygon":
        geom = geom.convex_hull
    if geom.is_empty or geom.geom_type != "Polygon":
        return None
    return mapping(geom)


def geometry_to_render_shape(raw_geometry: Any) -> Optional[dict[str, Any]]:
    """Full parcel geometry as a single GeoJSON Polygon/MultiPolygon (ALL parts).

    La geometría de un lote es un FeatureCollection que puede tener varias partes
    (p. ej. un lote con 99 polígonos). geometry_to_polygon se quedaba solo con el
    polígono MÁS GRANDE, así que el render dejaba el resto del lote sin NDVI
    (cargaba "solo un pedazo"). Esta versión conserva todas las partes; el mask
    del render (_polygon_rings) ya sabe dibujar Polygon y MultiPolygon, y el bbox
    se calcula sobre la extensión completa.
    """
    geom = _extract_shapely(raw_geometry)
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        geom = geom.convex_hull
    if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return mapping(geom)


def geometry_search_hull(raw_geometry: Any) -> Optional[dict[str, Any]]:
    """Single Polygon (convex hull) que cubre TODAS las partes del lote.

    Para la búsqueda de escenas: garantiza encontrar las escenas que intersecan
    cualquier parte de un lote disperso, sin depender de que EOS acepte un
    MultiPolygon en el shape de búsqueda.
    """
    geom = _extract_shapely(raw_geometry)
    if geom is None or geom.is_empty:
        return None
    hull = geom.convex_hull
    if hull.geom_type != "Polygon" or hull.is_empty:
        return None
    return mapping(hull)


def _polygon_bbox(polygon: dict[str, Any]) -> tuple[float, float, float, float]:
    return shape(polygon).bounds  # (minx, miny, maxx, maxy)


def _normalize_index(index: Optional[str]) -> str:
    key = (index or DEFAULT_INDEX).strip().upper()
    return key if key in RENDER_BANDS else DEFAULT_INDEX


def _scene_date_from_view_id(view_id: str) -> Optional[str]:
    parts = str(view_id or "").split("/")
    if len(parts) < 7:
        return None
    try:
        return f"{int(parts[4]):04d}-{int(parts[5]):02d}-{int(parts[6]):02d}"
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=4096)
def _cached_point_value(view_id: str, band: str, lat: float, lon: float) -> Optional[float]:
    # Sentinel-2 has a 10–20 m native pixel. Five decimals keep the lookup
    # spatially precise while deduplicating repeated hover events at one point.
    return client.fetch_point_value(view_id, band, lat, lon)


def point_value(
    raw_geometry: Any,
    *,
    index: str,
    lat: float,
    lon: float,
    view_ids: list[str],
) -> dict[str, Any]:
    """Resolve an exact EOSDA point value for the same scenes used by the map."""
    index_key = str(index or "").strip().upper()
    if index_key not in RENDER_BANDS:
        return {"available": False, "reason": "Índice EOSDA no soportado.", "index": index_key}
    if index_key == "RGB":
        return {"available": False, "reason": "Color natural no es un índice numérico.", "index": index_key}

    geom = _extract_shapely(raw_geometry)
    if geom is None or geom.is_empty or not geom.buffer(1e-9).covers(Point(float(lon), float(lat))):
        return {"available": False, "reason": "El punto está fuera del lote seleccionado.", "index": index_key}

    safe_view_ids: list[str] = []
    for raw_view_id in view_ids[:6]:
        candidate = str(raw_view_id or "").strip("/")
        if candidate.startswith("S2/") and re.fullmatch(r"[A-Za-z0-9_./-]+", candidate):
            safe_view_ids.append(candidate)
    if not safe_view_ids:
        return {"available": False, "reason": "La capa no incluye escenas EOSDA consultables.", "index": index_key}

    band = RENDER_BANDS[index_key]
    lookup_lat = round(float(lat), 5)
    lookup_lon = round(float(lon), 5)
    for view_id in safe_view_ids:
        value = _cached_point_value(view_id, band, lookup_lat, lookup_lon)
        if value is None or not math.isfinite(value):
            continue
        classification = classify_index_value(index_key, value) or {}
        return {
            "available": True,
            "index": index_key,
            "value": value,
            "lat": lookup_lat,
            "lon": lookup_lon,
            "view_id": view_id,
            "date": _scene_date_from_view_id(view_id),
            "provider": "EOSDA Point Value API",
            **classification,
        }

    return {
        "available": False,
        "reason": "EOSDA no devolvió un píxel utilizable en esta coordenada.",
        "index": index_key,
    }


def _default_date_range(target: Optional[date]) -> tuple[str, str]:
    if target is not None:
        return target.isoformat(), target.isoformat()
    today = datetime.utcnow().date()
    start = today - timedelta(days=settings.EOS_SEARCH_DAYS_BACK)
    return start.isoformat(), today.isoformat()


def _normalize_scene(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": raw.get("date"),
        "scene_id": raw.get("sceneID"),
        "view_id": raw.get("view_id"),
        "cloud": raw.get("cloudCoverage"),
        "data_coverage": raw.get("dataCoveragePercentage"),
        "tms": raw.get("tms"),
    }


def list_scenes(
    raw_geometry: Any,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    cloud_max: Optional[float] = None,
    limit: int = 60,
) -> list[dict[str, Any]]:
    # Hull que cubre TODAS las partes del lote: así las fechas listadas incluyen
    # escenas de cualquier zona de un lote multi-parte, no solo la más grande.
    polygon = geometry_search_hull(raw_geometry)
    if polygon is None:
        return []
    if not date_from or not date_to:
        default_from, default_to = _default_date_range(None)
        date_from = date_from or default_from
        date_to = date_to or default_to
    # Un fallo de EOS en la búsqueda no debe dar 502: se devuelve lista vacía.
    try:
        results = client.search_scenes(
            geometry=polygon,
            date_from=date_from,
            date_to=date_to,
            cloud_max=float(cloud_max if cloud_max is not None else settings.EOS_MAX_CLOUD),
            limit=limit,
        )
    except EOSApiError as exc:
        logger.warning("EOS: fallo listando escenas: %s", exc)
        return []
    return [_normalize_scene(r) for r in results if r.get("view_id")]


def prefetch_map_layers(
    parcels: list[tuple[str, Any]],
    *,
    index: Optional[str] = None,
    target_date: Optional[date] = None,
    limit: int = 3,
    delay_seconds: float = 1.0,
) -> int:
    """Calienta la caché (memoria + blob) de la capa para unos pocos lotes.

    Pensado para ejecutarse en segundo plano tras responder al usuario. EOS
    limita el render a ~10 solicitudes/minuto, así que se acota el número de
    lotes y se espacian las peticiones para NO robarle cupo a los clicks reales
    del usuario. Un lote ya cacheado se resuelve casi sin consumir cupo (la
    imagen sale del blob), y una vez calentado queda instantáneo para todos.
    """
    warmed = 0
    for pid, geom in list(parcels)[: max(1, limit)]:
        try:
            res = build_map_layer(geom, index=index, target_date=target_date)
            if res.get("available"):
                warmed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("EOS prefetch: lote %s falló: %s", pid, exc)
        sleep(delay_seconds)
    return warmed


def _render_notice(meta: dict[str, Any]) -> Optional[str]:
    """Mensaje amigable cuando la imagen es un resumen a menor detalle y/o parcial.

    Devuelve ``None`` cuando el render fue completo y a resolución normal, para
    que el frontend no muestre ningún aviso innecesario.
    """
    parts: list[str] = []
    if meta.get("degraded"):
        res = meta.get("approx_m_per_px")
        detalle = f" (~{res:.0f} m/píxel)" if isinstance(res, (int, float)) else ""
        parts.append(
            "Lote grande: se muestra el campo completo a menor resolución"
            f"{detalle} para cargar rápido dentro del límite del proveedor satelital."
        )
    if meta.get("partial"):
        parts.append(
            "Parte de la imagen no se cargó por el límite de solicitudes del "
            "proveedor; vuelve a intentarlo en un minuto para completarla."
        )
    return " ".join(parts) if parts else None


def build_map_layer(
    raw_geometry: Any,
    *,
    index: Optional[str] = None,
    target_date: Optional[date] = None,
    cloud_max: Optional[float] = None,
    scenes_limit: int = 30,
) -> dict[str, Any]:
    index_key = _normalize_index(index)
    band = RENDER_BANDS[index_key]

    # render_shape conserva TODAS las partes del lote (para bbox + máscara);
    # search_shape es un solo polígono (hull) que cubre todas las partes para
    # que la búsqueda encuentre las escenas de cualquier zona del lote.
    render_shape = geometry_to_render_shape(raw_geometry)
    if render_shape is None:
        return {"available": False, "reason": "El lote no tiene una geometría válida.", "index": index_key}
    search_shape = geometry_search_hull(raw_geometry) or render_shape

    cloud = float(cloud_max if cloud_max is not None else settings.EOS_MAX_CLOUD)
    if target_date is not None:
        # Buscar en una ventana pequeña alrededor de la fecha pedida y elegir la más cercana.
        window_from = (target_date - timedelta(days=6)).isoformat()
        window_to = (target_date + timedelta(days=6)).isoformat()
    else:
        window_from, window_to = _default_date_range(None)

    # Un fallo de EOS en la búsqueda no debe dar 502: se degrada a "no disponible".
    try:
        scenes = client.search_scenes(
            geometry=search_shape,
            date_from=window_from,
            date_to=window_to,
            cloud_max=cloud,
            limit=max(scenes_limit, 30),
        )
    except EOSApiError as exc:
        logger.warning("EOS: fallo buscando escenas para el mapa: %s", exc)
        return {
            "available": False,
            "reason": "El proveedor satelital no respondió a la búsqueda de imágenes. Intenta de nuevo en un minuto.",
            "index": index_key,
        }
    if not scenes:
        return {
            "available": False,
            "reason": "No hay imágenes satelitales disponibles para el rango solicitado.",
            "index": index_key,
        }

    if target_date is not None:
        def _distance(scene: dict[str, Any]) -> int:
            try:
                sd = datetime.fromisoformat(str(scene.get("date"))[:10]).date()
                return abs((sd - target_date).days)
            except Exception:
                return 10_000
        ordered = sorted(scenes, key=_distance)
    else:
        ordered = scenes  # ya vienen ordenadas por fecha desc (más reciente primero)

    bbox = _polygon_bbox(render_shape)
    normalized_scenes = [_normalize_scene(s) for s in scenes[:scenes_limit] if s.get("view_id")]

    # Mosaico multi-escena / multi-fecha para cubrir el lote COMPLETO:
    # un lote puede cruzar el borde de dos granules Sentinel-2 y/o su fecha más
    # reciente tener datos válidos solo en una parte (la otra parte quedaba vacía
    # = "un pedazo"). Se pasa al render una lista de escenas ordenada de mejor a
    # peor (fecha más cercana/reciente); cada tile toma la PRIMERA escena que
    # tenga datos, así se rellena todo el lote. La mejor escena domina; las demás
    # solo rellenan huecos (no cuestan peticiones en tiles ya cubiertos). Se
    # acota el número de escenas para no disparar el cupo de EOS en el 1er render.
    view_ids: list[str] = []
    seen_ids: set[str] = set()
    for scene in ordered:
        vid = scene.get("view_id")
        if not vid or vid in seen_ids:
            continue
        seen_ids.add(vid)
        view_ids.append(vid)
        if len(view_ids) >= settings.EOS_MOSAIC_MAX_SCENES:
            break

    if not view_ids:
        return {
            "available": False,
            "reason": "No hay imágenes satelitales disponibles para el rango solicitado.",
            "index": index_key,
            "scenes": normalized_scenes,
        }

    primary = ordered[0]  # escena principal (mejor fecha) para los metadatos
    try:
        png_bytes, bounds, meta = eos_render.render_clipped_index_png(
            view_ids=view_ids,
            band=band,
            geometry=render_shape,
            bbox=bbox,
        )
    except EOSApiError as exc:
        # Un 429 es un límite global de la API key: reintentar más solo consumiría
        # más cupo. Se corta y se pide reintentar en un minuto.
        if getattr(exc, "status_code", None) == 429:
            return {
                "available": False,
                "reason": (
                    "El proveedor satelital está momentáneamente saturado "
                    "(límite de solicitudes). Vuelve a intentarlo en un minuto."
                ),
                "index": index_key,
                "scenes": normalized_scenes,
                "debug_last_error": str(exc),
            }
        # Ninguna escena tenía tiles/datos disponibles (p. ej. aún sin procesar).
        logger.warning("EOS: no se pudo renderizar el mosaico (%s escenas): %s", len(view_ids), exc)
        return {
            "available": False,
            "reason": (
                "Las escenas satelitales más recientes de este lote todavía no tienen imagen "
                "procesada disponible. Suele resolverse en poco tiempo; intenta de nuevo o elige "
                "una fecha anterior."
            ),
            "index": index_key,
            "scenes": normalized_scenes,
            "debug_last_error": str(exc),
        }

    image_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    notice = _render_notice(meta)
    return {
        "available": True,
        "index": index_key,
        "band": band,
        "date": primary.get("date"),
        "cloud": primary.get("cloudCoverage"),
        "scene_id": primary.get("sceneID"),
        "view_id": view_ids[0],
        "view_ids": view_ids,
        "legend": legend_for_index(index_key),
        "image_url": image_url,
        "bounds": bounds,
        "render": meta,
        "scenes": normalized_scenes,
        # Vista reducida (lote grande) y/o parcial (faltaron tiles por el
        # límite de EOS). El frontend muestra este aviso sin bloquear la imagen.
        "coarse": bool(meta.get("degraded")),
        "partial": bool(meta.get("partial")),
        "resolution_m": meta.get("approx_m_per_px"),
        "notice": notice,
    }


def _parse_stats_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for entry in payload.get("result", []) or []:
        indexes = entry.get("indexes") or {}
        values = {
            name: {
                "average": stats.get("average"),
                "min": stats.get("min"),
                "max": stats.get("max"),
                "median": stats.get("median"),
                "std": stats.get("std"),
                # Cuartiles reales: permiten construir p25/p75 (gráficos) y una
                # distribución de zonas exacta (25% bajo / 50% medio / 25% alto)
                # sin necesitar acceso al ráster crudo.
                "q1": stats.get("q1"),
                "q3": stats.get("q3"),
                "p10": stats.get("p10"),
                "p90": stats.get("p90"),
                "variance": stats.get("variance"),
            }
            for name, stats in indexes.items()
            if isinstance(stats, dict)
        }
        series.append({
            "date": entry.get("date"),
            "scene_id": entry.get("scene_id"),
            "cloud": entry.get("cloud"),
            "values": values,
        })
    series.sort(key=lambda item: str(item.get("date") or ""))
    return series


def statistics_series(
    raw_geometry: Any,
    *,
    indices: Optional[list[str]] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
) -> dict[str, Any]:
    # Estadísticas sobre TODAS las partes del lote (no solo el polígono mayor).
    polygon = geometry_to_render_shape(raw_geometry)
    if polygon is None:
        return {"status": "error", "reason": "El lote no tiene una geometría válida.", "series": []}

    wanted = [i.upper() for i in (indices or ["NDVI"]) if i]
    wanted = [i for i in wanted if i in STATS_INDICES][:3] or ["NDVI"]

    if not date_start or not date_end:
        default_start, default_end = _default_date_range(None)
        date_start = date_start or default_start
        date_end = date_end or default_end

    # Un error de EOS (p. ej. un 4xx/5xx transitorio) NO debe convertirse en un
    # 502 de nuestra API (dispara alertas por correo): las estadísticas se
    # degradan a un estado limpio y el frontend puede reintentar.
    try:
        created = client.create_stats_task(
            geometry=polygon,
            date_start=date_start,
            date_end=date_end,
            indices=wanted,
        )
    except EOSApiError as exc:
        logger.warning("EOS stats: no se pudo crear la tarea: %s", exc)
        return {"status": "error", "reason": "No se pudieron calcular las estadísticas satelitales en este momento. Intenta de nuevo en un minuto.", "series": [], "indices": wanted}

    task_id = created.get("task_id")
    if not task_id:
        return {"status": "error", "reason": "EOS no devolvió un identificador de tarea.", "series": []}

    for _ in range(settings.EOS_STATS_MAX_POLLS):
        sleep(settings.EOS_STATS_POLL_SECONDS)
        try:
            payload = client.get_stats_task(task_id)
        except EOSApiError as exc:
            # Hipo transitorio de EOS durante el procesamiento: se reintenta el
            # siguiente ciclo en vez de fallar la petición completa.
            logger.warning("EOS stats: fallo consultando la tarea %s (se reintenta): %s", task_id, exc)
            continue
        has_result = isinstance(payload.get("result"), list)
        status_value = str(payload.get("status") or "").lower()
        if has_result or status_value in {"finished", "completed", "done"}:
            return {
                "status": "completed",
                "task_id": task_id,
                "indices": wanted,
                "series": _parse_stats_result(payload),
                "errors": payload.get("errors") or [],
            }

    # Still processing after the polling budget — let the caller retry.
    return {"status": "processing", "task_id": task_id, "indices": wanted, "series": []}


def statistics_by_task(task_id: str) -> dict[str, Any]:
    try:
        payload = client.get_stats_task(task_id)
    except EOSApiError as exc:
        logger.warning("EOS stats: fallo consultando la tarea %s: %s", task_id, exc)
        return {"status": "processing", "task_id": task_id, "series": []}
    has_result = isinstance(payload.get("result"), list)
    if not has_result:
        return {"status": "processing", "task_id": task_id, "series": []}
    return {
        "status": "completed",
        "task_id": task_id,
        "series": _parse_stats_result(payload),
        "errors": payload.get("errors") or [],
    }
