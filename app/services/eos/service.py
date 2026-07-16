"""High-level EOS satellite operations for the Dataris backend."""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timedelta
from time import sleep
from typing import Any, Optional

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from app.core.config import settings
from app.services.eos import client
from app.services.eos import render as eos_render
from app.services.eos.client import EOSApiError

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


def _polygon_bbox(polygon: dict[str, Any]) -> tuple[float, float, float, float]:
    return shape(polygon).bounds  # (minx, miny, maxx, maxy)


def _normalize_index(index: Optional[str]) -> str:
    key = (index or DEFAULT_INDEX).strip().upper()
    return key if key in RENDER_BANDS else DEFAULT_INDEX


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
    polygon = geometry_to_polygon(raw_geometry)
    if polygon is None:
        return []
    if not date_from or not date_to:
        default_from, default_to = _default_date_range(None)
        date_from = date_from or default_from
        date_to = date_to or default_to
    results = client.search_scenes(
        geometry=polygon,
        date_from=date_from,
        date_to=date_to,
        cloud_max=float(cloud_max if cloud_max is not None else settings.EOS_MAX_CLOUD),
        limit=limit,
    )
    return [_normalize_scene(r) for r in results if r.get("view_id")]


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

    polygon = geometry_to_polygon(raw_geometry)
    if polygon is None:
        return {"available": False, "reason": "El lote no tiene una geometría válida.", "index": index_key}

    cloud = float(cloud_max if cloud_max is not None else settings.EOS_MAX_CLOUD)
    if target_date is not None:
        # Buscar en una ventana pequeña alrededor de la fecha pedida y elegir la más cercana.
        window_from = (target_date - timedelta(days=6)).isoformat()
        window_to = (target_date + timedelta(days=6)).isoformat()
    else:
        window_from, window_to = _default_date_range(None)

    scenes = client.search_scenes(
        geometry=polygon,
        date_from=window_from,
        date_to=window_to,
        cloud_max=cloud,
        limit=max(scenes_limit, 30),
    )
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

    bbox = _polygon_bbox(polygon)
    normalized_scenes = [_normalize_scene(s) for s in scenes[:scenes_limit] if s.get("view_id")]

    # Un lote grande puede cruzar el borde de dos escenas Sentinel-2 (granules)
    # de la MISMA fecha (p. ej. .../P/XS y .../P/XR): cada granule solo cubre una
    # parte del lote, y renderizar una sola dejaba media parcela sin NDVI (la
    # "línea diagonal"). Se agrupan las escenas por fecha para renderizar juntas
    # todas las de una fecha y cubrir el lote completo.
    scenes_by_date: dict[str, list[dict[str, Any]]] = {}
    date_priority: list[str] = []
    for scene in ordered:
        if not scene.get("view_id"):
            continue
        day = str(scene.get("date") or "")[:10]
        if not day:
            continue
        if day not in scenes_by_date:
            scenes_by_date[day] = []
            date_priority.append(day)
        scenes_by_date[day].append(scene)

    # EOS a veces cataloga una escena como disponible en la búsqueda antes de que
    # sus tiles de render terminen de procesarse (más común en escenas muy
    # recientes). Si la mejor FECHA falla al renderizar, se intenta con la
    # siguiente. El número de intentos se acota (EOS_RENDER_SCENE_FALLBACK_LIMIT)
    # porque cada intento consume tiles del mismo cupo de render de EOS.
    last_error: str | None = None
    attempts = 0
    for day in date_priority:
        if attempts >= settings.EOS_RENDER_SCENE_FALLBACK_LIMIT:
            break
        attempts += 1
        day_scenes = scenes_by_date[day]
        view_ids = [s["view_id"] for s in day_scenes if s.get("view_id")]
        chosen = day_scenes[0]  # representativa de la fecha (para metadatos)
        try:
            png_bytes, bounds, meta = eos_render.render_clipped_index_png(
                view_ids=view_ids,
                band=band,
                geometry=polygon,
                bbox=bbox,
            )
        except EOSApiError as exc:
            last_error = str(exc)
            # Un 429 es un límite global de la API key: probar otra fecha solo
            # consumiría más cupo sin resolver nada. Se corta y se pide reintentar.
            if getattr(exc, "status_code", None) == 429:
                return {
                    "available": False,
                    "reason": (
                        "El proveedor satelital está momentáneamente saturado "
                        "(límite de solicitudes). Vuelve a intentarlo en un minuto."
                    ),
                    "index": index_key,
                    "scenes": normalized_scenes,
                    "debug_last_error": last_error,
                }
            logger.warning("EOS: fecha %s (%s escenas) no se pudo renderizar, probando la siguiente: %s", day, len(view_ids), exc)
            continue

        image_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        notice = _render_notice(meta)
        return {
            "available": True,
            "index": index_key,
            "band": band,
            "date": chosen.get("date"),
            "cloud": chosen.get("cloudCoverage"),
            "scene_id": chosen.get("sceneID"),
            "view_id": view_ids[0],
            "view_ids": view_ids,
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

    return {
        "available": False,
        "reason": (
            "Las escenas satelitales más recientes de este lote todavía no tienen imagen "
            "procesada disponible. Suele resolverse en poco tiempo; intenta de nuevo o elige "
            "una fecha anterior."
        ),
        "index": index_key,
        "scenes": normalized_scenes,
        "debug_last_error": last_error,
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
    polygon = geometry_to_polygon(raw_geometry)
    if polygon is None:
        return {"status": "error", "reason": "El lote no tiene una geometría válida.", "series": []}

    wanted = [i.upper() for i in (indices or ["NDVI"]) if i]
    wanted = [i for i in wanted if i in STATS_INDICES][:3] or ["NDVI"]

    if not date_start or not date_end:
        default_start, default_end = _default_date_range(None)
        date_start = date_start or default_start
        date_end = date_end or default_end

    created = client.create_stats_task(
        geometry=polygon,
        date_start=date_start,
        date_end=date_end,
        indices=wanted,
    )
    task_id = created.get("task_id")
    if not task_id:
        return {"status": "error", "reason": "EOS no devolvió un identificador de tarea.", "series": []}

    for _ in range(settings.EOS_STATS_MAX_POLLS):
        sleep(settings.EOS_STATS_POLL_SECONDS)
        payload = client.get_stats_task(task_id)
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
    payload = client.get_stats_task(task_id)
    has_result = isinstance(payload.get("result"), list)
    if not has_result:
        return {"status": "processing", "task_id": task_id, "series": []}
    return {
        "status": "completed",
        "task_id": task_id,
        "series": _parse_stats_result(payload),
        "errors": payload.get("errors") or [],
    }
