from __future__ import annotations

import os
import json
import re
import base64
import logging
import time as time_module
import uuid
import asyncio
import threading
import hashlib
import tempfile
import httpx
from pathlib import Path
from io import BytesIO
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Body, Header, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool
from shapely.geometry import box as shapely_box, mapping, shape as shapely_shape
from shapely.ops import transform, unary_union
from shapely import wkt as shapely_wkt
try:
    from shapely.validation import make_valid as shapely_make_valid
except Exception:  # pragma: no cover - fallback for older Shapely builds
    shapely_make_valid = None
from PIL import Image, ImageDraw

from app.api.routers.compat import (
    LOCK,
    bearer_user,
    now,
    parcel_manager_covers_user,
    parcel_manager_permission,
    read_db,
    require_admin_context,
    table,
    write_db,
)
from app.core.config import settings
from app.services.graniot_client import GraniotAPIError, GraniotClient, GraniotNotConfigured
from app.services.graniot_debug import clear_logs, get_log_file_path, log_event, read_logs, safe_payload
from app.services.graniot_embed_accounts import (
    account_is_manager,
    alias_already_taken,
    create_embed_account,
    embed_alias,
    fetch_company_farms,
    index_platform_users,
    link_farm_to_account,
    platform_user_id_from_alias,
    unlink_farm_from_account,
)

router = APIRouter(prefix="/graniot", tags=["Graniot"])

_wms_cloud_logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Graniot performance cache
# ---------------------------------------------------------------------------
# Cloud Run instances are ephemeral, but keeping a small /tmp cache still helps a
# lot because the same satellite layer/date is usually requested many times by
# the same customer during a work session. The cache is intentionally local to
# the running instance: no Redis/Memorystore is required, so the MVP remains
# cheap.
GRANIOT_CATALOG_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_CATALOG_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 30)))
GRANIOT_DATE_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_DATE_CACHE_TTL_SECONDS", str(60 * 60 * 12)))
GRANIOT_STATS_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_STATS_CACHE_TTL_SECONDS", str(60 * 30)))
GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS", str(60 * 30)))
GRANIOT_WMS_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_WMS_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 7)))
GRANIOT_WMS_CACHE_MAX_MB = int(os.getenv("GRANIOT_WMS_CACHE_MAX_MB", "256"))
GRANIOT_WMS_PREFETCH_CONCURRENCY = int(os.getenv("GRANIOT_WMS_PREFETCH_CONCURRENCY", "3"))
# /api/accounts/ cambia poco (altas/bajas de cuentas Graniot). Una caché corta
# evita golpear a Graniot en cada carga del módulo Satélite; la expiración del
# auth_id de cada cuenta se valida por petición, así que cachear es seguro.
GRANIOT_EMBED_ACCOUNTS_CACHE_TTL_SECONDS = int(os.getenv("GRANIOT_EMBED_ACCOUNTS_CACHE_TTL_SECONDS", str(60 * 5)))
# Censo de dueños de finca (/api/company/farms/): una sola petición, pero pesada
# (cientos de fincas). Cambia solo cuando Graniot da de alta fincas o gestores.
GRANIOT_COMPANY_FARMS_CACHE_TTL_SECONDS = int(
    os.getenv("GRANIOT_COMPANY_FARMS_CACHE_TTL_SECONDS", str(settings.GRANIOT_COMPANY_FARMS_CACHE_TTL_SECONDS))
)
GRANIOT_WMS_BACKEND_MASK_ENABLED = str(os.getenv("GRANIOT_WMS_BACKEND_MASK_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}

_RUNTIME_CACHE: Dict[str, Tuple[float, Any]] = {}
_WMS_CACHE_DIR = Path(os.getenv("GRANIOT_WMS_CACHE_DIR", str(Path(tempfile.gettempdir()) / "dataris_graniot_wms_cache")))


def _cache_get(key: str) -> Optional[Any]:
    item = _RUNTIME_CACHE.get(key)
    if not item:
        return None
    expires_at, value = item
    if expires_at <= time_module.time():
        _RUNTIME_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: int) -> Any:
    if ttl_seconds <= 0:
        return value
    # Keep the in-memory cache bounded. This is not an LRU, but it prevents an
    # accidentally large satellite session from growing without limits.
    if len(_RUNTIME_CACHE) > 750:
        now_ts = time_module.time()
        for stale_key, (expires_at, _) in list(_RUNTIME_CACHE.items()):
            if expires_at <= now_ts:
                _RUNTIME_CACHE.pop(stale_key, None)
        if len(_RUNTIME_CACHE) > 750:
            for stale_key in list(_RUNTIME_CACHE.keys())[:150]:
                _RUNTIME_CACHE.pop(stale_key, None)
    _RUNTIME_CACHE[key] = (time_module.time() + ttl_seconds, value)
    return value


def _cache_delete_prefix(prefix: str) -> None:
    for key in list(_RUNTIME_CACHE.keys()):
        if key.startswith(prefix):
            _RUNTIME_CACHE.pop(key, None)


def _stable_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _wms_cache_public_key(
    *,
    cache_key: Optional[str],
    parcel_id: Optional[str],
    access_key: Optional[str],
    layer: str,
    time: Optional[str],
    width: int,
    height: int,
    bbox: Optional[str],
    south: Optional[float],
    west: Optional[float],
    north: Optional[float],
    east: Optional[float],
) -> str:
    # Prefer a caller-provided stable key that does not include expiring signed
    # Graniot tokens. If it is not available, include a hash of the access key as
    # a fallback so unrelated parcels cannot collide.
    stable = str(cache_key or "").strip()
    fallback_key = hashlib.sha256(str(access_key or "").encode("utf-8")).hexdigest()[:16] if access_key else "no-key"
    payload = {
        "version": 6,
        "stable": stable or None,
        "parcel_id": parcel_id or None,
        "access_hash": None if stable else fallback_key,
        "layer": layer,
        "time": time or "latest",
        "width": width,
        "height": height,
        "bbox": bbox or None,
        "south": None if south is None else round(float(south), 7),
        "west": None if west is None else round(float(west), 7),
        "north": None if north is None else round(float(north), 7),
        "east": None if east is None else round(float(east), 7),
    }
    return _stable_hash(payload)


def _wms_cache_paths(key: str) -> Tuple[Path, Path]:
    safe_key = re.sub(r"[^a-f0-9]", "", key.lower())[:64]
    return _WMS_CACHE_DIR / f"{safe_key}.bin", _WMS_CACHE_DIR / f"{safe_key}.json"


def _read_wms_disk_cache(key: str) -> Optional[Response]:
    if GRANIOT_WMS_CACHE_TTL_SECONDS <= 0:
        return None
    try:
        data_path, meta_path = _wms_cache_paths(key)
        if not data_path.exists() or not meta_path.exists():
            return None
        age = time_module.time() - data_path.stat().st_mtime
        if age > GRANIOT_WMS_CACHE_TTL_SECONDS:
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        content = data_path.read_bytes()
        media_type = str(meta.get("media_type") or "image/png")
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
                "X-Dataris-WMS-Cache": "HIT",
            },
        )
    except Exception as exc:
        log_event({"event": "dataris.graniot.wms_cache.read_failed", "key": key, "message": str(exc)})
        return None


def _write_wms_disk_cache(key: str, content: bytes, media_type: str) -> None:
    if GRANIOT_WMS_CACHE_TTL_SECONDS <= 0 or not content:
        return
    try:
        _WMS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data_path, meta_path = _wms_cache_paths(key)
        tmp_path = data_path.with_suffix(".tmp")
        tmp_path.write_bytes(content)
        tmp_path.replace(data_path)
        meta_path.write_text(json.dumps({"media_type": media_type, "saved_at": time_module.time()}), encoding="utf-8")
        _prune_wms_disk_cache()
    except Exception as exc:
        log_event({"event": "dataris.graniot.wms_cache.write_failed", "key": key, "message": str(exc)})


def _looks_like_bad_solid_index_raster(content: bytes, media_type: str) -> Tuple[bool, Dict[str, Any]]:
    """Detect Graniot WMS responses that are technically PNGs but visually wrong.

    Some Graniot attempts can return a nearly single-color red raster. Because it
    is still a valid image/png, the proxy used to accept it as successful and the
    UI showed a solid red polygon. This heuristic is intentionally conservative:
    it only skips rasters that are both very flat and clearly red-dominant.
    """
    if not content or "image" not in str(media_type or "").lower():
        return False, {"reason": "not_image"}
    try:
        image = Image.open(BytesIO(content)).convert("RGBA")
        if max(image.size) > 128:
            try:
                resampling = Image.Resampling.BILINEAR
            except AttributeError:
                resampling = Image.BILINEAR
            image.thumbnail((128, 128), resampling)

        pixels = [px for px in image.getdata() if px[3] > 16]
        total = len(pixels)
        if total < 200:
            return False, {"reason": "too_few_visible_pixels", "visible_pixels": total}

        counts: Dict[Tuple[int, int, int], int] = {}
        r_sum = g_sum = b_sum = 0
        for r, g, b, _a in pixels:
            # Quantize slightly so JPEG/antialiasing noise does not hide a flat raster.
            key = (int(r) // 8 * 8, int(g) // 8 * 8, int(b) // 8 * 8)
            counts[key] = counts.get(key, 0) + 1
            r_sum += int(r)
            g_sum += int(g)
            b_sum += int(b)

        dominant = max(counts.values()) if counts else 0
        dominant_ratio = dominant / max(total, 1)
        unique_colors = len(counts)
        r_avg = r_sum / total
        g_avg = g_sum / total
        b_avg = b_sum / total

        red_dominant = r_avg > 140 and r_avg > g_avg + 35 and r_avg > b_avg + 35
        visually_flat = dominant_ratio >= 0.88 or unique_colors <= 8
        is_bad = bool(red_dominant and visually_flat)
        return is_bad, {
            "visible_pixels": total,
            "unique_colors": unique_colors,
            "dominant_ratio": round(dominant_ratio, 4),
            "avg_rgb": [round(r_avg, 2), round(g_avg, 2), round(b_avg, 2)],
            "red_dominant": red_dominant,
            "visually_flat": visually_flat,
        }
    except Exception as exc:
        return False, {"reason": "analysis_failed", "message": str(exc)}


def _prune_wms_disk_cache() -> None:
    try:
        max_bytes = max(16, GRANIOT_WMS_CACHE_MAX_MB) * 1024 * 1024
        files = [path for path in _WMS_CACHE_DIR.glob("*.bin") if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        if total <= max_bytes:
            return
        files.sort(key=lambda path: path.stat().st_mtime)
        for path in files:
            if total <= max_bytes:
                break
            size = path.stat().st_size
            meta = path.with_suffix(".json")
            path.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            total -= size
    except Exception as exc:
        log_event({"event": "dataris.graniot.wms_cache.prune_failed", "message": str(exc)})


def _response_with_cache_headers(response: Response, cache_value: str = "MISS") -> Response:
    try:
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        response.headers["X-Dataris-WMS-Cache"] = cache_value
    except Exception:
        pass
    return response


def _wms_json_safe(value: Any) -> Any:
    """Return a JSON/log friendly payload without risking diagnostic crashes."""
    try:
        return safe_payload(value)
    except Exception:
        try:
            if isinstance(value, dict):
                return {str(k): _wms_json_safe(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_wms_json_safe(v) for v in value]
            return str(value)
        except Exception:
            return "<unserializable>"


def _wms_token_info(value: Any) -> Dict[str, Any]:
    """Log token shape only. Never write the full Graniot access key to Cloud Run."""
    try:
        text = str(value or "").strip()
        return {
            "present": bool(text),
            "length": len(text),
            "signed_like": bool(text and ":" in text),
            "uuid_like": bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)),
            "prefix": text[:10] if text else "",
            "suffix": text[-6:] if len(text) > 16 else "",
        }
    except Exception:
        return {"present": bool(value), "diagnostic_error": True}


def _wms_cloud_log(level: int, event: str, **fields: Any) -> None:
    """Write compact WMS diagnostics to Cloud Run stdout/stderr.

    This function must never raise. The previous diagnostic version could fail
    before reaching Graniot, producing a fast 500 instead of the useful cause.
    """
    try:
        payload = _wms_json_safe(fields)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        _wms_cloud_logger.log(level, "WMS_PROXY %s %s", event, line)
    except Exception:
        try:
            _wms_cloud_logger.log(level, "WMS_PROXY %s <diagnostic-log-failed>", event)
        except Exception:
            pass


def _wms_cloud_exception(event: str, exc: Exception, **fields: Any) -> None:
    """Write a traceback to Cloud Run without allowing logging itself to fail."""
    try:
        payload = _wms_json_safe({
            **fields,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        })
        line = json.dumps(payload, ensure_ascii=False, default=str)
        _wms_cloud_logger.exception("WMS_PROXY %s %s", event, line)
    except Exception:
        try:
            _wms_cloud_logger.exception("WMS_PROXY %s <diagnostic-exception-log-failed>", event)
        except Exception:
            pass


def _wms_http_detail_with_request_id(detail: Any, request_id: str) -> Any:
    try:
        if isinstance(detail, dict):
            enriched = dict(_wms_json_safe(detail))
            enriched["request_id"] = request_id
            return enriched
        return {"message": str(detail), "request_id": request_id}
    except Exception:
        return {"message": "WMS proxy failed", "request_id": request_id}


def _require_user(authorization: Optional[str]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _acting_user(authorization: Optional[str], on_behalf_of: Optional[str] = None) -> Dict[str, Any]:
    """Usuario cuyos lotes se manipulan.

    Normalmente es el autenticado. El equipo de Dataris (permiso
    `can_manage_parcels`) administra los lotes de sus clientes desde el panel de
    administración, así que puede indicar el dueño con `user_id`.
    """
    user = _require_user(authorization)
    target_id = str(on_behalf_of or "").strip()
    if not target_id or target_id == str(user.get("id") or ""):
        return user

    db = read_db()
    permission = parcel_manager_permission(db, str(user.get("id") or ""))
    if not parcel_manager_covers_user(db, permission, target_id):
        raise HTTPException(status_code=403, detail="No tienes permiso para administrar los lotes de ese usuario")
    owner = next((u for u in db.get("users", []) if str(u.get("id")) == target_id), None)
    if not owner:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return owner


def _raise_graniot_error(exc: Exception) -> None:
    if isinstance(exc, GraniotNotConfigured):
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GraniotAPIError):
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "payload": exc.payload})
    raise HTTPException(status_code=500, detail=f"Error conectando con Graniot: {exc}")


def _items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # Graniot /api/parcels/ returns a GeoJSON FeatureCollection. The parcel
        # id, bbox, properties.wms_url and properties.image_url live in features[].
        if payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
            return [x for x in payload.get("features") or [] if isinstance(x, dict)]
        for key in ("results", "data", "items", "layers", "features"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [payload]
    return []


def _public_embed_account(account: Optional[Dict[str, Any]], fallback_email: str = "") -> Dict[str, str]:
    """Validate one Graniot account and keep only what the browser may see.

    ``account_access`` and the backend API key never leave the backend.
    """
    embedded_url = str((account or {}).get("embedded_url") or "").strip()
    parsed = urlparse(embedded_url)
    auth_id = parse_qs(parsed.query).get("auth_id", [])
    expected_host = (settings.GRANIOT_EMBED_HOST or "embed.graniot.com").strip().lower()
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != expected_host or not auth_id or not auth_id[0]:
        raise HTTPException(status_code=502, detail="Graniot devolvió un enlace embebido inválido")

    return {
        "account_email": str((account or {}).get("account_email") or fallback_email),
        "embedded_url": embedded_url,
    }


def _select_embed_account(payload: Any, target_email: str) -> Dict[str, str]:
    """Select and validate the dedicated Graniot embed account by its email."""
    normalized_target = str(target_email or "").strip().lower()
    account = next(
        (
            item
            for item in _items(payload)
            if str(item.get("account_email") or "").strip().lower() == normalized_target
        ),
        None,
    )
    if not account:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró la cuenta Graniot configurada ({target_email})",
        )

    return _public_embed_account(account, normalized_target)


def _looks_like_layer(item: Dict[str, Any]) -> bool:
    return bool(
        isinstance(item, dict)
        and (item.get("key") or item.get("layer_key") or item.get("name") or item.get("id"))
        and (
            item.get("displayed_name") is not None
            or item.get("display_name") is not None
            or item.get("layer_resolution") is not None
            or item.get("resolution") is not None
            or item.get("legend") is not None
            or item.get("color") is not None
            or item.get("name") is not None
        )
    )


def _layer_items(payload: Any) -> List[Dict[str, Any]]:
    """Flatten the layer response returned by /layers/* endpoints.

    Graniot's documented schemas say `Layer`, but different endpoints may return
    arrays, paginated objects, or dictionaries grouped by resolution. This helper
    extracts only objects that look like real layers and deduplicates them by key.
    """
    found: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return

        if _looks_like_layer(value):
            found.append(value)

        for key in ("results", "data", "items", "layers", "wms_layers", "available_layers"):
            child = value.get(key)
            if child is not None:
                walk(child)

        # Some responses are grouped like {"Sentinel": [{...}], "Landsat": [{...}]}
        for child in value.values():
            if isinstance(child, (list, dict)):
                walk(child)

    walk(payload)

    deduped: Dict[str, Dict[str, Any]] = {}
    for item in found:
        key = str(item.get("key") or item.get("layer_key") or item.get("name") or item.get("id"))
        deduped[key] = item
    return list(deduped.values())


def _resolution_key_from_layer(layer: Dict[str, Any]) -> Optional[str]:
    resolution_obj = layer.get("layer_resolution")
    if isinstance(resolution_obj, dict):
        value = resolution_obj.get("key") or resolution_obj.get("id") or resolution_obj.get("resolution")
        return str(value) if value not in (None, "") else None
    value = layer.get("resolution_key") or layer.get("layer_resolution_key")
    if value not in (None, ""):
        return str(value)
    return None


def _resolution_label_from_layer(layer: Dict[str, Any]) -> Optional[str]:
    resolution_obj = layer.get("layer_resolution")
    if isinstance(resolution_obj, dict):
        value = resolution_obj.get("resolution") or resolution_obj.get("label") or resolution_obj.get("name")
        return str(value) if value not in (None, "") else None
    value = layer.get("resolution_name") or layer.get("resolution_label")
    if value not in (None, ""):
        return str(value)
    return None


def _wms_layer_name_for(layer: Dict[str, Any], wms_names: Optional[set[str]] = None) -> str:
    # Confirmed Graniot behavior:
    # - /parcels/{id}/layers/{layer_key}/statistics and json-index require layer.key UUID.
    # - /api/wms/?layers=... requires the WMS layer name, e.g. NDVI, NDVI_PLANET.
    candidates = [
        layer.get("wms_layer"),
        layer.get("wms_name"),
        layer.get("layer"),
        layer.get("layers"),
        layer.get("name"),
    ]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        text = str(candidate).strip()
        if not text:
            continue
        if not wms_names or text in wms_names or text.upper() in {name.upper() for name in wms_names}:
            return text
    fallback = layer.get("name") or layer.get("key") or layer.get("id") or ""
    return str(fallback).strip()


def _normalize_layer(layer: Dict[str, Any], wms_names: Optional[set[str]] = None) -> Dict[str, Any]:
    displayed = layer.get("displayed_name") or layer.get("display_name") or layer.get("label") or layer.get("name") or "Capa"
    name = str(layer.get("name") or displayed or "").strip()
    # Keep UUID `key` for Graniot statistics/json-index. If no UUID exists
    # (get_wms_layers returns only names), use the WMS name as a safe fallback.
    key = str(layer.get("key") or layer.get("layer_key") or name or layer.get("id") or "").strip()
    resolution_key = _resolution_key_from_layer(layer)
    resolution_label = _resolution_label_from_layer(layer)
    wms_layer = _wms_layer_name_for(layer, wms_names)
    return {
        "id": layer.get("id"),
        "key": key,
        "name": name,
        "displayed_name": displayed,
        "label": displayed,
        "wms_layer": wms_layer,
        "color": layer.get("color"),
        "legend": layer.get("legend"),
        "config": layer.get("config"),
        "layer_stats": layer.get("layer_stats"),
        "layer_resolution": resolution_key or layer.get("layer_resolution") or layer.get("resolution_name") or layer.get("resolution"),
        "layer_resolution_key": resolution_key,
        "resolution_key": resolution_key,
        "resolution_label": resolution_label,
        "resolution": layer.get("resolution"),
        "resolution_id": layer.get("resolution") if isinstance(layer.get("resolution"), int) else layer.get("resolution_id"),
        "is_sentinel": layer.get("is_sentinel"),
        "is_active": layer.get("is_active"),
        "is_experimental": layer.get("is_experimental"),
        "is_init_visible": layer.get("is_init_visible"),
        "menu_priority": layer.get("menu_priority"),
        "raw": layer,
    }

def _normalize_resolution(resolution: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": resolution.get("id"),
        "key": str(resolution.get("key") or resolution.get("resolution") or resolution.get("id") or ""),
        "resolution": resolution.get("resolution"),
        "label": resolution.get("resolution"),
        "is_init_visible": resolution.get("is_init_visible"),
        "instance_id": resolution.get("instance_id"),
        "config_aux": resolution.get("config_aux"),
        "raw": resolution,
    }


def _feature_collection_from_geometry(geometry: Any, parcel_id: str, name: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(geometry, str):
        import json
        geometry = json.loads(geometry)

    if not geometry:
        raise HTTPException(status_code=400, detail="El lote no tiene geometría")

    if geometry.get("type") == "FeatureCollection":
        features = geometry.get("features") or []
    elif geometry.get("type") == "Feature":
        features = [geometry]
    else:
        features = [{"type": "Feature", "properties": {}, "geometry": geometry}]

    polygon_features = []
    for feature in features:
        geom = feature.get("geometry") if isinstance(feature, dict) else None
        if not geom or geom.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        props = dict(feature.get("properties") or {})
        props.setdefault("name", name)
        props.setdefault("dataris_parcel_id", parcel_id)
        if metadata:
            props.setdefault("metadata", metadata)
        polygon_features.append({"type": "Feature", "properties": props, "geometry": geom})

    if not polygon_features:
        raise HTTPException(status_code=400, detail="El archivo no contiene polígonos válidos para Graniot")

    return {"type": "FeatureCollection", "features": polygon_features}


def _main_geometry(feature_collection: Dict[str, Any]) -> Dict[str, Any]:
    geoms = []
    for feature in feature_collection.get("features") or []:
        try:
            geom = shapely_shape(feature.get("geometry"))
            if not geom.is_empty:
                geoms.append(geom)
        except Exception:
            continue
    if not geoms:
        raise HTTPException(status_code=400, detail="No se pudo convertir la geometría del lote")
    union = unary_union(geoms)
    return mapping(union)


def _response_has_parcel_identity(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    return bool(
        item.get("id") is not None
        or item.get("key")
        or item.get("access_key")
        or item.get("wms_url")
        or item.get("image_url")
        or props.get("key")
        or props.get("wms_url")
        or props.get("image_url")
    )


def _first_parcel_response_object(value: Any) -> Optional[Dict[str, Any]]:
    """Find the first parcel-like object in common Graniot response shapes.

    The create endpoint can return a parcel directly, a paginated wrapper, or a
    wrapper with `data.parcels`. Dataris stores the first created Graniot id/key
    so the local parcel can render WMS layers afterwards.
    """
    if isinstance(value, list):
        for child in value:
            found = _first_parcel_response_object(child)
            if found:
                return found
        return None

    if not isinstance(value, dict):
        return None

    if _response_has_parcel_identity(value):
        return value

    for key in ("data", "parcel", "parcels", "results", "items", "features"):
        child = value.get(key)
        if child is None:
            continue
        found = _first_parcel_response_object(child)
        if found:
            return found

    return None


def _extract_graniot_ids(response: Any) -> Dict[str, Any]:
    obj = _first_parcel_response_object(response)
    props = obj.get("properties") if isinstance(obj, dict) else {}
    props = props or {}
    wms_url = None
    access_key_from_url = None
    if isinstance(obj, dict):
        # Graniot returns properties.wms_url with the signed WMS access_key and
        # properties.image_url with Geometry/BBOX. Merge them so /api/wms/ gets
        # the signed token and still has the raster template parameters.
        signed_url = props.get("wms_url") or obj.get("wms_url")
        image_template = props.get("image_url") or obj.get("image_url")
        wms_url = _merge_signed_wms_and_image_templates(signed_url, image_template)
    if isinstance(wms_url, str) and "access_key=" in wms_url:
        try:
            source_query = urlparse(wms_url).query or wms_url
            access_key_from_url = (parse_qs(source_query).get("access_key") or [None])[0]
        except Exception:
            access_key_from_url = None
    return {
        "graniot_parcel_id": obj.get("id") if isinstance(obj, dict) else None,
        "graniot_parcel_key": (obj.get("key") or props.get("key")) if isinstance(obj, dict) else None,
        # For /api/wms/ Graniot needs the signed token from properties.image_url,
        # not the UUID-like parcel key. Keep the UUID in graniot_parcel_key.
        "graniot_access_key": (access_key_from_url or obj.get("access_key") or props.get("access_key") or props.get("key") or obj.get("key")) if isinstance(obj, dict) else None,
        "graniot_wms_access_key": access_key_from_url,
        "graniot_wms_url": wms_url,
    }


def _extract_graniot_farm_id(response: Any) -> Optional[str]:
    obj = response
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        obj = response["data"]
    if isinstance(obj, dict) and isinstance(obj.get("results"), list) and obj["results"]:
        obj = obj["results"][0]
    if not isinstance(obj, dict):
        return None
    value = obj.get("id") or obj.get("farm") or obj.get("farm_id")
    return str(value) if value is not None and value != "" else None


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except Exception:
        return value


def _find_nested_value(value: Any, keys: set[str]) -> Optional[Any]:
    value = _safe_json_loads(value)
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for child in value.values():
            found = _find_nested_value(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested_value(child, keys)
            if found not in (None, ""):
                return found
    return None


def _access_key_from_wms_template(template: Optional[str]) -> Optional[str]:
    """Extract the signed WMS access_key from a Graniot image_url/template.

    Important: Graniot returns two different identifiers for parcels:
    - properties.key / access_key: a UUID-like parcel key used by API metadata.
    - image_url access_key: a signed token required by /api/wms/.

    Sending the UUID to /api/wms/ returns {"Invalid access key."}.
    """
    if not isinstance(template, str) or not template.strip():
        return None
    try:
        raw = template.strip()
        query = urlparse(raw).query if "?" in raw else raw
        value = (parse_qs(query).get("access_key") or [None])[0]
        return str(value).strip() if value else None
    except Exception:
        return None


def _wms_data_matches_requested(data: Dict[str, Any], *, access_key: Optional[str], graniot_parcel_id: Optional[str]) -> bool:
    wanted_key = _normalized_token(access_key)
    wanted_parcel_key = _normalized_token(_parcel_key_from_signed_wms_access_key(access_key))
    wanted_id = _normalized_token(graniot_parcel_id)

    keys = [
        data.get("graniot_access_key"),
        data.get("graniot_wms_access_key"),
        data.get("graniot_parcel_key"),
        _access_key_from_wms_template(data.get("graniot_wms_url")),
        _parcel_key_from_signed_wms_access_key(data.get("graniot_access_key")),
        _parcel_key_from_signed_wms_access_key(data.get("graniot_wms_access_key")),
        _parcel_key_from_signed_wms_access_key(_access_key_from_wms_template(data.get("graniot_wms_url"))),
    ]
    ids = [data.get("graniot_parcel_id")]

    if wanted_key and any(_normalized_token(value) == wanted_key for value in keys):
        return True
    if wanted_parcel_key and any(_normalized_token(value) == wanted_parcel_key for value in keys):
        return True
    if wanted_id and any(_normalized_token(value) == wanted_id for value in ids):
        return True
    return False


def _wms_template_from_local(
    local: Optional[Dict[str, Any]],
    *,
    access_key: Optional[str] = None,
    graniot_parcel_id: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(local, dict):
        return None

    # When a local Dataris lot was split into several Graniot parcels, each
    # subparcel can have its own image_url with a signed WMS access_key. Select
    # the image_url that matches the subparcel requested by the frontend.
    subparcels = local.get("graniot_parcels")
    if isinstance(subparcels, list):
        fallback_subparcel_template: Optional[str] = None
        # La fila padre guarda el graniot_parcel_id de la PRIMERA subparcela,
        # así que emparejar por id devolvía siempre la plantilla de esa primera
        # subparcela para las 13 del lote. La clave firmada que manda el
        # navegador identifica la subparcela exacta: primero se busca por clave
        # y solo si ninguna coincide se recurre al id.
        for match_by_key_only in (True, False):
            for item in subparcels:
                if not isinstance(item, dict):
                    continue
                template = item.get("graniot_wms_url") or item.get("graniot_image_url") or item.get("wms_url") or item.get("image_url")
                if isinstance(template, str) and template.strip() and not fallback_subparcel_template:
                    fallback_subparcel_template = template.strip()
                if not template:
                    continue
                data = {
                    "graniot_access_key": item.get("graniot_access_key"),
                    "graniot_wms_access_key": item.get("graniot_wms_access_key"),
                    "graniot_parcel_key": item.get("graniot_parcel_key"),
                    "graniot_parcel_id": item.get("graniot_parcel_id"),
                    "graniot_wms_url": template,
                }
                if match_by_key_only:
                    if access_key and _wms_data_matches_requested(data, access_key=access_key, graniot_parcel_id=None):
                        return str(template).strip()
                elif _wms_data_matches_requested(data, access_key=access_key, graniot_parcel_id=graniot_parcel_id):
                    return str(template).strip()
        if fallback_subparcel_template and not (access_key or graniot_parcel_id):
            return fallback_subparcel_template

    # graniot_raw contains the original Graniot response. It usually has
    # properties.image_url with the valid signed token. Prefer an object that
    # matches the requested raw UUID/key before falling back to row-level fields.
    raw_data = _wms_data_from_payload(local.get("graniot_raw"), access_key=access_key, parcel_id=graniot_parcel_id)
    if raw_data and isinstance(raw_data.get("graniot_wms_url"), str) and raw_data.get("graniot_wms_url").strip():
        return raw_data["graniot_wms_url"].strip()

    for key in ("graniot_wms_url", "graniot_image_url", "wms_url", "image_url"):
        value = local.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    found = _find_nested_value(local.get("graniot_raw"), {"image_url", "wms_url"})
    if isinstance(found, str) and found.strip():
        return found.strip()
    return None

def _query_params_from_wms_template(template: Optional[str]) -> Dict[str, Any]:
    if not isinstance(template, str) or not template.strip():
        return {}
    raw = template.strip()
    query = urlparse(raw).query if "?" in raw else raw
    params: Dict[str, Any] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key:
            params[key] = value
    return params


def _normalized_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _decode_signed_wms_access_key_payload(value: Any) -> Optional[Dict[str, Any]]:
    """Decode the public payload part of Graniot signed WMS access keys.

    Graniot access keys used by /api/wms/ look like:
    base64url({"parcel_key":"..."}):timestamp:signature

    We never validate or expose the signature here; this is only used to match
    the signed token that arrives from the frontend with the correct parcel row
    inside local graniot_raw/subparcel payloads. Without this match, Dataris can
    accidentally reuse the first stored image_url/template for every subparcel.
    """
    try:
        text = str(value or "").strip()
        if not text or ":" not in text or _is_uuid_like(text):
            return None
        first_part = text.split(":", 1)[0].strip()
        if not first_part:
            return None
        first_part += "=" * (-len(first_part) % 4)
        decoded = base64.urlsafe_b64decode(first_part.encode("utf-8"))
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _parcel_key_from_signed_wms_access_key(value: Any) -> Optional[str]:
    payload = _decode_signed_wms_access_key_payload(value)
    if not isinstance(payload, dict):
        return None
    parcel_key = payload.get("parcel_key") or payload.get("key") or payload.get("parcel")
    if parcel_key in (None, ""):
        return None
    return str(parcel_key).strip()


def _is_uuid_like(value: Any) -> bool:
    clean = str(value or "").strip()
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", clean))


def _signed_wms_access_key(value: Any) -> Optional[str]:
    key = _access_key_from_wms_template(value)
    if key and not _is_uuid_like(key):
        return key
    return None

def _signed_access_key_value(value: Any) -> Optional[str]:
    """Return a usable /api/wms/ access_key.

    Graniot rejects UUID parcel keys in /api/wms/. The signed WMS key is the
    long token normally found in image_url/wms_url or passed by the frontend.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or _is_uuid_like(text):
        return None
    return text


def _choose_wms_access_key(*values: Any) -> str:
    for value in values:
        signed = _signed_access_key_value(value)
        if signed:
            return signed
    for value in values:
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _merge_signed_wms_and_image_templates(wms_url: Any, image_url: Any) -> Optional[str]:
    """Build one reusable WMS template from Graniot properties.

    Graniot returns two different values for a parcel:
    - properties.wms_url: contains the signed access_key required by /api/wms/.
    - properties.image_url: contains the Geometry/BBOX/GetMap template, but in
      this environment it does not include access_key.

    Dataris must not use properties.key as access_key. This helper copies the
    signed access_key from wms_url into the image_url template when both exist.
    """
    wms_url_str = str(wms_url or "").strip()
    image_url_str = str(image_url or "").strip()

    signed_key = _signed_wms_access_key(wms_url_str) or _signed_wms_access_key(image_url_str)
    if not signed_key:
        return image_url_str or wms_url_str or None

    # Prefer image_url as the base because it carries Geometry/BBOX. If it does
    # not exist, keep wms_url as the template.
    base = image_url_str or wms_url_str
    if not base:
        return f"access_key={quote(signed_key)}&layers="

    try:
        parsed = urlparse(base)
        query = parsed.query if parsed.query else base
        params = parse_qs(query, keep_blank_values=True)
        flat = {k: (v[-1] if isinstance(v, list) and v else v) for k, v in params.items()}
        flat["access_key"] = signed_key
        # Keep layers empty as a template; _build_wms_param_variants sets the
        # selected layer later using lowercase `layers`.
        flat.setdefault("layers", "")
        encoded = urlencode(flat, doseq=False)
        if parsed.scheme and parsed.netloc:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/api/wms/", "", encoded, ""))
        if base.startswith("/"):
            return urlunparse(("", "", parsed.path or "/api/wms/", "", encoded, ""))
        return encoded
    except Exception:
        sep = "&" if "=" in base else ""
        return f"{base}{sep}access_key={quote(signed_key)}"



def _deep_iter_dicts(value: Any) -> List[Dict[str, Any]]:
    """Return all dictionaries nested in a Graniot payload.

    Graniot returns parcels in several shapes: /api/parcels/, farms[].farm_parcels,
    FeatureCollection.features, and wrappers with data/results/items. WMS recovery
    needs to find the feature that owns an access_key even when the local row was
    synchronized before Dataris started saving image_url/Geometry.
    """
    found: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        node = _safe_json_loads(node)
        if isinstance(node, dict):
            found.append(node)
            for child in node.values():
                if isinstance(child, (dict, list, str)):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _parcel_like_wms_object(item: Dict[str, Any]) -> bool:
    props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    return bool(
        item.get("image_url")
        or item.get("wms_url")
        or item.get("access_key")
        or item.get("key")
        or props.get("image_url")
        or props.get("wms_url")
        or props.get("access_key")
        or props.get("key")
    )


def _extract_wms_data_from_parcel_object(item: Dict[str, Any]) -> Dict[str, Any]:
    props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    bbox = item.get("bbox") or props.get("bbox")
    signed_url = props.get("wms_url") or item.get("wms_url")
    image_template = props.get("image_url") or item.get("image_url")
    merged_wms_template = _merge_signed_wms_and_image_templates(signed_url, image_template)
    signed_wms_key = _signed_wms_access_key(signed_url) or _signed_wms_access_key(merged_wms_template)
    parcel_key = item.get("key") or props.get("key")
    # Use the signed WMS token when available. The UUID key is still preserved
    # as graniot_parcel_key so old frontend URLs can be matched and upgraded.
    access_key = signed_wms_key or item.get("access_key") or props.get("access_key") or parcel_key
    return {
        "graniot_parcel_id": item.get("id") or props.get("id"),
        "graniot_parcel_key": parcel_key,
        "graniot_access_key": access_key,
        "graniot_wms_access_key": signed_wms_key,
        "graniot_wms_url": merged_wms_template,
        "graniot_image_url": image_template,
        "graniot_bbox": bbox,
        "graniot_geometry": item.get("geometry") or props.get("geometry"),
        "raw": item,
    }


def _parcel_object_matches(item: Dict[str, Any], *, access_key: Optional[str] = None, parcel_id: Optional[str] = None) -> bool:
    props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    wanted_key = _normalized_token(access_key)
    wanted_parcel_key = _normalized_token(_parcel_key_from_signed_wms_access_key(access_key))
    wanted_id = _normalized_token(parcel_id)

    signed_keys = [
        _signed_wms_access_key(props.get("wms_url") or item.get("wms_url")),
        _signed_wms_access_key(props.get("image_url") or item.get("image_url")),
    ]
    signed_payload_keys = [_parcel_key_from_signed_wms_access_key(value) for value in signed_keys]
    keys = [
        item.get("access_key"),
        item.get("key"),
        props.get("access_key"),
        props.get("key"),
        *signed_keys,
        *signed_payload_keys,
    ]
    ids = [item.get("id"), props.get("id"), item.get("parcel_id"), props.get("parcel_id")]

    if wanted_key and any(_normalized_token(value) == wanted_key for value in keys):
        return True
    if wanted_parcel_key and any(_normalized_token(value) == wanted_parcel_key for value in keys):
        return True
    if wanted_id and any(_normalized_token(value) == wanted_id for value in ids):
        return True
    return False


def _wms_data_from_payload(payload: Any, *, access_key: Optional[str] = None, parcel_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    fallback: Optional[Dict[str, Any]] = None
    has_match_criteria = bool(_normalized_token(access_key) or _normalized_token(parcel_id))
    for item in _deep_iter_dicts(payload):
        if not _parcel_like_wms_object(item):
            continue
        data = _extract_wms_data_from_parcel_object(item)
        if not fallback and data.get("graniot_wms_url"):
            fallback = data
        if _parcel_object_matches(item, access_key=access_key, parcel_id=parcel_id):
            return data
    # Never use a random farm parcel as a fallback when we are recovering a
    # specific access_key. That would paint the wrong lot on the map.
    return None if has_match_criteria else fallback


def _all_wms_data_from_payload(payload: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen = set()
    for item in _deep_iter_dicts(payload):
        if not _parcel_like_wms_object(item):
            continue
        data = _extract_wms_data_from_parcel_object(item)
        if not (data.get("graniot_access_key") or data.get("graniot_parcel_key") or data.get("graniot_parcel_id")):
            continue
        key = str(data.get("graniot_access_key") or data.get("graniot_parcel_key") or data.get("graniot_parcel_id"))
        if key in seen:
            continue
        seen.add(key)
        items.append(data)
    return items


def _latest_image_date_from_resolutions(value: Any, resolution_id: Optional[int] = None) -> Optional[str]:
    value = _safe_json_loads(value)
    if not isinstance(value, list):
        return None
    candidates: List[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if resolution_id is not None:
            try:
                if int(item.get("resolution")) != int(resolution_id):
                    continue
            except Exception:
                continue
        date = item.get("last_image_date") or item.get("date") or item.get("image_date")
        if date:
            candidates.append(str(date)[:10])
    return sorted(candidates)[-1] if candidates else None


def _public_graniot_subparcels(payload: Any) -> List[Dict[str, Any]]:
    public_items: List[Dict[str, Any]] = []
    for data in _all_wms_data_from_payload(payload):
        bounds = (
            _bbox_from_graniot_bbox(data.get("graniot_bbox"))
            or _bounds_from_wms_template(data.get("graniot_wms_url"))
            or _bounds_from_wms_template(data.get("graniot_image_url"))
        )
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        public_items.append({
            "graniot_parcel_id": data.get("graniot_parcel_id"),
            "graniot_parcel_key": data.get("graniot_parcel_key"),
            "graniot_access_key": data.get("graniot_access_key"),
            "graniot_wms_access_key": data.get("graniot_wms_access_key"),
            "graniot_wms_url": data.get("graniot_wms_url"),
            "graniot_image_url": data.get("graniot_image_url"),
            "graniot_bbox": data.get("graniot_bbox"),
            "bbox": data.get("graniot_bbox"),
            "graniot_geometry": data.get("graniot_geometry"),
            "name": props.get("name") or raw.get("name"),
            "hectares": props.get("hectares"),
            "parcelresolution_set": props.get("parcelresolution_set") or raw.get("parcelresolution_set"),
            "last_image_date": _latest_image_date_from_resolutions(props.get("parcelresolution_set") or raw.get("parcelresolution_set")),
            "bounds": bounds,
        })
    return public_items


def _bbox_from_graniot_bbox(value: Any) -> Optional[Dict[str, float]]:
    """Normalize a Graniot bbox.

    Graniot feature bbox is usually [west, south, east, north], while the WMS
    image_url BBOX is EPSG:4326 WMS 1.3.0 [south, west, north, east]. This helper
    only consumes feature-style arrays because query BBOX is already parsed from
    image_url templates.
    """
    value = _safe_json_loads(value)
    if not isinstance(value, list) or len(value) < 4:
        return None
    nums = [_float_or_none(v) for v in value[:4]]
    if any(v is None for v in nums):
        return None
    west, south, east, north = nums  # type: ignore[misc]
    if south is None or west is None or north is None or east is None:
        return None
    if not (south < north and west < east):
        return None
    return {"south": south, "west": west, "north": north, "east": east}


def _bounds_from_wms_template(template: Any) -> Optional[Dict[str, float]]:
    """Extract display bounds from Graniot's image_url/wms_url template.

    The frontend must place each WMS image using the BBOX returned by Graniot.
    If we stretch a subparcel raster over the full local FeatureCollection bbox,
    the NDVI appears shifted and leaves unpainted areas inside the parcel.
    """
    if not isinstance(template, str) or not template.strip():
        return None
    try:
        params = _query_params_from_wms_template(template)
        raw_bbox = params.get("BBOX") or params.get("bbox")
        if not raw_bbox:
            return None
        parts = [part.strip() for part in str(raw_bbox).split(",")]
        if len(parts) < 4:
            return None
        nums = [_float_or_none(part) for part in parts[:4]]
        if any(value is None for value in nums):
            return None
        a, b, c, d = nums  # type: ignore[misc]
        version = str(params.get("VERSION") or params.get("version") or "").strip()
        crs = str(params.get("CRS") or params.get("crs") or params.get("SRS") or params.get("srs") or "").upper()
        # WMS 1.3.0 + EPSG:4326 uses lat/lon. Graniot image_url currently uses
        # this order: BBOX=south,west,north,east.
        if version.startswith("1.3") and "4326" in crs:
            south, west, north, east = a, b, c, d
        else:
            west, south, east, north = a, b, c, d
        if south is None or west is None or north is None or east is None:
            return None
        if not (south < north and west < east):
            return None
        return {"south": south, "west": west, "north": north, "east": east}
    except Exception:
        return None


async def _recover_wms_data_from_graniot(
    client: GraniotClient,
    *,
    access_key: Optional[str],
    graniot_parcel_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ask Graniot for parcel metadata and recover image_url/Geometry.

    This fixes local rows synchronized with earlier versions where Dataris saved
    only the access_key but not properties.image_url. Without Geometry, Graniot's
    WMS often returns JSON/404/500 instead of a raster.
    """
    lookups: List[tuple[str, Optional[Dict[str, Any]]]] = []
    if graniot_parcel_id:
        lookups.append((f"/api/parcels/{graniot_parcel_id}/", None))
    lookups.extend([
        ("/api/parcels/", None),
        ("/api/farms/", None),
    ])

    for path, params in lookups:
        try:
            payload = await client.get(
                path,
                params=params,
                debug_context={
                    "operation": "recover-wms-data",
                    "access_key": access_key,
                    "graniot_parcel_id": graniot_parcel_id,
                },
            )
            recovered = _wms_data_from_payload(payload, access_key=access_key, parcel_id=graniot_parcel_id)
            if recovered and (recovered.get("graniot_wms_url") or recovered.get("graniot_access_key")):
                return recovered
        except Exception as exc:
            log_event({
                "event": "dataris.graniot.recover_wms_data.failed_lookup",
                "operation": "recover-wms-data",
                "path": path,
                "access_key": access_key,
                "graniot_parcel_id": graniot_parcel_id,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            })
            continue
    return None


_WMS_RECOVERY_TTL_SECONDS = 20 * 60
_WMS_RECOVERY_LOCKS: Dict[str, asyncio.Lock] = {}


def _wms_recovery_cache_key(graniot_parcel_id: Optional[str], access_key: Optional[str]) -> str:
    token = _normalized_token(graniot_parcel_id) or _normalized_token(_parcel_key_from_signed_wms_access_key(access_key)) or _normalized_token(access_key)
    return f"wms-recovery:{token}"


async def _recover_wms_data_shared(
    client: GraniotClient,
    *,
    access_key: Optional[str],
    graniot_parcel_id: Optional[str] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Recupera los metadatos WMS de Graniot una sola vez por parcela.

    Un lote dividido dispara una imagen por subparcela, todas a la vez, y cada
    una pedía a Graniot la misma parcela y reescribía la base local: con 13
    subparcelas el servidor dejó de responder al /health y Azure lo reinició.
    Las peticiones concurrentes de la misma parcela comparten una llamada y el
    resultado se recuerda 20 minutos (la clave firmada dura días). Con
    ``force`` se salta la memoria: es el camino del reintento cuando Graniot
    rechazó la clave.
    """
    cache_key = _wms_recovery_cache_key(graniot_parcel_id, access_key)
    if not force:
        cached = _cache_get(cache_key)
        if isinstance(cached, dict) and cached:
            return cached
    # Un asyncio.Lock queda ligado al loop que lo usa; se guarda por loop para
    # que un lock de otro ciclo (tests, reinicios) nunca se reutilice.
    lock_key = f"{id(asyncio.get_running_loop())}:{cache_key}"
    if len(_WMS_RECOVERY_LOCKS) > 500:
        _WMS_RECOVERY_LOCKS.clear()
    lock = _WMS_RECOVERY_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        if not force:
            cached = _cache_get(cache_key)
            if isinstance(cached, dict) and cached:
                return cached
        recovered = await _recover_wms_data_from_graniot(
            client,
            access_key=access_key,
            graniot_parcel_id=graniot_parcel_id,
        )
        if recovered:
            _cache_set(cache_key, recovered, _WMS_RECOVERY_TTL_SECONDS)
        return recovered


def _graniot_parcel_id_for_wms_request(local: Optional[Dict[str, Any]], access_key: Optional[str]) -> Optional[str]:
    """Id en Graniot de la (sub)parcela a la que pertenece la clave firmada.

    La fila padre lleva el id de su primera subparcela; si la clave del
    navegador es de otra subparcela, renovar con el id del padre devolvía la
    clave —y la imagen— equivocada.
    """
    if not isinstance(local, dict):
        return None
    wanted_key = _normalized_token(_parcel_key_from_signed_wms_access_key(access_key))
    wanted_raw = _normalized_token(access_key)
    subparcels = local.get("graniot_parcels")
    if (wanted_key or wanted_raw) and isinstance(subparcels, list):
        for item in subparcels:
            if not isinstance(item, dict):
                continue
            item_keys = {
                _normalized_token(item.get("graniot_parcel_key") or item.get("key")),
                _normalized_token(item.get("graniot_access_key")),
                _normalized_token(item.get("graniot_wms_access_key")),
                _normalized_token(_parcel_key_from_signed_wms_access_key(item.get("graniot_access_key"))),
                _normalized_token(_parcel_key_from_signed_wms_access_key(item.get("graniot_wms_access_key"))),
                _normalized_token(_parcel_key_from_signed_wms_access_key(_access_key_from_wms_template(item.get("graniot_wms_url")))),
            }
            item_keys.discard("")
            if (wanted_key and wanted_key in item_keys) or (wanted_raw and wanted_raw in item_keys):
                item_id = item.get("graniot_parcel_id") or item.get("id")
                if item_id not in (None, ""):
                    return str(item_id)
    if local.get("graniot_parcel_id"):
        return str(local.get("graniot_parcel_id"))
    return None


def _store_recovered_wms_data(local: Optional[Dict[str, Any]], data: Optional[Dict[str, Any]]) -> None:
    """Guarda en la fila local la clave/plantilla recién firmada por Graniot.

    Si la clave pertenece a una subparcela (``graniot_parcels``), se actualiza
    esa entrada y no la fila padre: antes cada imagen de un lote dividido
    pisaba los campos del padre con la subparcela que hubiera llegado última.
    Solo se reescribe la base cuando algún valor cambia de verdad; con 13
    imágenes concurrentes eso evita 13 reescrituras del JSON completo.
    """
    if not local or not data:
        return
    updates = {k: v for k, v in {
        "graniot_parcel_id": data.get("graniot_parcel_id"),
        "graniot_parcel_key": data.get("graniot_parcel_key"),
        "graniot_access_key": data.get("graniot_wms_access_key") or data.get("graniot_access_key"),
        "graniot_wms_access_key": data.get("graniot_wms_access_key"),
        "graniot_wms_url": data.get("graniot_wms_url"),
        "graniot_image_url": data.get("graniot_image_url"),
        "graniot_bbox": data.get("graniot_bbox"),
        "graniot_geometry": data.get("graniot_geometry"),
    }.items() if v not in (None, "")}
    if not updates:
        return
    recovered_id = _normalized_token(data.get("graniot_parcel_id"))
    recovered_key = _normalized_token(data.get("graniot_parcel_key"))

    def _same_graniot_parcel(item: Dict[str, Any]) -> bool:
        item_id = _normalized_token(item.get("graniot_parcel_id") or item.get("id"))
        item_key = _normalized_token(item.get("graniot_parcel_key") or item.get("key"))
        return bool((recovered_id and item_id == recovered_id) or (recovered_key and item_key == recovered_key))

    try:
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == local.get("id")), None)
            if not row:
                return
            subparcels = row.get("graniot_parcels")
            target: Optional[Dict[str, Any]] = None
            if isinstance(subparcels, list):
                target = next((item for item in subparcels if isinstance(item, dict) and _same_graniot_parcel(item)), None)
            if target is None and (not recovered_id and not recovered_key or _same_graniot_parcel(row)):
                target = row
            if target is None:
                # La clave recuperada no es de este lote ni de sus subparcelas:
                # no se pisa nada.
                return
            changed = {}
            for k, v in updates.items():
                if k in ("graniot_parcel_id", "graniot_parcel_key"):
                    # Graniot devuelve el id como entero; si ya está guardado
                    # (como texto), no se toca.
                    if _normalized_token(target.get(k)) != _normalized_token(v):
                        changed[k] = str(v)
                elif target.get(k) != v:
                    changed[k] = v
            if not changed:
                return
            target.update(changed)
            row["updated_at"] = now()
            write_db(db)
    except Exception as exc:
        log_event({
            "event": "dataris.graniot.store_recovered_wms_data.failed",
            "operation": "recover-wms-data",
            "local_parcel_id": local.get("id"),
            "exception_type": type(exc).__name__,
            "message": str(exc),
        })


_WMS_STORE_THREADS: List[threading.Thread] = []


def _store_recovered_wms_data_in_background(local: Optional[Dict[str, Any]], data: Optional[Dict[str, Any]]) -> None:
    """Persiste la clave renovada sin retrasar la imagen.

    Graniot re-firma la clave en cada lectura, así que casi siempre hay algo
    que guardar, y guardar es reescribir el JSON completo de la plataforma en
    Neon: 19 s medidos en producción. La imagen no tiene por qué esperar a eso.
    Va en un hilo propio (no en el event loop): el guardado captura sus
    excepciones y LOCK es un RLock, así que es seguro con el resto del proceso.
    """
    if not local or not data:
        return
    _WMS_STORE_THREADS[:] = [t for t in _WMS_STORE_THREADS if t.is_alive()]
    thread = threading.Thread(
        target=_store_recovered_wms_data,
        args=(local, data),
        name="wms-store-recovered",
        daemon=False,
    )
    _WMS_STORE_THREADS.append(thread)
    thread.start()


def _layer_identifier_candidates(layer: str) -> List[str]:
    """Return a small, safe set of Graniot WMS layer identifiers.

    Prefer the exact name requested by the frontend/catalog. Do not blindly
    convert underscores to hyphens: Graniot has valid names such as
    ACTUAL_EVAPOTRANSPIRATION and ANOMALY_MEAN_NDVI where that conversion makes
    a valid layer become invalid. Only explicit aliases are tried.
    """
    raw = str(layer or "").strip()
    if not raw:
        return []

    compact = raw.upper().replace(" ", "_").replace("-", "_")
    aliases: Dict[str, List[str]] = {
        "MOISTURE_INDEX": ["MOISTURE-INDEX", "MOISTURE_INDEX"],
        "NATURAL_COLOUR_SKY": [
            "NATURAL_COLOUR_SKY",
            "NATURAL_COLOR_SKYWATCH_RGB",
            "NATURAL-COLOUR",
            "NATURAL_COLOUR",
        ],
        "NATURAL_COLOR_SKY": [
            "NATURAL_COLOR_SKYWATCH_RGB",
            "NATURAL_COLOUR_SKY",
            "NATURAL-COLOUR",
            "NATURAL_COLOR",
        ],
        "NATURAL_COLOR_SKYWATCH_RGB": [
            "NATURAL_COLOR_SKYWATCH_RGB",
            "NATURAL_COLOUR_SKY",
            "NATURAL-COLOUR",
        ],
        "NATURAL_COLOUR": [
            "NATURAL-COLOUR",
            "NATURAL_COLOUR",
            "NATURAL_COLOR_SKYWATCH_RGB",
        ],
        "NATURAL_COLOR": [
            "NATURAL_COLOR",
            "NATURAL-COLOUR",
            "NATURAL_COLOR_SKYWATCH_RGB",
        ],
        "NATURAL_COLOUR_PLANET": ["NATURAL-COLOUR-PLANET", "NATURAL_COLOUR_PLANET"],
        "NATURAL_COLOR_PLANET": ["NATURAL-COLOUR-PLANET", "NATURAL_COLOR_PLANET"],
        "NATURAL_COLOUR_PLANET4": ["NATURAL-COLOUR-PLANET4", "NATURAL_COLOUR_PLANET4"],
        "NATURAL_COLOR_PLANET4": ["NATURAL-COLOUR-PLANET4", "NATURAL_COLOR_PLANET4"],
    }

    candidates: List[str] = [raw]
    candidates.extend(aliases.get(compact, []))
    candidates.append(raw.upper())

    deduped: List[str] = []
    seen = set()
    for item in candidates:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            deduped.append(value)
            seen.add(key)
        if len(deduped) >= 4:
            break
    return deduped


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        return number if number == number else None
    except Exception:
        return None


def _bbox_values_from_bounds(south: Any, west: Any, north: Any, east: Any) -> Optional[Dict[str, float]]:
    s = _float_or_none(south)
    w = _float_or_none(west)
    n = _float_or_none(north)
    e = _float_or_none(east)
    if s is None or w is None or n is None or e is None:
        return None
    if not (s < n and w < e):
        return None
    return {"south": s, "west": w, "north": n, "east": e}


def _bbox_from_bounds(south: Any, west: Any, north: Any, east: Any) -> Optional[str]:
    values = _bbox_values_from_bounds(south, west, north, east)
    if not values:
        return None
    # WMS 1.3.0 + EPSG:4326 uses latitude/longitude ordering.
    return f"{values['south']},{values['west']},{values['north']},{values['east']}"


def _bbox_lonlat_from_bounds(south: Any, west: Any, north: Any, east: Any) -> Optional[str]:
    values = _bbox_values_from_bounds(south, west, north, east)
    if not values:
        return None
    # Some WMS handlers still expect the classic lon/lat ordering.
    return f"{values['west']},{values['south']},{values['east']},{values['north']}"


def _wms_path_from_template(template: Optional[str]) -> str:
    """Return the WMS endpoint without keeping query params in the URL.

    The query params are parsed separately and sent via httpx `params`. Keeping
    `?access_key=...&layers=` inside an absolute URL while also sending params
    can make httpx/Graniot receive the wrong access_key or duplicate layer
    fields.
    """
    if not isinstance(template, str) or not template.strip():
        return "/api/wms/"

    raw = template.strip()

    if "=" in raw and not raw.startswith(("/", "http://", "https://")):
        return "/api/wms/"

    try:
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            path = parsed.path or "/api/wms/"
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        if parsed.path and parsed.path.startswith("/"):
            return parsed.path
    except Exception:
        pass

    return "/api/wms/"

def _clean_wms_params(params: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            clean[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            clean[key] = str(value)
        else:
            clean[key] = str(value)
    return clean


def _dedupe_wms_variants(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for params in variants:
        clean = _clean_wms_params(params)
        key = tuple(sorted(clean.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped


def _build_wms_param_variants(
    *,
    template_params: Dict[str, Any],
    access_key: str,
    layer: str,
    time: Optional[str],
    width: int,
    height: int,
    bbox_latlon: Optional[str],
    bbox_lonlat: Optional[str],
) -> List[Dict[str, Any]]:
    """Build Graniot WMS attempts from safest to most permissive.

    The uploaded OpenAPI documents `/api/wms/` as an access_key based endpoint
    with only `access_key`, `layers`, `time`, `width`, `height`, `bands`,
    `content`, `evalscript_url` and `response_format`. Therefore the first
    request is the official sized PNG. Legacy Geometry/BBOX/template variants
    remain as fallbacks for older Graniot deployments, but they no longer win
    before the official endpoint because a stale template BBOX can return an
    incomplete or shifted raster.
    """
    layer_value = str(layer or "").strip()
    template_access_key = template_params.get("access_key") or template_params.get("ACCESS_KEY")
    # IMPORTANT: when the frontend is rendering split Graniot subparcels it
    # passes the signed access_key for the exact tile being drawn. Do not let a
    # stale/first template access_key override it, otherwise every overlay is
    # requested with the same token and Graniot returns "Invalid access key.".
    # If the request key is only a UUID parcel key, then fall back to the signed
    # key extracted from the Graniot template.
    access_key_value = _choose_wms_access_key(access_key, template_access_key)

    variants: List[Dict[str, Any]] = []

    geometry = template_params.get("Geometry") or template_params.get("geometry")
    bbox_from_template = template_params.get("BBOX") or template_params.get("bbox")

    # 1) Exact Graniot image_url template. This is the most accurate request for
    # fitting the raster to the parcel because Graniot provides Geometry, BBOX,
    # WIDTH/HEIGHT and CRS in properties.image_url. The signed access_key comes
    # from properties.wms_url and is merged into template_params before here.
    if template_params and (geometry or bbox_from_template):
        exact_template = dict(template_params)
        exact_template["access_key"] = access_key_value
        exact_template["layers"] = layer_value
        exact_template.setdefault("FORMAT", "image/png")
        exact_template.setdefault("response_format", "image/png")
        exact_template.setdefault("WIDTH", width)
        exact_template.setdefault("HEIGHT", height)
        exact_template.setdefault("width", width)
        exact_template.setdefault("height", height)
        if time:
            exact_template["TIME"] = time
            exact_template["time"] = time
        variants.append(exact_template)

    # 2) Graniot-style request with the exact Geometry and requested BBOX.
    graniot_style = {
        "access_key": access_key_value,
        "layers": layer_value,
        "response_format": "image/png",
        "width": width,
        "height": height,
    }
    if geometry:
        graniot_style["Geometry"] = geometry
    if time:
        graniot_style["time"] = time
    if bbox_latlon:
        graniot_style["BBOX"] = bbox_latlon
    elif bbox_from_template:
        graniot_style["BBOX"] = bbox_from_template
    variants.append(graniot_style)

    # 3) Documented request plus BBOX. Some deployments accept it and it keeps
    # the raster coordinate system aligned with the overlay bounds.
    official_sized = {
        "access_key": access_key_value,
        "layers": layer_value,
        "response_format": "image/png",
        "width": width,
        "height": height,
    }
    if time:
        official_sized["time"] = time
    if bbox_latlon:
        official_bbox = dict(official_sized)
        official_bbox["BBOX"] = bbox_latlon
        variants.append(official_bbox)

    # 4) Official Graniot request with explicit image size.
    variants.append(official_sized)

    # 5) Official minimal request for deployments that choose size server-side.
    official_minimal = {
        "access_key": access_key_value,
        "layers": layer_value,
        "response_format": "image/png",
    }
    if time:
        official_minimal["time"] = time
    variants.append(official_minimal)

    if template_params:
        # 6) Sanitized template fallback.
        allowed_template_keys = {
            "bands",
            "content",
            "evalscript_url",
            "height",
            "layers",
            "response_format",
            "time",
            "width",
        }
        sanitized_template = {
            key: value
            for key, value in template_params.items()
            if str(key).strip() in allowed_template_keys
        }
        sanitized_template.update({
            "access_key": access_key_value,
            "layers": layer_value,
            "response_format": "image/png",
            "width": width,
            "height": height,
        })
        if time:
            sanitized_template["time"] = time
        variants.append(sanitized_template)

    # 7) Standard OGC WMS fallbacks are disabled by default because Graniot's
    # /api/wms/ endpoint expects lowercase `layers` and returns
    # {"layers": ["This field is required."]} when receiving `LAYERS`. Keep this
    # behind a flag only for private/legacy deployments that explicitly need it.
    if bool(getattr(settings, "GRANIOT_WMS_TRY_STANDARD_FALLBACK", False)):
        if bbox_latlon:
            standard_130 = {
                "access_key": access_key_value,
                "SERVICE": "WMS",
                "REQUEST": "GetMap",
                "VERSION": "1.3.0",
                "CRS": "EPSG:4326",
                "BBOX": bbox_latlon,
                "LAYERS": layer_value,
                "FORMAT": "image/png",
                "TRANSPARENT": "TRUE",
                "WIDTH": width,
                "HEIGHT": height,
            }
            if time:
                standard_130["TIME"] = time
            variants.append(standard_130)

        if bbox_lonlat:
            standard_111 = {
                "access_key": access_key_value,
                "SERVICE": "WMS",
                "REQUEST": "GetMap",
                "VERSION": "1.1.1",
                "SRS": "EPSG:4326",
                "BBOX": bbox_lonlat,
                "LAYERS": layer_value,
                "FORMAT": "image/png",
                "TRANSPARENT": "TRUE",
                "WIDTH": width,
                "HEIGHT": height,
            }
            if time:
                standard_111["TIME"] = time
            variants.append(standard_111)

    return variants

def _is_image_response(response: Any) -> bool:
    media_type = ""
    try:
        media_type = response.headers.get("content-type") or ""
    except Exception:
        media_type = ""
    content = getattr(response, "content", b"") or b""
    return bool(content) and "image" in media_type.lower()


def _iter_polygon_parts(geom: Any) -> List[Any]:
    if not geom or getattr(geom, "is_empty", True):
        return []
    geom_type = getattr(geom, "geom_type", "")
    if geom_type == "Polygon":
        return [geom]
    if geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not getattr(part, "is_empty", True)]
    if geom_type == "GeometryCollection":
        parts: List[Any] = []
        for child in geom.geoms:
            parts.extend(_iter_polygon_parts(child))
        return parts
    return []


def _geometry_bounds_look_latlon_swapped(geom: Any) -> bool:
    """Detect GeoJSON/WKT where coordinates arrived as [lat, lon].

    Dataris/Leaflet expects geometries internally as lon/lat (GeoJSON). Some
    Graniot templates serialize WKT as lat/lon. When X looks like latitude and Y
    looks like longitude we swap axes before clipping.
    """
    try:
        minx, miny, maxx, maxy = geom.bounds
        x_looks_like_lat = -90 <= minx <= 90 and -90 <= maxx <= 90
        y_looks_like_lon = abs(miny) > 90 or abs(maxy) > 90
        return bool(x_looks_like_lat and y_looks_like_lon)
    except Exception:
        return False


def _normalize_geometry_axes(geom: Any) -> Any:
    if _geometry_bounds_look_latlon_swapped(geom):
        try:
            return transform(lambda x, y, z=None: (y, x) if z is None else (y, x, z), geom)
        except Exception:
            return geom
    return geom


def _fix_polygonal_geometry(geom: Any) -> Optional[Any]:
    if not geom or getattr(geom, "is_empty", True):
        return None

    geom = _normalize_geometry_axes(geom)

    try:
        if shapely_make_valid is not None and not geom.is_valid:
            geom = shapely_make_valid(geom)
    except Exception:
        pass

    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
    except Exception:
        pass

    parts = _iter_polygon_parts(geom)
    if not parts:
        return None

    try:
        fixed_parts = []
        for part in parts:
            candidate = part
            try:
                if shapely_make_valid is not None and not candidate.is_valid:
                    candidate = shapely_make_valid(candidate)
            except Exception:
                pass
            try:
                if not candidate.is_valid:
                    candidate = candidate.buffer(0)
            except Exception:
                pass
            fixed_parts.extend(_iter_polygon_parts(candidate))
        if not fixed_parts:
            return None
        return unary_union(fixed_parts)
    except Exception:
        return parts[0] if len(parts) == 1 else None


def _geometry_from_geojson_value(value: Any) -> Optional[Any]:
    value = _safe_json_loads(value)
    if not isinstance(value, dict):
        return None
    try:
        if value.get("type") == "Feature":
            geometry = value.get("geometry")
        elif value.get("type") == "FeatureCollection":
            geoms = []
            for feature in value.get("features") or []:
                if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict):
                    parsed = _geometry_from_geojson_value(feature.get("geometry"))
                    if parsed is not None:
                        geoms.append(parsed)
            return _fix_polygonal_geometry(unary_union(geoms)) if geoms else None
        else:
            geometry = value
        if not isinstance(geometry, dict):
            return None
        return _fix_polygonal_geometry(shapely_shape(geometry))
    except Exception:
        return None

def _geometry_from_wkt_template(value: Any) -> Optional[Any]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        geom = shapely_wkt.loads(raw)
    except Exception:
        return None

    return _fix_polygonal_geometry(geom)


def _subparcel_matches_request(item: Dict[str, Any], *, access_key: Optional[str], graniot_parcel_id: Optional[str]) -> bool:
    data = {
        "graniot_access_key": item.get("graniot_access_key") or item.get("access_key"),
        "graniot_wms_access_key": item.get("graniot_wms_access_key"),
        "graniot_parcel_key": item.get("graniot_parcel_key") or item.get("key"),
        "graniot_parcel_id": item.get("graniot_parcel_id") or item.get("id"),
        "graniot_wms_url": item.get("graniot_wms_url") or item.get("wms_url") or item.get("image_url"),
    }
    if _wms_data_matches_requested(data, access_key=access_key, graniot_parcel_id=graniot_parcel_id):
        return True
    image_url = item.get("graniot_image_url") or item.get("image_url")
    if image_url:
        data["graniot_wms_url"] = image_url
        return _wms_data_matches_requested(data, access_key=access_key, graniot_parcel_id=graniot_parcel_id)
    return False


def _geometry_from_subparcel_item(item: Dict[str, Any]) -> Optional[Any]:
    for candidate in (
        item.get("geometry"),
        item.get("geom"),
        item.get("graniot_geometry"),
        item.get("raw", {}).get("geometry") if isinstance(item.get("raw"), dict) else None,
    ):
        geom = _geometry_from_geojson_value(candidate)
        if geom is not None:
            return geom

    raw = item.get("raw")
    if isinstance(raw, dict):
        data = _extract_wms_data_from_parcel_object(raw)
        geom = _geometry_from_geojson_value(data.get("graniot_geometry"))
        if geom is not None:
            return geom
    return None


def _clip_geometry_from_payload(
    local: Optional[Dict[str, Any]],
    template_params: Dict[str, Any],
    recovered_wms_data: Optional[Dict[str, Any]],
    *,
    access_key: Optional[str] = None,
    graniot_parcel_id: Optional[str] = None,
) -> tuple[Optional[Any], str]:
    """Return the geometry used to clip the WMS image.

    The mask must follow the exact lot/sub-lot being requested. Prefer the
    matching Graniot subparcel geometry when the frontend asks for a specific
    signed access_key, then fall back to recovered Graniot metadata and finally
    to the local Dataris geometry. All inputs are repaired with Shapely and axes
    are normalized to lon/lat before raster masking.
    """
    if local:
        subparcels = local.get("graniot_parcels")
        if isinstance(subparcels, list):
            fallback_subparcel_geom: Optional[Any] = None
            for item in subparcels:
                if not isinstance(item, dict):
                    continue
                geom = _geometry_from_subparcel_item(item)
                if geom is not None and fallback_subparcel_geom is None:
                    fallback_subparcel_geom = geom
                if geom is not None and _subparcel_matches_request(item, access_key=access_key, graniot_parcel_id=graniot_parcel_id):
                    return geom, "local.graniot_parcels.match.geometry"
            if fallback_subparcel_geom is not None and not (access_key or graniot_parcel_id):
                return fallback_subparcel_geom, "local.graniot_parcels.first.geometry"

    if recovered_wms_data:
        geom = _geometry_from_geojson_value(recovered_wms_data.get("graniot_geometry"))
        if geom is not None:
            return geom, "recovered.graniot_geometry"

    if local:
        raw_data = _wms_data_from_payload(
            local.get("graniot_raw"),
            access_key=access_key or local.get("graniot_wms_access_key") or local.get("graniot_access_key") or local.get("graniot_parcel_key"),
            parcel_id=graniot_parcel_id or local.get("graniot_parcel_id"),
        )
        if raw_data:
            geom = _geometry_from_geojson_value(raw_data.get("graniot_geometry"))
            if geom is not None:
                return geom, "local.graniot_raw.match.geometry"

        for source_name, candidate in (
            ("local.geometry", local.get("geometry")),
            ("local.graniot_geometry", local.get("graniot_geometry")),
        ):
            geom = _geometry_from_geojson_value(candidate)
            if geom is not None:
                return geom, source_name

    template_geometry = template_params.get("Geometry") or template_params.get("geometry")
    geom = _geometry_from_wkt_template(template_geometry)
    if geom is not None:
        return geom, "template.Geometry"

    return None, "none"

def _clip_bounds_from_context(template: Optional[str], bbox_values: Optional[Dict[str, float]], local: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    bounds = _bounds_from_wms_template(template)
    if bounds:
        return bounds
    if bbox_values:
        return bbox_values
    if local:
        for key in ("graniot_image_url", "graniot_wms_url", "image_url", "wms_url"):
            bounds = _bounds_from_wms_template(local.get(key))
            if bounds:
                return bounds
        raw_data = _wms_data_from_payload(local.get("graniot_raw"), access_key=local.get("graniot_parcel_key"), parcel_id=local.get("graniot_parcel_id"))
        if raw_data:
            bounds = _bounds_from_wms_template(raw_data.get("graniot_image_url")) or _bounds_from_wms_template(raw_data.get("graniot_wms_url"))
            if bounds:
                return bounds
    return None


def _bounds_from_wms_params(params: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Parse the exact BBOX sent to the successful WMS request.

    The mask must use the same coordinate extent as the returned image. Graniot
    accepts both WMS 1.3 EPSG:4326 order (south,west,north,east) and classic
    lon/lat order in different templates, so this parser validates both and
    picks the one implied by VERSION/CRS first.
    """
    if not isinstance(params, dict):
        return None
    raw_bbox = params.get("BBOX") or params.get("bbox")
    if not raw_bbox:
        return None
    try:
        parts = [str(part).strip() for part in str(raw_bbox).split(",")]
        if len(parts) < 4:
            return None
        nums = [_float_or_none(part) for part in parts[:4]]
        if any(value is None for value in nums):
            return None
        a, b, c, d = nums  # type: ignore[misc]
        version = str(params.get("VERSION") or params.get("version") or "").strip()
        crs = str(params.get("CRS") or params.get("crs") or params.get("SRS") or params.get("srs") or "").upper()

        def latlon_candidate() -> Optional[Dict[str, float]]:
            south, west, north, east = a, b, c, d
            if south is None or west is None or north is None or east is None:
                return None
            if -90 <= south < north <= 90 and -180 <= west < east <= 180:
                return {"south": float(south), "west": float(west), "north": float(north), "east": float(east)}
            return None

        def lonlat_candidate() -> Optional[Dict[str, float]]:
            west, south, east, north = a, b, c, d
            if south is None or west is None or north is None or east is None:
                return None
            if -90 <= south < north <= 90 and -180 <= west < east <= 180:
                return {"south": float(south), "west": float(west), "north": float(north), "east": float(east)}
            return None

        if version.startswith("1.3") and "4326" in crs:
            return latlon_candidate() or lonlat_candidate()
        if version or crs:
            return lonlat_candidate() or latlon_candidate()
        return latlon_candidate() or lonlat_candidate()
    except Exception:
        return None


def _pixel_points_from_coords(coords: Any, bounds: Dict[str, float], width: int, height: int, scale: int = 1) -> List[tuple[int, int]]:
    south = float(bounds["south"])
    north = float(bounds["north"])
    west = float(bounds["west"])
    east = float(bounds["east"])
    lon_span = east - west
    lat_span = north - south
    if lon_span <= 0 or lat_span <= 0:
        return []
    points: List[tuple[int, int]] = []
    for lon, lat in list(coords):
        x = ((float(lon) - west) / lon_span) * width * scale
        y = ((north - float(lat)) / lat_span) * height * scale
        # Keep a tiny padding around the image to avoid cutting a border pixel
        # from polygons that lie exactly on the WMS bbox.
        x = max(-2 * scale, min((width + 2) * scale, x))
        y = max(-2 * scale, min((height + 2) * scale, y))
        points.append((int(round(x)), int(round(y))))
    return points

def _pixel_points_from_polygon(poly: Any, bounds: Dict[str, float], width: int, height: int, scale: int = 1) -> List[tuple[int, int]]:
    return _pixel_points_from_coords(poly.exterior.coords, bounds, width, height, scale=scale)


def _geometry_intersection_with_bounds(geometry: Any, bounds: Dict[str, float]) -> Optional[Any]:
    try:
        clipping_box = shapely_box(
            float(bounds["west"]),
            float(bounds["south"]),
            float(bounds["east"]),
            float(bounds["north"]),
        )
        clipped = geometry.intersection(clipping_box)
        return _fix_polygonal_geometry(clipped)
    except Exception:
        return _fix_polygonal_geometry(geometry)


def _apply_backend_polygon_mask(
    content: bytes,
    *,
    media_type: str,
    bounds: Optional[Dict[str, float]],
    geometry: Optional[Any],
) -> tuple[bytes, str, bool, Dict[str, Any]]:
    if not content or not bounds or geometry is None:
        return content, media_type, False, {"reason": "missing_content_bounds_or_geometry"}

    try:
        image = Image.open(BytesIO(content)).convert("RGBA")
    except Exception as exc:
        return content, media_type, False, {"reason": "open_image_failed", "message": str(exc)}

    width, height = image.size
    if width <= 0 or height <= 0:
        return content, media_type, False, {"reason": "invalid_image_size", "width": width, "height": height}

    geometry = _geometry_intersection_with_bounds(geometry, bounds)
    parts = _iter_polygon_parts(geometry)
    if not parts:
        return content, media_type, False, {"reason": "no_polygon_intersection", "bounds": bounds}

    # Backend mask in the exact WMS response coordinate system. The alpha channel
    # is rebuilt from the repaired polygon, not multiplied with the source alpha.
    # This prevents Graniot transparent padding/nodata inside the lot from making
    # the image look incomplete while still preventing any bleed outside the lot.
    scale = 4 if max(width, height) <= 2048 else 2
    mask = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(mask)
    drew_any = False
    part_count = 0
    hole_count = 0

    for poly in parts:
        exterior_points = _pixel_points_from_coords(poly.exterior.coords, bounds, width, height, scale=scale)
        if len(exterior_points) < 3:
            continue
        draw.polygon(exterior_points, fill=255)
        drew_any = True
        part_count += 1

        # Real inner rings are preserved as transparent holes. Invalid/self-
        # intersecting rings were repaired by Shapely before this point.
        for interior in getattr(poly, "interiors", []) or []:
            interior_points = _pixel_points_from_coords(interior.coords, bounds, width, height, scale=scale)
            if len(interior_points) >= 3:
                draw.polygon(interior_points, fill=0)
                hole_count += 1

    if not drew_any:
        return content, media_type, False, {"reason": "mask_draw_empty", "bounds": bounds}

    if scale != 1:
        try:
            resampling = Image.Resampling.LANCZOS
        except AttributeError:
            resampling = Image.LANCZOS
        mask = mask.resize((width, height), resampling)
        # Hard threshold: no leaks outside the local polygon, including complex
        # rings. Low threshold keeps border pixels from disappearing.
        mask = mask.point(lambda value: 255 if value >= 16 else 0)

    image.putalpha(mask)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), "image/png", True, {
        "width": width,
        "height": height,
        "parts": part_count,
        "holes": hole_count,
        "bounds": bounds,
        "alpha_mode": "geometry_mask",
    }

def _response_preview_text(response: Any) -> str:
    try:
        return (response.text or "")[:500]
    except Exception:
        content = getattr(response, "content", b"") or b""
        return str(content[:250])


def _payload_error_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail") or detail.get("error")
    message = detail or payload.get("message") or payload.get("error")
    if not message:
        return None
    text = str(message).strip()
    if not text:
        return None
    lowered = text.lower()
    known_errors = (
        "missing farm",
        "invalid api key",
        "not authenticated",
        "authentication",
        "permission",
        "required",
        "error",
    )
    if any(marker in lowered for marker in known_errors):
        return text
    return None


def _normalize_farm(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name") or item.get("displayed_name") or item.get("label") or f"Finca {item.get('id')}",
        "type": item.get("type"),
        "is_active": item.get("is_active"),
        "center": item.get("center"),
        "total_hectares": item.get("total_hectares"),
        "raw": item,
    }


def _select_farm_id(farms_payload: Any) -> Optional[str]:
    farms = _items(farms_payload)
    if not farms:
        return None

    active = [farm for farm in farms if farm.get("is_active") is not False]
    selected = active[0] if active else farms[0]
    value = selected.get("id")
    return str(value) if value is not None and value != "" else None


def _farm_name_equals(item: Dict[str, Any], name: str) -> bool:
    return str(item.get("name") or "").strip().lower() == name.strip().lower()


async def _find_farm_by_name(client: GraniotClient, name: str) -> Optional[Dict[str, Any]]:
    """Return an existing Graniot farm by exact name when the API allows listing.

    Graniot sometimes returns HTTP 200 with a validation payload instead of a
    created farm. Refreshing the list lets Dataris recover if the farm was
    actually created or already existed.
    """
    try:
        farms_raw = await client.get("/api/farms/")
    except Exception:
        return None

    farms = [_normalize_farm(item) for item in _items(farms_raw)]
    matches = [farm for farm in farms if _farm_name_equals(farm, name)]
    if matches:
        return matches[-1]
    return None


def _farm_create_attempts(name: str, farm_type: str, is_active: bool) -> List[Dict[str, Any]]:
    """Build a short, ordered list of farm-create attempts.

    The uploaded Graniot OpenAPI file documents POST /api/farms/ as Farm Create
    and the response schema uses `name`, `type` and `is_active`, but the live
    endpoint can return HTTP 200 with `{message: "Missing Farm Name"}` when the
    serializer is expecting camelCase fields. The Graniot web app commonly uses
    frontend-style names, so we try `farmName` first and keep the retry list
    intentionally small to avoid the request storm that happened previously.
    """
    return [
        {
            "label": "form:farmName",
            "mode": "form",
            "payload": {
                "farmName": name,
                "farmType": farm_type,
                "type": farm_type,
                "is_active": "true" if is_active else "false",
            },
        },
        {
            "label": "json:farmName",
            "mode": "json",
            "payload": {
                "farmName": name,
                "farmType": farm_type,
                "type": farm_type,
                "is_active": is_active,
            },
        },
        {
            "label": "json:openapi-name",
            "mode": "json",
            "payload": {
                "name": name,
                "type": farm_type,
                "is_active": is_active,
            },
        },
        {
            "label": "form:openapi-name",
            "mode": "form",
            "payload": {
                "name": name,
                "type": farm_type,
                "is_active": "true" if is_active else "false",
            },
        },
    ]


def _farm_response_is_valid(raw: Any, expected_name: str) -> Optional[Dict[str, Any]]:
    """Normalize a Graniot farm response only when it has an actual id.

    Graniot sometimes returns HTTP 200 for validation errors. We therefore do
    not trust the HTTP status alone; a successful create must include an id.
    """
    items = _items(raw)
    if not items and isinstance(raw, dict):
        items = [raw]
    for item in items:
        if not isinstance(item, dict):
            continue
        farm = _normalize_farm(item)
        if farm.get("id") not in (None, ""):
            # Prefer matching name, but accept a single returned farm with id.
            returned_name = str(farm.get("name") or "").strip()
            if not returned_name or returned_name.lower() == expected_name.strip().lower() or len(items) == 1:
                return farm
    return None


async def _create_farm_on_graniot(client: GraniotClient, name: str, farm_type: str, is_active: bool = True) -> Dict[str, Any]:
    """Create a farm in Graniot and return a normalized farm object.

    This version avoids brute-force query/json/form combinations. It uses the
    schema fields from the uploaded OpenAPI docs plus the camelCase fields used
    by many Graniot frontend serializers, and it performs only one list refresh
    at the beginning and one at the end.
    """
    existing = await _find_farm_by_name(client, name)
    if existing and existing.get("id") not in (None, ""):
        return existing

    last_payload: Any = None
    last_message: Optional[str] = None
    last_status = 400
    attempts_used: List[str] = []

    for attempt in _farm_create_attempts(name, farm_type, is_active):
        label = str(attempt["label"])
        mode = str(attempt["mode"])
        body = attempt["payload"]
        attempts_used.append(label)
        try:
            if mode == "form":
                candidate = await client.post_form("/api/farms/", data=body)
            else:
                candidate = await client.post("/api/farms/", json_body=body)
        except GraniotAPIError as exc:
            last_status = exc.status_code
            last_payload = exc.payload
            last_message = str(exc)
            # If the endpoint/account does not allow creation, fail quickly.
            if exc.status_code == 405 or "method not allowed" in str(exc).lower():
                raise GraniotAPIError(
                    405,
                    "La API de Graniot respondió Method Not Allowed al crear fincas. "
                    "Crea la finca en Graniot y selecciónala en Dataris, o configura GRANIOT_DEFAULT_FARM_ID.",
                    exc.payload,
                )
            # 400 can mean validation; continue with the next documented variant.
            if exc.status_code in {400, 401, 403, 415}:
                continue
            raise

        last_payload = candidate
        error = _payload_error_message(candidate)
        if error:
            last_message = error
            last_status = 400
            # Continue only for the specific serializer mismatch we are solving.
            if "missing farm name" in error.lower() or "farm name" in error.lower():
                continue
            raise GraniotAPIError(400, error, candidate)

        farm = _farm_response_is_valid(candidate, name)
        if farm:
            return farm

        # HTTP 200 with no validation message and no id is still not usable.
        last_message = "Graniot respondió sin id de finca"
        last_status = 400

    # One final refresh: if the farm was created but the response did not expose
    # an id, recover it from the farm list without making repeated GETs.
    found = await _find_farm_by_name(client, name)
    if found and found.get("id") not in (None, ""):
        return found

    detail = last_message or "No se pudo crear la finca en Graniot"
    if "missing farm name" in detail.lower() or "farm name" in detail.lower():
        detail = (
            "Graniot siguió respondiendo 'Missing Farm Name'. "
            "Se probaron los campos farmName y name según el YAML/API. "
            "Si persiste, esa cuenta/API puede no permitir crear fincas externas; "
            "créala en Graniot y selecciónala con Recargar o usa el ID manual."
        )
    raise GraniotAPIError(
        last_status,
        detail,
        {"last_payload": last_payload, "attempts": attempts_used},
    )

async def _create_default_farm(client: GraniotClient, *, name: Optional[str] = None) -> str:
    name = str(name or "").strip() or settings.GRANIOT_DEFAULT_FARM_NAME or "Dataris"
    farm_type = settings.GRANIOT_DEFAULT_FARM_TYPE or "PRO"
    farm = await _create_farm_on_graniot(client, name, farm_type, True)
    farm_id = farm.get("id")
    if not farm_id:
        raise GraniotAPIError(400, "Graniot creó/contestó una finca sin devolver id. Configura GRANIOT_DEFAULT_FARM_ID manualmente.", farm)
    return str(farm_id)


def _clean_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("id") or value.get("pk") or value.get("value")
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"undefined", "null", "none"}:
        return None
    return text

def _as_int_if_numeric(value: Any) -> Any:
    """Return an int for numeric ids because Graniot serializers can be strict.

    The UI gives us farm ids as strings (for example "3615"), but DRF
    serializers often validate FK fields as integers. Sending a string can make
    Graniot behave as if the field was missing.
    """
    clean = _clean_id(value)
    if clean is None:
        return None
    return int(clean) if clean.isdigit() else clean


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _feature_name(feature: Dict[str, Any], fallback: str) -> str:
    props = feature.get("properties") if isinstance(feature, dict) else None
    props = props or {}
    return str(
        props.get("name")
        or props.get("Name")
        or props.get("nombre")
        or props.get("NOMBRE")
        or props.get("lote")
        or props.get("LOTE")
        or fallback
    )


def _with_farm_properties(feature_collection: Dict[str, Any], farm_ref: Any, name: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a FeatureCollection whose features carry the farm id.

    The Graniot OpenAPI shows Parcel as GeoJSON Feature and /api/farms/{id}/parcels/
    as GET-only. Therefore parcel creation must happen through POST /api/parcels/.
    The live API, however, validates a hidden Farm ID. To keep the body aligned
    with GeoJSON serializers, we include the relation inside each feature's
    properties as the first-class place, and only then duplicate it at top level
    in the request variants.
    """
    cloned = json.loads(json.dumps(feature_collection))
    features = cloned.get("features") if isinstance(cloned, dict) else []
    if not isinstance(features, list):
        features = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            props = {}
            feature["properties"] = props
        props.setdefault("name", _feature_name(feature, name) or f"{name} {index + 1}")
        props["is_active"] = True
        props["farm"] = farm_ref
        props["farm_id"] = farm_ref
        props["farmId"] = farm_ref
        props["metadata"] = {**metadata, **(props.get("metadata") if isinstance(props.get("metadata"), dict) else {})}
    return cloned


def _first_polygon_feature(feature_collection: Dict[str, Any], name: str, fallback_geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Return one Polygon feature for /api/parcels/ create.

    Graniot's Parcel schema documents geometry.type = Polygon, not MultiPolygon.
    If a zip contains several polygons, the safest create payload is a normal
    GeoJSON FeatureCollection first, then a single Feature fallback.
    """
    for feature in feature_collection.get("features") or []:
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry")
        if isinstance(geom, dict) and geom.get("type") == "Polygon":
            return {
                "type": "Feature",
                "geometry": geom,
                "properties": dict(feature.get("properties") or {"name": name}),
            }
    return {"type": "Feature", "geometry": fallback_geometry, "properties": {"name": name}}


def _coerce_coordinates(value: Any) -> Any:
    """Turn nested tuples into JSON lists.

    Shapely's ``mapping()`` (used when a lot is normalized on creation) returns
    coordinates as nested tuples. Rows read straight from the in-memory compat
    store therefore carry tuples instead of lists, and the previous
    ``isinstance(..., list)`` checks silently rejected a perfectly valid polygon.
    """
    if isinstance(value, (list, tuple)):
        return [_coerce_coordinates(item) for item in value]
    return value


def _polygon_geometries(geometry: Any) -> List[Dict[str, Any]]:
    """Return Polygon geometries accepted by Graniot's parcels array.

    Graniot support confirmed POST /api/parcels/ expects one polygon under
    each `parcels[].geom`. MultiPolygon inputs are split into individual Polygon
    entries so the request keeps one polygon per parcel object.
    """
    if not isinstance(geometry, dict):
        return []

    geom_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geom_type == "Polygon" and isinstance(coordinates, (list, tuple)):
        return [{"type": "Polygon", "coordinates": _coerce_coordinates(coordinates)}]

    if geom_type == "MultiPolygon" and isinstance(coordinates, (list, tuple)):
        polygons: List[Dict[str, Any]] = []
        for polygon_coordinates in coordinates:
            if isinstance(polygon_coordinates, (list, tuple)) and polygon_coordinates:
                polygons.append({"type": "Polygon", "coordinates": _coerce_coordinates(polygon_coordinates)})
        return polygons

    return []


def _parcel_metadata(metadata: Dict[str, Any], feature: Dict[str, Any], feature_index: int, polygon_index: int) -> Dict[str, Any]:
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    nested_metadata = props.get("metadata") if isinstance(props.get("metadata"), dict) else {}
    clean_props = {
        key: value
        for key, value in props.items()
        if key not in {"metadata", "farm", "farm_id", "farmId"} and value not in (None, "")
    }
    result = {**metadata, **nested_metadata}
    if clean_props:
        result.setdefault("source_properties", clean_props)
    result.setdefault("feature_index", feature_index)
    result.setdefault("polygon_index", polygon_index)
    return result


def _build_graniot_parcels_payload(feature_collection: Dict[str, Any], farm_ref: Any, name: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Build the exact payload confirmed by Graniot support for POST /api/parcels/.

    Required shape:
    {
      "farm": {"id": 3615},
      "parcels": [{"name": "Lote 1", "metadata": {...}, "geom": GeoJSON Feature}]
    }
    """
    if farm_ref in (None, ""):
        raise HTTPException(status_code=400, detail="Missing Farm ID")

    entries: List[Dict[str, Any]] = []
    features = feature_collection.get("features") if isinstance(feature_collection, dict) else []
    if not isinstance(features, list):
        features = []

    for feature_index, feature in enumerate(features):
        if not isinstance(feature, dict):
            continue
        polygons = _polygon_geometries(feature.get("geometry"))
        if not polygons:
            continue

        base_name = _feature_name(feature, name) or name or "Lote"
        for polygon_index, polygon_geometry in enumerate(polygons):
            entries.append({
                "base_name": base_name,
                "metadata": _parcel_metadata(metadata, feature, feature_index, polygon_index),
                "geom": {
                    "type": "Feature",
                    "properties": {},
                    "geometry": polygon_geometry,
                },
            })

    if not entries:
        raise HTTPException(status_code=400, detail="El archivo no contiene polígonos válidos para Graniot")

    name_counts: Dict[str, int] = {}
    parcels: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        base_name = str(entry.pop("base_name") or name or "Lote").strip() or "Lote"
        name_counts[base_name] = name_counts.get(base_name, 0) + 1
        should_suffix = len(entries) > 1 and (base_name == name or name_counts[base_name] > 1)
        parcel_name = f"{base_name} {index}" if should_suffix else base_name
        parcels.append({
            "name": parcel_name,
            "metadata": entry["metadata"],
            "geom": entry["geom"],
        })

    return {
        "farm": {"id": farm_ref},
        "parcels": parcels,
    }


def _flat_metadata(metadata: Any) -> Dict[str, Any]:
    """Graniot documents PATCH metadata as flat key/value pairs."""
    flat: Dict[str, Any] = {}
    if not isinstance(metadata, dict):
        return flat
    for key, value in metadata.items():
        if value in (None, ""):
            continue
        flat[str(key)] = value if isinstance(value, (str, int, float, bool)) else _json_dumps(value)
    return flat


def _build_graniot_parcel_patch_payload(parcel: Dict[str, Any], remote_id: Any) -> Dict[str, Any]:
    """Build the PATCH /api/parcels/{id}/ body documented by Graniot.

    Updating a parcel does NOT reuse the create shape: it expects ``id``, an
    optional ``name``/flat ``metadata`` and the geometry under
    ``parcelGeoJson`` as a FeatureCollection whose feature carries the parcel id.
    Sending the create-style ``geom`` key makes Graniot answer HTTP 500.
    """
    geometry = (parcel.get("geom") or {}).get("geometry") if isinstance(parcel.get("geom"), dict) else None
    if not geometry:
        raise HTTPException(status_code=400, detail="No hay geometría para actualizar en Graniot")
    reference = _as_int_if_numeric(remote_id)
    return {
        "id": reference,
        "name": parcel.get("name"),
        "metadata": _flat_metadata(parcel.get("metadata")),
        "parcelGeoJson": {
            "type": "FeatureCollection",
            "features": [{"id": reference, "type": "Feature", "geometry": geometry}],
        },
    }


async def _farm_id_for_finca(client: GraniotClient, finca: str) -> Optional[str]:
    """Finca de Graniot que corresponde a la finca del lote, creándola si falta.

    Cada finca de Dataris (la que agrupa los lotes de un mismo KML) debe ser una
    finca en el portal de Graniot. Sin esto, todos los lotes del cliente caen en
    la primera finca que exista en su cuenta y el portal los muestra amontonados
    bajo un único nombre, sin las secciones que el cliente reconoce.
    """
    name = str(finca or "").strip()
    if not name:
        return None
    try:
        farm = await _create_farm_on_graniot(
            client,
            name,
            settings.GRANIOT_DEFAULT_FARM_TYPE or "PRO",
            True,
        )
    except Exception as exc:  # noqa: BLE001 — sin finca propia se sigue con la de siempre
        log_event({
            "event": "dataris.graniot.farm_for_finca.failed",
            "operation": "resolve-farm-id",
            "finca": name,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        })
        return None
    farm_id = _clean_id(farm.get("id"))
    if farm_id:
        log_event({
            "event": "dataris.graniot.farm_for_finca.ok",
            "operation": "resolve-farm-id",
            "finca": name,
            "farm_id": farm_id,
        })
    return farm_id


async def _resolve_farm_id(
    client: GraniotClient,
    local: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    allow_global_default: bool = True,
) -> str:
    # Acepta camelCase y snake_case porque el frontend puede enviar farmId,
    # mientras que Graniot y el backend usan farm_id/graniot_farm_id.
    # Una elección explícita de quien llama manda sobre todo lo demás.
    for value in (
        payload.get("graniot_farm_id"),
        payload.get("farm_id"),
        payload.get("farmId"),
        payload.get("farmID"),
        payload.get("farm"),
    ):
        clean = _clean_id(value)
        if clean:
            return clean

    # La finca del lote define su sección en el portal. Va por delante del
    # `graniot_farm_id` ya guardado para que, al reorganizar los lotes en
    # Dataris, el portal del cliente se reordene con ellos.
    farm_for_finca = await _farm_id_for_finca(client, local.get("finca"))
    if farm_for_finca:
        return farm_for_finca

    candidates = [
        local.get("graniot_farm_id"),
        local.get("farm_id"),
    ]
    # GRANIOT_DEFAULT_FARM_ID belongs to the API key owner. While acting on
    # behalf of another Graniot account it would either fail or, worse, attach
    # the parcel to a farm the user cannot see in their portal.
    if allow_global_default:
        candidates.append(settings.GRANIOT_DEFAULT_FARM_ID)
    for value in candidates:
        clean = _clean_id(value)
        if clean:
            return clean

    try:
        farms_payload = await client.get("/api/farms/")
        farm_id = _select_farm_id(farms_payload)
        if farm_id:
            return farm_id
    except Exception:
        # If listing farms is not available for this key, try creating one below.
        pass

    return await _create_default_farm(client, name=None if allow_global_default else settings.GRANIOT_PARCEL_SYNC_FARM_NAME)


def _public_parcel(row: Dict[str, Any]) -> Dict[str, Any]:
    return dict(row)


@router.get("/status")
def status():
    # Public diagnostic endpoint: it never exposes the API key.
    # Useful for checking local/Cloud Run configuration directly from the browser.
    client = GraniotClient()
    return {
        "data": {
            "configured": client.is_configured,
            "base_url": client.base_url,
            "auth_header": client.auth_header,
            "auth_scheme": client.auth_scheme,
            "client_id_configured": bool(client.client_id),
            "default_farm_id_configured": bool(settings.GRANIOT_DEFAULT_FARM_ID),
            "debug_logs_enabled": bool(settings.GRANIOT_DEBUG_LOGS_ENABLED),
            "debug_log_file": str(get_log_file_path()),
        },
        "error": None,
    }


def _configured_embed_account() -> Optional[Dict[str, str]]:
    """Return the statically configured embed account, if any."""
    if not settings.GRANIOT_EMBED_URL:
        return None
    return _select_embed_account(
        [{
            "account_email": settings.GRANIOT_EMBED_ACCOUNT_EMAIL,
            "embedded_url": settings.GRANIOT_EMBED_URL,
        }],
        settings.GRANIOT_EMBED_ACCOUNT_EMAIL,
    )


def _embed_minting_configured() -> bool:
    """True when the backend can mint a fresh embed access token itself."""
    return bool(
        (settings.GRANIOT_EMBED_USERNAME and settings.GRANIOT_EMBED_PASSWORD)
        or settings.GRANIOT_EMBED_REFRESH_TOKEN
    )


def _build_embed_url(access_token: str) -> str:
    host = (settings.GRANIOT_EMBED_HOST or "embed.graniot.com").strip()
    return f"https://{host}/?{urlencode({'auth_id': access_token})}"


def _embed_token_looks_expired(access_token: str, *, skew_seconds: int = 30) -> bool:
    """Best-effort: decode a SimpleJWT access token and check it isn't expired."""
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return exp <= time_module.time() + skew_seconds
    except Exception:
        return False


async def _mint_embed_access_token() -> str:
    """Mint a fresh embed access token from the SimpleJWT auth host.

    ``embed.graniot.com`` authenticates the portal with a ~3h SimpleJWT *access*
    token passed as ``?auth_id=``. Graniot does not refresh the token exposed by
    ``/api/accounts/`` (it goes stale → iframe stuck on "loading"). So we mint a
    fresh one on demand: prefer the embed-account credentials (never expire),
    otherwise use a configured refresh token. Raises ``HTTPException(502)`` on
    failure so the caller surfaces a clean error instead of a stale token.

    Credentials/tokens are never included in error messages or logs.
    """
    host = (settings.GRANIOT_EMBED_HOST or "embed.graniot.com").strip()
    base = f"https://{host}"
    timeout = float(settings.GRANIOT_TIMEOUT_SECONDS or 30)

    if settings.GRANIOT_EMBED_USERNAME and settings.GRANIOT_EMBED_PASSWORD:
        endpoint = f"{base}/api/token/"
        payload = {
            "username": settings.GRANIOT_EMBED_USERNAME,
            "password": settings.GRANIOT_EMBED_PASSWORD,
        }
        mode = "login"
    else:
        endpoint = f"{base}/api/token/refresh/"
        payload = {"refresh": settings.GRANIOT_EMBED_REFRESH_TOKEN}
        mode = "refresh"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json=payload, headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo contactar al portal Graniot para renovar el acceso ({mode})",
        ) from exc

    if resp.status_code != 200:
        # Never echo the response body: it may reflect the submitted credentials.
        raise HTTPException(
            status_code=502,
            detail=f"Graniot rechazó la renovación del acceso al portal (HTTP {resp.status_code}, {mode})",
        )

    try:
        access = str((resp.json() or {}).get("access") or "").strip()
    except Exception:
        access = ""
    if not access or _embed_token_looks_expired(access):
        raise HTTPException(status_code=502, detail="Graniot no devolvió un token de portal válido")
    return access


_EMBED_ACCOUNTS_CACHE_KEY = "graniot:embed:accounts"


def _embed_url_auth_token(embedded_url: str) -> str:
    """Extract the ``auth_id`` token from a Graniot embedded URL."""
    try:
        values = parse_qs(urlparse(str(embedded_url or "")).query).get("auth_id") or []
        return str(values[0] or "").strip() if values else ""
    except Exception:
        return ""


def _embed_service_account_emails() -> set[str]:
    """Emails that identify the dedicated embed service account."""
    return {
        str(email).strip().lower()
        for email in (settings.GRANIOT_EMBED_ACCOUNT_EMAIL, settings.GRANIOT_EMBED_USERNAME)
        if email and str(email).strip()
    }


GRANIOT_ACCOUNTS_MAX_PAGES = int(os.getenv("GRANIOT_ACCOUNTS_MAX_PAGES", "20"))


async def _fetch_all_accounts(client: GraniotClient, *, operation: str) -> List[Dict[str, Any]]:
    """Read every Graniot account, following pagination when present.

    ``/api/accounts/`` may answer with a plain list or with a DRF paginated
    object (``{"count", "next", "results"}``). Reading only the first page would
    silently hide accounts, and a user whose account is not seen here is treated
    as "has no Graniot account" (their lots would never be uploaded).
    """
    accounts: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    path: Optional[str] = "/api/accounts/"

    for _ in range(max(1, GRANIOT_ACCOUNTS_MAX_PAGES)):
        if not path or path in seen_urls:
            break
        seen_urls.add(path)
        payload = await client.get(
            path,
            include_client_id=False,
            debug_context={"operation": operation, "page": len(seen_urls)},
        )
        accounts.extend(_items(payload))
        next_url = payload.get("next") if isinstance(payload, dict) else None
        path = str(next_url) if next_url else None

    # La misma cuenta puede repetirse entre páginas si Graniot reordena.
    deduped: Dict[str, Dict[str, Any]] = {}
    for account in accounts:
        key = str(account.get("id") or account.get("account_email") or len(deduped))
        deduped.setdefault(key, account)
    return list(deduped.values())


async def _fetch_embed_accounts(*, refresh: bool = False) -> Any:
    """Fetch (with a short cache) the Graniot accounts listing for embed matching."""
    cached = None if refresh else _cache_get(_EMBED_ACCOUNTS_CACHE_KEY)
    if cached is not None:
        return cached
    client = GraniotClient()
    accounts = await _fetch_all_accounts(client, operation="resolve-embed-url-per-user")
    return _cache_set(_EMBED_ACCOUNTS_CACHE_KEY, accounts, GRANIOT_EMBED_ACCOUNTS_CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Cuentas de mapa embebido por usuario
# ---------------------------------------------------------------------------
# Graniot separa a los **usuarios de plataforma** (los que entran en
# app.graniot.com y son dueños de las fincas) de los **usuarios de mapa
# embebido** (los únicos que devuelve /api/accounts/ y los únicos que
# embed.graniot.com acepta). No hay endpoint que liste a los primeros, así que
# emparejar por correo contra /api/accounts/ solo encontraba a las 3 cuentas de
# la sección API: cualquier otro cliente terminaba viendo el portal de la cuenta
# de servicio, con fincas que no son suyas.
#
# La vía que Graniot confirma es crear una cuenta embebida y asignarle las
# fincas del usuario de plataforma. Dataris lo hace solo:
#   1. /api/company/farms/ da el censo de dueños de finca (correo + id numérico).
#   2. Se da de alta la cuenta embebida (con un alias, porque Graniot rechaza
#      repetir el correo de un usuario de plataforma).
#   3. Se le asignan las fincas de esa persona y se guarda el vínculo.
_COMPANY_FARMS_CACHE_KEY = "graniot:company:farms"
EMBED_LINKS_TABLE = "graniot_embed_links"


async def _fetch_company_farms_index(*, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Censo ``correo -> {user_id numérico, nombre, fincas}`` de Graniot."""
    cached = None if refresh else _cache_get(_COMPANY_FARMS_CACHE_KEY)
    if cached is not None:
        return cached
    farms = await fetch_company_farms(GraniotClient(), operation="graniot-company-farms")
    index = index_platform_users(farms)
    return _cache_set(_COMPANY_FARMS_CACHE_KEY, index, GRANIOT_COMPANY_FARMS_CACHE_TTL_SECONDS)


async def _platform_user_for_email(email: str, *, refresh: bool = False) -> Optional[Dict[str, Any]]:
    """Usuario de plataforma de Graniot con ese correo (o None si no tiene fincas)."""
    normalized = str(email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return None
    index = await _fetch_company_farms_index(refresh=refresh)
    return index.get(normalized)


def _embed_links(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    return table(db, EMBED_LINKS_TABLE)


def _find_embed_link(db: Dict[str, Any], email: str) -> Optional[Dict[str, Any]]:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return next(
        (
            link
            for link in _embed_links(db)
            if str(link.get("user_email") or "").strip().lower() == normalized
        ),
        None,
    )


def _save_embed_link(user_email: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Crea o actualiza el vínculo usuario Dataris ↔ cuenta embebida de Graniot."""
    normalized = str(user_email or "").strip().lower()
    with LOCK:
        db = read_db()
        link = _find_embed_link(db, normalized)
        if not link:
            link = {
                "id": str(uuid.uuid4()),
                "user_email": normalized,
                "created_at": now(),
            }
            _embed_links(db).append(link)
        link.update(updates)
        link["updated_at"] = now()
        write_db(db)
        return dict(link)


def _delete_embed_link(user_email: str) -> Optional[Dict[str, Any]]:
    normalized = str(user_email or "").strip().lower()
    with LOCK:
        db = read_db()
        link = _find_embed_link(db, normalized)
        if not link:
            return None
        _embed_links(db).remove(link)
        write_db(db)
        return dict(link)


def _public_embed_link(link: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(link, dict):
        return None
    return {
        "id": link.get("id"),
        "user_email": link.get("user_email"),
        "account_id": link.get("account_id"),
        "account_email": link.get("account_email"),
        "platform_email": link.get("platform_email"),
        "platform_user_id": link.get("platform_user_id"),
        "farm_ids": link.get("farm_ids") or [],
        "farms_synced_at": link.get("farms_synced_at"),
        "provisioned_by": link.get("provisioned_by"),
        "created_at": link.get("created_at"),
        "updated_at": link.get("updated_at"),
        "last_error": link.get("last_error"),
    }


def _account_by_id(payload: Any, account_id: Any) -> Optional[Dict[str, Any]]:
    wanted = str(account_id or "").strip()
    if not wanted:
        return None
    return next((item for item in _items(payload) if str(item.get("id") or "").strip() == wanted), None)


# Cuántos alias se prueban antes de rendirse. Hace falta más de uno porque dar
# de baja una cuenta embebida en Graniot la deja desactivada y su correo
# inutilizable por API (solo se reactiva desde la aplicación de Graniot).
GRANIOT_EMBED_ALIAS_MAX_ATTEMPTS = int(os.getenv("GRANIOT_EMBED_ALIAS_MAX_ATTEMPTS", "4"))


def _embed_alias_for(platform_user: Dict[str, Any], email: str, *, attempt: int = 0) -> str:
    return embed_alias(
        email,
        (platform_user or {}).get("user_id"),
        settings.GRANIOT_EMBED_ALIAS_TEMPLATE or "dataris-embed+{uid}@dataris.es",
        attempt=attempt,
    )


async def _create_embed_account_for_user(
    email: str,
    platform_user: Dict[str, Any],
    *,
    operation: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Exception]]:
    """Da de alta la cuenta embebida, esquivando los alias ya ocupados.

    Un alias puede estar cogido por una cuenta viva (que entonces es la suya y se
    reutiliza) o por una desactivada, que Graniot no lista ni deja recrear. En
    ese segundo caso se prueba con el alias siguiente en vez de dejar al usuario
    sin portal.
    """
    client = GraniotClient()
    last_error: Optional[Exception] = None
    for attempt in range(max(1, GRANIOT_EMBED_ALIAS_MAX_ATTEMPTS)):
        alias = _embed_alias_for(platform_user, email, attempt=attempt)
        try:
            return await create_embed_account(client, alias, operation=operation), None
        except Exception as exc:  # noqa: BLE001 — se decide según el motivo
            last_error = exc
            if not alias_already_taken(exc):
                return None, exc
            existing = _raw_account_for_email(await _fetch_embed_accounts(refresh=True), alias)
            if existing:
                return existing, None
            log_event({
                "event": "dataris.graniot.embed_provision.alias_unavailable",
                "operation": operation,
                "email": email,
                "alias": alias,
                "message": str(exc)[:300],
            })
    return None, last_error


async def _find_embed_account(
    email: str,
    *,
    link: Optional[Dict[str, Any]] = None,
    platform_user: Optional[Dict[str, Any]] = None,
    refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Cuenta embebida de Graniot que corresponde a este usuario de Dataris.

    Se busca en tres pasos, del más fiable al más tolerante: el vínculo guardado,
    el correo tal cual (las cuentas que ya existían antes de todo esto) y el
    alias esperado, que lleva dentro el id del usuario de plataforma y permite
    reconstruir el vínculo aunque el registro local se haya perdido.
    """
    accounts = await _fetch_embed_accounts(refresh=refresh)
    normalized = str(email or "").strip().lower()

    candidates: List[Optional[Dict[str, Any]]] = [
        _account_by_id(accounts, (link or {}).get("account_id")),
        _raw_account_for_email(accounts, normalized),
        _raw_account_for_email(accounts, (link or {}).get("account_email")),
    ]
    if platform_user:
        candidates.extend(
            _raw_account_for_email(accounts, _embed_alias_for(platform_user, normalized, attempt=attempt))
            for attempt in range(max(1, GRANIOT_EMBED_ALIAS_MAX_ATTEMPTS))
        )
    return next((account for account in candidates if account), None)


async def _sync_embed_account_farms(
    account: Dict[str, Any],
    platform_user: Dict[str, Any],
    *,
    operation: str = "graniot-embed-farm-sync",
) -> Dict[str, Any]:
    """Asigna a la cuenta embebida las fincas del usuario de plataforma.

    Solo añade lo que falta: nunca retira vínculos que Graniot (o su equipo)
    hayan creado por su cuenta. Los fallos por finca se acumulan en ``errors`` en
    vez de abortar, para que una finca problemática no deje al usuario sin las
    demás.
    """
    account_id = str(account.get("id") or "").strip()
    client = GraniotClient()
    owner_client_id = _clean_id((platform_user or {}).get("user_id"))
    linked: List[Any] = []
    errors: List[Dict[str, Any]] = []

    for farm in (platform_user or {}).get("farms") or []:
        farm_id = farm.get("id")
        if farm_id is None:
            continue
        try:
            # El alta es idempotente y devuelve los gestores de la finca: se
            # comprueba en la respuesta que la cuenta quedó dentro, en vez de
            # dar por hecho que un 2xx significa que se asignó.
            managers = await link_farm_to_account(
                client,
                farm_id,
                account_id,
                owner_client_id=owner_client_id,
                operation=operation,
            )
            if not account_is_manager(managers, account_id):
                raise GraniotAPIError(502, "Graniot no incluyó la cuenta entre los gestores", managers)
            linked.append(farm_id)
        except Exception as exc:  # noqa: BLE001 — una finca no puede tumbar el resto
            errors.append({"farm_id": farm_id, "error": str(exc)[:300]})
            log_event({
                "event": "dataris.graniot.embed_provision.farm_link_failed",
                "operation": operation,
                "farm_id": farm_id,
                "account_id": account_id,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:300],
            })

    return {"linked": linked, "errors": errors}


async def _provision_embed_account(
    email: str,
    *,
    platform_user: Optional[Dict[str, Any]] = None,
    provisioned_by: Optional[str] = None,
    operation: str = "graniot-embed-provision",
) -> Dict[str, Any]:
    """Da de alta el portal embebido de un usuario y le asigna sus fincas.

    Devuelve siempre un diagnóstico (``reason`` explica por qué no se hizo nada).
    Nunca crea una cuenta para quien no tiene fincas en Graniot: sería un portal
    vacío, y el usuario está mejor servido por el fallback existente.
    """
    normalized = str(email or "").strip().lower()
    result: Dict[str, Any] = {
        "email": normalized,
        "provisioned": False,
        "account": None,
        "link": None,
        "farms": None,
        "reason": None,
    }
    if not normalized or "@" not in normalized:
        result["reason"] = "user_without_email"
        return result

    if platform_user is None:
        platform_user = await _platform_user_for_email(normalized)
    if not platform_user:
        result["reason"] = "no_platform_farms_for_email"
        return result

    db = read_db()
    link = _find_embed_link(db, normalized)
    account = await _find_embed_account(normalized, link=link, platform_user=platform_user)

    if account is None:
        account, create_error = await _create_embed_account_for_user(
            normalized, platform_user, operation=operation
        )
        if account is None:
            result["reason"] = "create_account_failed"
            result["error"] = str(create_error or "")[:300]
            _save_embed_link(normalized, {"last_error": result["error"]})
            return result
        _cache_delete_prefix(_EMBED_ACCOUNTS_CACHE_KEY)

    farms = await _sync_embed_account_farms(account, platform_user, operation=operation)
    result["farms"] = farms
    result["account"] = {
        "id": account.get("id"),
        "account_email": account.get("account_email"),
    }
    result["provisioned"] = True
    result["link"] = _public_embed_link(_save_embed_link(normalized, {
        "account_id": account.get("id"),
        "account_email": account.get("account_email"),
        "platform_email": platform_user.get("email"),
        "platform_user_id": platform_user.get("user_id"),
        "farm_ids": sorted(
            {str(farm.get("id")) for farm in platform_user.get("farms") or [] if farm.get("id") is not None}
        ),
        "farms_synced_at": now() if not farms.get("errors") else None,
        "provisioned_by": provisioned_by or "auto",
        "last_error": (farms.get("errors") or [{}])[0].get("error") if farms.get("errors") else None,
    }))
    # La cuenta acaba de nacer o de cambiar de fincas: el listado cacheado ya no
    # la refleja y el portal debe servirse con datos frescos.
    _cache_delete_prefix(_EMBED_ACCOUNTS_CACHE_KEY)
    log_event({
        "event": "dataris.graniot.embed_provision.done",
        "operation": operation,
        "email": normalized,
        "account_id": account.get("id"),
        "farms_linked": len(farms.get("linked") or []),
        "farms_failed": len(farms.get("errors") or []),
    })
    return result


def _ensure_embed_link(
    email: str,
    account: Dict[str, Any],
    platform_user: Optional[Dict[str, Any]],
    link: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Deja registrado qué cuenta embebida sirve a este usuario.

    Cubre las cuentas que ya existían antes de todo esto (dadas de alta a mano
    por Graniot): a partir de ahora se resuelven por vínculo, sin depender de que
    el correo siga coincidiendo. Solo escribe cuando algo cambia.
    """
    account_id = str(account.get("id") or "").strip()
    if not account_id or (link and str(link.get("account_id") or "") == account_id):
        return link
    return _save_embed_link(email, {
        "account_id": account_id,
        "account_email": account.get("account_email"),
        "platform_email": (platform_user or {}).get("email"),
        "platform_user_id": (platform_user or {}).get("user_id"),
        # Sus fincas se revisan en la primera reconciliación en segundo plano.
        "farms_synced_at": None,
        "provisioned_by": (link or {}).get("provisioned_by") or "discovered",
    })


def _mark_embed_farms_pending(email: str, farm_id: Any) -> None:
    """Marca el portal de un usuario para reconciliar cuando aparece una finca.

    Subir un lote puede crear una finca nueva en la cuenta de Graniot de esa
    persona. Su portal embebido solo la mostrará cuando esa finca se le asigne,
    así que se invalida el censo y se deja el vínculo pendiente: la próxima
    apertura del mapa la reconcilia en segundo plano.
    """
    normalized = str(email or "").strip().lower()
    clean_farm = _clean_id(farm_id)
    if not normalized or not clean_farm:
        return
    with LOCK:
        db = read_db()
        link = _find_embed_link(db, normalized)
        if not link or clean_farm in {str(value) for value in link.get("farm_ids") or []}:
            return
    _cache_delete_prefix(_COMPANY_FARMS_CACHE_KEY)
    _save_embed_link(normalized, {"farms_synced_at": None})


def _embed_farm_sync_is_stale(link: Optional[Dict[str, Any]]) -> bool:
    """¿Toca revisar si al usuario le han añadido fincas en Graniot?"""
    ttl = int(settings.GRANIOT_EMBED_FARM_SYNC_TTL_SECONDS or 0)
    if ttl <= 0 or not isinstance(link, dict):
        return False
    synced_at = link.get("farms_synced_at")
    if not synced_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(synced_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() >= ttl


# Dos pestañas abiertas a la vez no deben dar de alta dos cuentas para la misma
# persona. La clave incluye el bucle de eventos porque cada worker tiene el suyo.
_EMBED_PROVISION_LOCKS: Dict[Tuple[int, str], asyncio.Lock] = {}


def _embed_provision_lock(email: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), str(email or "").strip().lower())
    lock = _EMBED_PROVISION_LOCKS.get(key)
    if lock is None:
        if len(_EMBED_PROVISION_LOCKS) > 500:
            _EMBED_PROVISION_LOCKS.clear()
        lock = _EMBED_PROVISION_LOCKS[key] = asyncio.Lock()
    return lock


async def _provision_embed_account_guarded(
    email: str,
    *,
    platform_user: Optional[Dict[str, Any]] = None,
    provisioned_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Aprovisiona sin que dos peticiones simultáneas dupliquen la cuenta."""
    async with _embed_provision_lock(email):
        # Otra petición pudo terminar el alta mientras se esperaba el turno.
        link = _find_embed_link(read_db(), email)
        if link and link.get("account_id") and not _embed_farm_sync_is_stale(link):
            return {"email": email, "provisioned": True, "reason": "already_provisioned", "link": _public_embed_link(link)}
        return await _provision_embed_account(
            email, platform_user=platform_user, provisioned_by=provisioned_by
        )


async def _refresh_embed_farms_in_background(email: str) -> None:
    """Reconcilia las fincas del portal sin hacer esperar a quien abre el mapa."""
    try:
        async with _embed_provision_lock(email):
            await _provision_embed_account(email, provisioned_by="auto-refresh")
    except Exception as exc:  # noqa: BLE001 — es mantenimiento, nunca rompe nada
        log_event({
            "event": "dataris.graniot.embed_provision.refresh_failed",
            "operation": "graniot-embed-provision",
            "email": email,
            "exception_type": type(exc).__name__,
            "message": str(exc)[:300],
        })


async def _embed_account_for_user(user: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return the personal Graniot portal for the authenticated Dataris user.

    Order of preference: the account linked to this user (or matching their
    email, or the expected alias), and if there is none, an account provisioned
    on the spot with the farms of their Graniot platform user. Returning ``None``
    hands the request back to the dedicated service account, whose token IS
    minted fresh on demand. This function never raises: neither the per-user
    match nor the provisioning may break the fallback.
    """
    if not settings.GRANIOT_EMBED_PER_USER_ENABLED:
        return None
    email = str((user or {}).get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    if email in _embed_service_account_emails():
        # The service account is better served by the minted token (always fresh).
        return None

    try:
        db = read_db()
        link = _find_embed_link(db, email)
        platform_user = await _platform_user_for_email(email)
        account = await _find_embed_account(email, link=link, platform_user=platform_user)

        if account is None and platform_user and settings.GRANIOT_EMBED_AUTOPROVISION_ENABLED:
            # El alta puede necesitar varias llamadas a Graniot (una por finca).
            # Se le da un presupuesto de tiempo: si se agota, el trabajo sigue en
            # segundo plano y esta carga usa el portal de siempre, para que el
            # mapa no se quede esperando.
            task = asyncio.create_task(
                _provision_embed_account_guarded(email, platform_user=platform_user)
            )
            try:
                provisioned = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=float(settings.GRANIOT_EMBED_PROVISION_TIMEOUT_SECONDS or 12),
                )
            except asyncio.TimeoutError:
                log_event({
                    "event": "dataris.graniot.embed_provision.still_running",
                    "operation": "resolve-embed-url-per-user",
                    "email": email,
                })
                return None
            if provisioned.get("provisioned"):
                account = await _find_embed_account(
                    email,
                    link=_find_embed_link(read_db(), email),
                    platform_user=platform_user,
                    refresh=True,
                )
        if account is None:
            return None

        link = _ensure_embed_link(email, account, platform_user, link)
        public = _public_embed_account(account, email)
    except HTTPException as exc:
        if exc.status_code != 404:
            # 404 = the user simply has no Graniot account (expected, no noise).
            log_event({
                "event": "dataris.graniot.embed_per_user.invalid_account",
                "operation": "resolve-embed-url-per-user",
                "email": email,
                "status_code": exc.status_code,
            })
        return None
    except Exception as exc:  # noqa: BLE001 — the fallback must always survive
        log_event({
            "event": "dataris.graniot.embed_per_user.lookup_failed",
            "operation": "resolve-embed-url-per-user",
            "email": email,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        })
        return None

    token = _embed_url_auth_token(public.get("embedded_url") or "")
    if not token or _embed_token_looks_expired(token):
        # Graniot renueva el auth_id en cada lectura de /api/accounts/, así que
        # un token vencido casi siempre viene de nuestra caché: se relee antes de
        # rendirse, porque servirlo dejaría el iframe girando para siempre.
        try:
            account = await _find_embed_account(
                email, link=_find_embed_link(read_db(), email), platform_user=platform_user, refresh=True
            )
            public = _public_embed_account(account, email) if account else None
            token = _embed_url_auth_token((public or {}).get("embedded_url") or "")
        except Exception:  # noqa: BLE001 — cae al portal de servicio
            public, token = None, ""
        if not public or not token or _embed_token_looks_expired(token):
            log_event({
                "event": "dataris.graniot.embed_per_user.token_expired",
                "operation": "resolve-embed-url-per-user",
                "email": email,
            })
            return None

    # Si a esta persona le han añadido fincas en Graniot después del alta, su
    # portal no las vería. Se reconcilia en segundo plano: el mapa se abre ya.
    if (
        platform_user
        and _embed_farm_sync_is_stale(link)
        and settings.GRANIOT_EMBED_AUTOPROVISION_ENABLED
    ):
        asyncio.create_task(_refresh_embed_farms_in_background(email))

    return public


# ---------------------------------------------------------------------------
# Parcels inside each user's own Graniot account
# ---------------------------------------------------------------------------
# The Satélite module embeds the Graniot portal *of the authenticated user*
# (matched by email against /api/accounts/). Lots must therefore be created in
# that same account, otherwise the user would never see them in their portal.
#
# Graniot documents farm/parcel endpoints as "Provides get_effective_user() for
# views where a privileged user can act on behalf of another user via
# `client_id` parameter", and /api/accounts/ exposes `account_access` (the
# account's own token). Dataris uses the account token when available and the
# privileged `client_id` otherwise, so parcel upload/removal no longer depends
# on Graniot's commercial team.

SYNC_MODE_TOKEN = "token"
SYNC_MODE_CLIENT_ID = "client_id"
SYNC_MODE_SERVICE = "service"


def _raw_account_for_email(payload: Any, email: str) -> Optional[Dict[str, Any]]:
    """Return the full Graniot account object (including private fields)."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return next(
        (
            item
            for item in _items(payload)
            if str(item.get("account_email") or "").strip().lower() == normalized
        ),
        None,
    )


def _account_id_value(account: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(account, dict):
        return None
    return _clean_id(account.get("id") or account.get("account_id") or account.get("user_id"))


def _jwt_payload(token: Any) -> Dict[str, Any]:
    """Decode the public payload of a SimpleJWT token (no signature check)."""
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("utf-8")))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _account_client_id(account: Optional[Dict[str, Any]]) -> Optional[str]:
    """Value for Graniot's privileged ``client_id`` parameter.

    ``client_id`` identifies the *user* Graniot must act as by its numeric id,
    which travels in the account/embed SimpleJWT under the ``id`` claim.

    Verified against the live API: numeric ids work (1528 → that account's own
    farm, 1471 → its 5 farms), while the ``acc-<uuid>`` ids from
    ``/api/accounts/``, an account email or an unknown id all answer HTTP 500.
    So without a numeric id there is no usable client_id and the account token
    stays as the only way in.
    """
    if not isinstance(account, dict):
        return None
    tokens = [
        account.get("account_access"),
        _embed_url_auth_token(account.get("embedded_url") or ""),
    ]
    for token in tokens:
        payload = _jwt_payload(token)
        user_id = _clean_id(payload.get("id") or payload.get("user_id") or payload.get("uid"))
        if user_id and user_id.isdigit():
            return user_id
    return None


def _account_access_token(account: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(account, dict):
        return None
    token = str(account.get("account_access") or "").strip()
    if not token or _embed_token_looks_expired(token):
        return None
    return token


def _public_sync_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """Target description safe to return to the browser (never tokens)."""
    return {
        "mode": target.get("mode"),
        "user_email": target.get("user_email"),
        "account_email": target.get("account_email"),
        "account_id": target.get("account_id"),
        "client_id": target.get("client_id"),
        "platform_user_id": target.get("platform_user_id"),
        "has_account_token": bool(target.get("access_token")),
        "reason": target.get("reason"),
    }


async def _resolve_sync_target(
    email: Optional[str],
    *,
    refresh: bool = False,
    operation: str = "resolve-parcel-sync-target",
) -> Dict[str, Any]:
    """Resolve which Graniot account owns the parcels of this Dataris email.

    Never raises: when the account cannot be resolved the caller decides whether
    to fall back to the API key owner's account (manual sync) or to skip the
    operation entirely (automatic sync).
    """
    normalized = str(email or "").strip().lower()
    target: Dict[str, Any] = {
        "mode": SYNC_MODE_SERVICE,
        "user_email": normalized or None,
        "account_email": None,
        "account_id": None,
        "client_id": None,
        "platform_user_id": None,
        "access_token": None,
        "reason": None,
    }

    if not settings.GRANIOT_PARCEL_SYNC_PER_USER_ENABLED:
        target["reason"] = "per_user_disabled"
        return target
    if not normalized or "@" not in normalized:
        target["reason"] = "user_without_email"
        return target

    # El censo de dueños de finca es un extra: si Graniot no responde, todavía se
    # puede resolver la cuenta embebida por vínculo o por correo.
    platform_user: Optional[Dict[str, Any]] = None
    try:
        platform_user = await _platform_user_for_email(normalized, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        log_event({
            "event": "dataris.graniot.parcel_sync.platform_census_failed",
            "operation": operation,
            "email": normalized,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        })
    target["platform_user_id"] = _clean_id((platform_user or {}).get("user_id"))

    try:
        link = _find_embed_link(read_db(), normalized)
        account = await _find_embed_account(
            normalized, link=link, platform_user=platform_user, refresh=refresh
        )
    except Exception as exc:  # noqa: BLE001 — resolution must never break the caller
        log_event({
            "event": "dataris.graniot.parcel_sync.accounts_lookup_failed",
            "operation": operation,
            "email": normalized,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        })
        target["reason"] = "accounts_lookup_failed"
        return target

    mode = str(settings.GRANIOT_PARCEL_SYNC_MODE or "auto").strip().lower()

    if not account:
        # Sin cuenta embebida todavía, pero Graniot sí acepta actuar en nombre
        # del usuario de plataforma por su id numérico: los lotes se crean en la
        # cuenta correcta, y el portal embebido los verá en cuanto se aprovisione.
        if target["platform_user_id"] and mode in {"auto", SYNC_MODE_CLIENT_ID}:
            target["mode"] = SYNC_MODE_CLIENT_ID
            target["account_email"] = str((platform_user or {}).get("email") or normalized)
            target["client_id"] = target["platform_user_id"]
            target["reason"] = "platform_user"
            return target
        target["reason"] = "no_graniot_account_for_email"
        return target

    target["account_email"] = str(account.get("account_email") or normalized)
    target["account_id"] = _account_id_value(account)
    target["client_id"] = _account_client_id(account)
    token = _account_access_token(account)

    if mode in {"auto", SYNC_MODE_TOKEN} and token:
        target["mode"] = SYNC_MODE_TOKEN
        target["access_token"] = token
        return target
    if mode in {"auto", SYNC_MODE_CLIENT_ID} and target["client_id"]:
        target["mode"] = SYNC_MODE_CLIENT_ID
        return target

    target["reason"] = "token_unavailable" if mode == SYNC_MODE_TOKEN else "account_without_id"
    return target


async def _sync_target_for_row(
    user: Dict[str, Any],
    local: Optional[Dict[str, Any]] = None,
    *,
    refresh: bool = False,
    operation: str = "resolve-parcel-sync-target",
) -> Dict[str, Any]:
    """Resolve the target account for a local lot.

    A lot already synced remembers the Graniot account that owns it
    (``graniot_account_email``). Reusing it keeps updates and deletions pointing
    at the same account even if the Dataris user later changes their email.
    """
    stored_email = str((local or {}).get("graniot_account_email") or "").strip().lower()
    user_email = str((user or {}).get("email") or "").strip().lower()
    if stored_email and stored_email != user_email:
        target = await _resolve_sync_target(stored_email, refresh=refresh, operation=operation)
        if target.get("mode") != SYNC_MODE_SERVICE:
            return target
    return await _resolve_sync_target(user_email, refresh=refresh, operation=operation)


def _client_for_target(target: Dict[str, Any]) -> GraniotClient:
    mode = target.get("mode")
    if mode == SYNC_MODE_TOKEN:
        return GraniotClient(access_token=target.get("access_token"))
    if mode == SYNC_MODE_CLIENT_ID:
        return GraniotClient(client_id=target.get("client_id") or target.get("account_id"))
    return GraniotClient()


@router.get("/embed")
async def get_embed_url(
    response: Response,
    authorization: Optional[str] = Header(default=None),
):
    """Resolve the current dedicated embed URL without exposing API credentials.

    The Graniot embed link carries a time-limited ``auth_id``; serving a stale
    one leaves the embedded portal stuck on "loading" (``/api/accounts/`` does
    not refresh it). Order of preference (``Cache-Control: no-store``):

    1. **The user's own portal**: the embedded account linked to them, matching
       their email or the expected alias. If they own farms in Graniot as a
       platform user and have no embedded account yet, one is provisioned with
       those farms (Graniot keeps both registries apart and offers no other way).
    2. If embed credentials/refresh token are configured, **mint a fresh access
       token** on demand for the service account — always a valid ``auth_id``.
    3. Otherwise, fall back to the URL Graniot exposes via ``/api/accounts/``
       (may already be expired), then to a statically configured URL.

    ``source`` tells the browser whether it is looking at the user's own farms
    (``personal``) or at the shared demo portal (``service``).
    """
    user = _require_user(authorization)
    response.headers["Cache-Control"] = "no-store"

    personal = await _embed_account_for_user(user)
    if personal is not None:
        return {"data": {**personal, "source": "personal"}, "error": None}

    if _embed_minting_configured():
        access = await _mint_embed_access_token()
        account = _select_embed_account(
            [{
                "account_email": settings.GRANIOT_EMBED_ACCOUNT_EMAIL,
                "embedded_url": _build_embed_url(access),
            }],
            settings.GRANIOT_EMBED_ACCOUNT_EMAIL,
        )
        return {"data": {**account, "source": "service"}, "error": None}

    live_error: Optional[Exception] = None
    try:
        client = GraniotClient()
        raw = await client.get(
            "/api/accounts/",
            include_client_id=False,
            debug_context={"operation": "resolve-embed-url"},
        )
        account = _select_embed_account(raw, settings.GRANIOT_EMBED_ACCOUNT_EMAIL)
        return {"data": {**account, "source": "service"}, "error": None}
    except Exception as exc:  # noqa: BLE001 — fall back to the configured URL below
        live_error = exc

    fallback = _configured_embed_account()
    if fallback is not None:
        return {"data": {**fallback, "source": "service"}, "error": None}

    if isinstance(live_error, HTTPException):
        raise live_error
    _raise_graniot_error(live_error)


@router.get("/debug/logs")
def get_debug_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    request_id: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    return {
        "data": {
            "file": str(get_log_file_path()),
            "logs": read_logs(limit=limit, request_id=request_id),
        },
        "error": None,
    }


@router.delete("/debug/logs")
def delete_debug_logs(authorization: Optional[str] = Header(default=None)):
    _require_user(authorization)
    clear_logs()
    return {"data": {"file": str(get_log_file_path()), "cleared": True}, "error": None}


@router.get("/layers")
async def list_layers(
    resolution_id: Optional[int] = Query(default=None),
    resolution_name: Optional[str] = Query(default=None),
    platform: bool = Query(default=True),
    refresh: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    cache_key = _stable_hash({
        "scope": "graniot-layers",
        "resolution_id": resolution_id,
        "resolution_name": resolution_name,
        "platform": platform,
    })
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    client = GraniotClient()
    try:
        # Confirmed Graniot catalog behavior:
        # - /api/layers/layers-platform/ returns UUID layer.key, resolution and stats metadata.
        # - /api/layers/get_wms_layers/ returns WMS layer names such as NDVI.
        # Dataris must combine both: key for statistics/json-index, name for WMS.
        wms_raw = None
        wms_names: set[str] = set()
        try:
            wms_raw = await client.get("/api/layers/get_wms_layers/")
            for item in _layer_items(wms_raw):
                name = item.get("name") or item.get("layer") or item.get("layers") or item.get("key")
                if name not in (None, ""):
                    wms_names.add(str(name).strip())
        except Exception as exc:
            log_event({
                "event": "dataris.graniot.layers.wms_catalog_failed",
                "operation": "list-layers",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            })

        attempts = [
            ("/api/layers/layers-platform/", {}),
            ("/api/layers/", {"resolution_id": resolution_id, "resolution_name": resolution_name}),
        ]
        if not platform:
            attempts = [("/api/layers/", {"resolution_id": resolution_id, "resolution_name": resolution_name})]

        raw = None
        last_error: Optional[Exception] = None
        for path, params in attempts:
            try:
                raw = await client.get(path, params=params)
                items = _layer_items(raw)
                if items:
                    layers = [_normalize_layer(item, wms_names=wms_names) for item in items]
                    # When WMS names are known, keep only renderable layers for the
                    # map selector. If the WMS catalog is unavailable, return the API catalog.
                    if wms_names:
                        layers = [layer for layer in layers if str(layer.get("wms_layer") or "").strip() in wms_names]

                    def _priority(value: Any) -> int:
                        try:
                            return 999999 if value is None else int(value)
                        except Exception:
                            return 999999

                    layers.sort(key=lambda x: (
                        _priority(x.get("menu_priority")),
                        str(x.get("resolution_label") or x.get("layer_resolution") or ""),
                        str(x.get("displayed_name") or ""),
                        str(x.get("name") or ""),
                    ))
                    return _cache_set(cache_key, {
                        "data": layers,
                        "raw": raw,
                        "wms_raw": wms_raw,
                        "error": None,
                        "count": len(layers),
                        "source_path": path,
                        "wms_layer_names": sorted(wms_names),
                        "cache": {"status": "MISS", "ttl_seconds": GRANIOT_CATALOG_CACHE_TTL_SECONDS},
                    }, GRANIOT_CATALOG_CACHE_TTL_SECONDS)
            except Exception as exc:
                last_error = exc
                continue

        # Last-resort WMS-only response. These can render as images but will not
        # provide statistics/json-index UUIDs. Prefer this over demo layers.
        if wms_raw is not None:
            wms_layers = [_normalize_layer(item, wms_names=wms_names) for item in _layer_items(wms_raw)]
            return _cache_set(cache_key, {
                "data": wms_layers,
                "raw": wms_raw,
                "error": None,
                "count": len(wms_layers),
                "source_path": "/api/layers/get_wms_layers/",
                "wms_layer_names": sorted(wms_names),
                "cache": {"status": "MISS", "ttl_seconds": GRANIOT_CATALOG_CACHE_TTL_SECONDS},
            }, GRANIOT_CATALOG_CACHE_TTL_SECONDS)

        if last_error:
            raise last_error
        return _cache_set(cache_key, {"data": [], "raw": raw, "error": None, "count": 0, "cache": {"status": "MISS", "ttl_seconds": GRANIOT_CATALOG_CACHE_TTL_SECONDS}}, max(60, GRANIOT_CATALOG_CACHE_TTL_SECONDS // 24))
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/layers/resolutions")
async def list_resolutions(
    search: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    cache_key = _stable_hash({"scope": "graniot-resolutions", "search": search})
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    client = GraniotClient()
    try:
        raw = await client.get("/api/layersresolution/", params={"search": search})
        resolutions = [_normalize_resolution(item) for item in _items(raw)]
        return _cache_set(cache_key, {"data": resolutions, "raw": raw, "error": None, "count": len(resolutions), "cache": {"status": "MISS", "ttl_seconds": GRANIOT_CATALOG_CACHE_TTL_SECONDS}}, GRANIOT_CATALOG_CACHE_TTL_SECONDS)
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/farms")
async def list_farms(authorization: Optional[str] = Header(default=None)):
    _require_user(authorization)
    client = GraniotClient()
    try:
        raw = await client.get("/api/farms/")
        farms = [_normalize_farm(item) for item in _items(raw)]
        return {"data": farms, "raw": raw, "error": None, "count": len(farms)}
    except Exception as exc:
        _raise_graniot_error(exc)


@router.post("/farms")
async def create_farm(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la finca es requerido")

    farm_type = str(payload.get("type") or settings.GRANIOT_DEFAULT_FARM_TYPE or "PRO").strip().upper()
    if farm_type not in {"PRO", "ARR", "EST"}:
        raise HTTPException(status_code=400, detail="Tipo de finca inválido. Usa PRO, ARR o EST")

    client = GraniotClient()
    is_active = bool(payload.get("is_active", True))

    try:
        farm = await _create_farm_on_graniot(client, name, farm_type, is_active)
        return {"data": farm, "raw": farm.get("raw") or farm, "error": None}
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/parcels")
async def list_graniot_parcels(authorization: Optional[str] = Header(default=None)):
    _require_user(authorization)
    client = GraniotClient()
    try:
        raw = await client.get("/api/parcels/")
        return {"data": _items(raw), "raw": raw, "error": None}
    except Exception as exc:
        _raise_graniot_error(exc)


async def sync_local_parcel_to_graniot(
    user: Dict[str, Any],
    parcel_id: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    require_account: bool = False,
    prefer_update: bool = False,
) -> Dict[str, Any]:
    """Create/update the Graniot parcels of a local Dataris lot.

    The parcel is created inside the Graniot account that belongs to the same
    user (matched by email in ``/api/accounts/``), so it shows up in the portal
    embedded in the Satélite module. Users without a Graniot account keep the
    legacy behaviour (the API key owner's account) unless
    ``GRANIOT_PARCEL_SYNC_REQUIRE_ACCOUNT`` is enabled.
    """
    payload = dict(payload or {})

    with LOCK:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
    if not local:
        raise HTTPException(status_code=404, detail="Lote local no encontrado")

    target = await _sync_target_for_row(user, local, operation="sync-local-parcel")
    if target.get("mode") == SYNC_MODE_SERVICE and (require_account or settings.GRANIOT_PARCEL_SYNC_REQUIRE_ACCOUNT):
        # Automatic syncs must not push lots into the API key owner's account:
        # the user would never see them in their own embedded portal.
        raise HTTPException(
            status_code=409,
            detail=(
                "Este usuario no tiene una cuenta de Graniot con su mismo correo, "
                f"así que el lote no se puede subir a su portal ({target.get('reason')})."
            ),
        )
    client = _client_for_target(target)

    metadata = {
        "source": "dataris",
        "dataris_parcel_id": local.get("id"),
        "area_ha": local.get("area"),
        **(payload.get("metadata") or {}),
    }
    feature_collection = _feature_collection_from_geometry(local.get("geometry"), local.get("id"), local.get("name") or "Lote", metadata)
    main_geometry = _main_geometry(feature_collection)

    name = payload.get("name") or local.get("name") or "Lote DATARIS"

    log_event({
        "event": "dataris.sync_local_parcel.start",
        "operation": "sync-local-parcel",
        "local_parcel_id": parcel_id,
        "local_parcel_name": name,
        "incoming_payload": safe_payload(payload),
        "target": _public_sync_target(target),
        "local_existing_graniot": {
            "graniot_farm_id": local.get("graniot_farm_id"),
            "graniot_parcel_id": local.get("graniot_parcel_id"),
            "graniot_parcel_key": local.get("graniot_parcel_key"),
            "has_access_key": bool(local.get("graniot_access_key")),
        },
        "feature_count": len(feature_collection.get("features") or []),
        "main_geometry_type": main_geometry.get("type"),
    })

    acts_on_behalf = target.get("mode") != SYNC_MODE_SERVICE
    try:
        farm_id = await _resolve_farm_id(client, local, payload, allow_global_default=not acts_on_behalf)

        farm_ref = _as_int_if_numeric(farm_id)

        log_event({
            "event": "dataris.sync_local_parcel.farm_resolved",
            "operation": "sync-local-parcel",
            "local_parcel_id": parcel_id,
            "farm_id": farm_id,
            "farm_ref": farm_ref,
            "farm_ref_type": type(farm_ref).__name__,
        })

        graniot_payload = _build_graniot_parcels_payload(feature_collection, farm_ref, name, metadata)

        log_event({
            "event": "dataris.sync_local_parcel.payload_built",
            "operation": "sync-local-parcel",
            "local_parcel_id": parcel_id,
            "farm_id": farm_id,
            "farm_ref": farm_ref,
            "parcel_count": len(graniot_payload.get("parcels") or []),
            "payload_shape": "farm_object_with_parcels_array_and_geom_feature",
            "payload": safe_payload(graniot_payload),
        })

        # A lot that already exists in Graniot must be updated, never posted
        # again: re-uploading the same file updates the local row in place and a
        # second POST would leave duplicated parcels in the user's portal.
        remote_ids = _remote_parcel_ids(local)
        parcels_to_send = graniot_payload.get("parcels") or []
        wants_update = bool(remote_ids) and bool(payload.get("force_update") or prefer_update)

        if wants_update and len(remote_ids) != len(parcels_to_send):
            # El lote pasó a tener otro número de polígonos: actualizar uno por
            # uno dejaría parcelas de sobra o de menos en el portal, así que se
            # borra lo anterior y se vuelve a crear.
            log_event({
                "event": "dataris.sync_local_parcel.recreating",
                "operation": "sync-local-parcel",
                "local_parcel_id": parcel_id,
                "remote_ids": remote_ids,
                "new_parcel_count": len(parcels_to_send),
            })
            await delete_parcel_from_graniot(user, local, clear_local=False)
            remote_ids = []
            wants_update = False

        if wants_update:
            features: List[Dict[str, Any]] = []
            for remote_id, parcel in zip(remote_ids, parcels_to_send):
                patch_payload = _build_graniot_parcel_patch_payload(parcel, remote_id)
                patched = await client.patch(
                    f"/api/parcels/{remote_id}/",
                    json_body=patch_payload,
                    params=None,
                    debug_context={
                        "operation": "sync-local-parcel",
                        "attempt": "update-confirmed-parcelgeojson-payload",
                        "local_parcel_id": parcel_id,
                        "graniot_parcel_id": remote_id,
                        "farm_id": farm_id,
                    },
                )
                last_payload_error = _payload_error_message(patched)
                if last_payload_error:
                    raise GraniotAPIError(400, last_payload_error, patched)
                for item in _items(patched) or [{}]:
                    features.append(item if isinstance(item, dict) else {})
            raw = {"type": "FeatureCollection", "features": features}
        else:
            # Graniot support confirmed this endpoint expects exactly:
            # farm as an object, parcels as an array, and each parcel geometry
            # under the `geom` key as a GeoJSON Feature.
            raw = await client.post(
                "/api/parcels/",
                json_body=graniot_payload,
                params=None,
                debug_context={
                    "operation": "sync-local-parcel",
                    "attempt": "confirmed-farm-object-parcels-array-geom",
                    "local_parcel_id": parcel_id,
                    "farm_id": farm_id,
                    "farm_ref": farm_ref,
                    "parcel_count": len(graniot_payload.get("parcels") or []),
                },
            )
            last_payload_error = _payload_error_message(raw)
            if last_payload_error:
                raise GraniotAPIError(400, last_payload_error, raw)

        ids = _extract_graniot_ids(raw)
        graniot_subparcels = _public_graniot_subparcels(raw)
        if wants_update:
            # Una actualización puede responder sin repetir los identificadores:
            # el lote sigue siendo el mismo, así que se conservan los que había.
            ids = {
                key: (value if value not in (None, "") else local.get(key))
                for key, value in ids.items()
            }
            if not graniot_subparcels:
                graniot_subparcels = local.get("graniot_parcels") or []
        log_event({
            "event": "dataris.sync_local_parcel.graniot_raw_success",
            "operation": "sync-local-parcel",
            "local_parcel_id": parcel_id,
            "farm_id": farm_id,
            "ids": ids,
            "subparcel_count": len(graniot_subparcels),
            "raw": safe_payload(raw),
        })
        if not ids.get("graniot_parcel_id") and not ids.get("graniot_access_key") and not ids.get("graniot_parcel_key"):
            raise GraniotAPIError(400, "Graniot respondió sin id/key del lote. No se marcará como sincronizado.", raw)

        t = now()
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
            if not row:
                raise HTTPException(status_code=404, detail="Lote local no encontrado")
            row.update({
                **{k: v for k, v in ids.items() if v is not None},
                "graniot_farm_id": farm_id,
                "graniot_parcels": graniot_subparcels,
                "graniot_synced_at": t,
                "graniot_sync_error": None,
                "graniot_raw": raw,
                # Remember which Graniot account owns these parcels so updates
                # and deletions always target the same account.
                "graniot_account_email": target.get("account_email"),
                "graniot_account_id": target.get("account_id"),
                "graniot_sync_mode": target.get("mode"),
                "updated_at": t,
            })
            write_db(db)
            result = _public_parcel(row)
        _mark_embed_farms_pending(target.get("user_email") or user.get("email"), farm_id)
        return {
            "parcel": result,
            "graniot": raw,
            "farm_id": farm_id,
            "target": _public_sync_target(target),
        }
    except Exception as exc:
        error_message = str(exc)
        log_event({
            "event": "dataris.sync_local_parcel.exception",
            "operation": "sync-local-parcel",
            "local_parcel_id": parcel_id,
            "exception_type": type(exc).__name__,
            "error_message": error_message,
            "payload": safe_payload(getattr(exc, "payload", None)),
        })
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
            if row:
                row["graniot_sync_error"] = error_message
                row["updated_at"] = now()
                write_db(db)
        _raise_graniot_error(exc)


@router.post("/parcels/sync-local/{parcel_id}")
async def sync_local_parcel(
    parcel_id: str,
    user_id: Optional[str] = Query(default=None, description="Dueño del lote (solo para gestores de lotes)"),
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = _acting_user(authorization, user_id or (payload or {}).get("user_id"))
    data = await sync_local_parcel_to_graniot(user, parcel_id, payload)
    return {"data": data, "error": None}


# Local fields that only make sense while the lot exists in Graniot.
GRANIOT_LOCAL_SYNC_FIELDS = (
    "graniot_parcel_id",
    "graniot_parcel_key",
    "graniot_access_key",
    "graniot_wms_access_key",
    "graniot_wms_url",
    "graniot_image_url",
    "graniot_bbox",
    "graniot_geometry",
    "graniot_parcels",
    "graniot_raw",
    "graniot_farm_id",
    "graniot_synced_at",
    "graniot_sync_error",
    "graniot_account_email",
    "graniot_account_id",
    "graniot_sync_mode",
)


def _remote_parcel_ids(local: Dict[str, Any]) -> List[str]:
    """Graniot parcel ids created for one local Dataris lot.

    A single lot can become several Graniot parcels (one per polygon), so the
    subparcels stored in ``graniot_parcels`` must be deleted too.
    """
    ids: List[str] = []

    def push(value: Any) -> None:
        clean = _clean_id(value)
        if clean and clean not in ids:
            ids.append(clean)

    push((local or {}).get("graniot_parcel_id"))
    subparcels = (local or {}).get("graniot_parcels")
    if isinstance(subparcels, list):
        for item in subparcels:
            if isinstance(item, dict):
                push(item.get("graniot_parcel_id"))
    if not ids:
        # Older rows only kept the raw Graniot response.
        for data in _all_wms_data_from_payload((local or {}).get("graniot_raw")):
            push(data.get("graniot_parcel_id"))
    return ids


def _clear_local_graniot_fields(parcel_id: str, user_id: Any) -> Optional[Dict[str, Any]]:
    with LOCK:
        db = read_db()
        row = next(
            (p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user_id),
            None,
        )
        if not row:
            return None
        for field in GRANIOT_LOCAL_SYNC_FIELDS:
            row.pop(field, None)
        row["updated_at"] = now()
        write_db(db)
        return _public_parcel(row)


def _all_dataris_remote_ids(db: Dict[str, Any]) -> set:
    """Ids de parcela de Graniot ligados a CUALQUIER lote de CUALQUIER usuario.

    Es el conjunto protegido: nada de aquí se borra al limpiar un portal, porque
    corresponde a un lote real de algún cliente de Dataris (aunque comparta finca
    con otros).
    """
    protected = set()
    for row in table(db, "parcels"):
        for rid in _remote_parcel_ids(row):
            protected.add(str(rid))
    return protected


@router.post("/admin/portal/purge-orphans")
async def purge_portal_orphans(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Limpia el portal satelital de un cliente de parcelas huérfanas (superadmin).

    El portal de un cliente puede vivir en una finca de Graniot COMPARTIDA con
    otros gestores (el caso del cajón 3615): con el tiempo se llena de parcelas
    que no pertenecen a ningún cliente de Dataris. Este endpoint lista lo que el
    cliente ve en SU portal (finca), marca como huérfana toda parcela que NO
    esté ligada a NINGÚN lote de NINGÚN usuario de Dataris, y las borra.

    - Por defecto es dry-run (`dry_run=true`): no borra, informa y devuelve el
      backup con la geometría para poder recrear si hiciera falta.
    - El LISTADO se hace con la cuenta del propio cliente, así que se limita a su
      finca; el BORRADO usa la API key del partner, que puede borrar parcelas de
      cualquier cuenta dentro de esa finca.
    - NUNCA borra una parcela ligada a un lote de Dataris (de este cliente o de
      cualquier otro que comparta la finca).
    """
    db = read_db()
    ctx = require_admin_context(authorization, db)
    if ctx["admin"].get("admin_role") != "superadmin":
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede limpiar portales")

    email = str(payload.get("user_email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Falta user_email")
    dry_run = bool(payload.get("dry_run", True))

    user = next((u for u in db.get("users", []) if str(u.get("email") or "").strip().lower() == email), None)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Cliente escopado a la finca del usuario: solo ve su portal.
    target = await _resolve_sync_target(email, operation="purge-portal-orphans")
    scoped_client = _client_for_target(target)
    try:
        raw = await scoped_client.get("/api/parcels/")
    except Exception as exc:  # noqa: BLE001
        _raise_graniot_error(exc)
    portal = _items(raw)

    protected = _all_dataris_remote_ids(db)

    def _pid(feature: Dict[str, Any]) -> Optional[str]:
        return _clean_id(feature.get("id") or (feature.get("properties") or {}).get("id"))

    def _pname(feature: Dict[str, Any]) -> str:
        return str((feature.get("properties") or {}).get("name") or "")

    orphans: List[Dict[str, Any]] = []
    linked = 0
    for feature in portal:
        pid = _pid(feature)
        if not pid:
            continue
        if str(pid) in protected:
            linked += 1
        else:
            orphans.append(feature)

    backup = [
        {
            "graniot_parcel_id": _pid(f),
            "name": _pname(f),
            "geometry": f.get("geometry"),
            "bbox": f.get("bbox"),
        }
        for f in orphans
    ]

    summary = {
        "user_email": email,
        "portal_total": len(portal),
        "linked_to_dataris": linked,
        "orphans": len(orphans),
        "dry_run": dry_run,
        "target_mode": target.get("mode"),
    }

    if dry_run:
        return {
            "data": {
                **summary,
                "would_delete": [{"graniot_parcel_id": b["graniot_parcel_id"], "name": b["name"]} for b in backup],
                "backup": backup,
            },
            "error": None,
        }

    # Borrado real con la API key del partner (puede borrar de cualquier cuenta
    # dentro de la finca). Las que ya no estén cuentan como borradas.
    service_client = GraniotClient()
    deleted: List[str] = []
    missing: List[str] = []
    failed: List[Dict[str, Any]] = []
    for feature in orphans:
        pid = _pid(feature)
        try:
            await service_client.delete(
                f"/api/parcels/{pid}/",
                debug_context={"operation": "purge-portal-orphans", "graniot_parcel_id": pid, "user_email": email},
            )
            deleted.append(pid)
        except GraniotAPIError as exc:
            if exc.status_code in {404, 410}:
                missing.append(pid)
            else:
                failed.append({"graniot_parcel_id": pid, "status_code": exc.status_code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — seguir borrando el resto
            failed.append({"graniot_parcel_id": pid, "message": str(exc)})

    log_event({
        "event": "dataris.graniot.purge_portal_orphans",
        "user_email": email,
        "portal_total": len(portal),
        "deleted": len(deleted),
        "missing": len(missing),
        "failed": len(failed),
    })
    return {
        "data": {**summary, "deleted": deleted, "missing": missing, "failed": failed, "backup": backup},
        "error": None,
    }


async def delete_parcel_from_graniot(
    user: Dict[str, Any],
    local: Dict[str, Any],
    *,
    clear_local: bool = True,
) -> Dict[str, Any]:
    """Delete from Graniot every parcel created for a local Dataris lot.

    ``local`` may be a row that no longer exists in Dataris (the automatic
    cleanup runs right after the lot is deleted), so the Graniot ids are read
    from the snapshot passed in. Parcels already gone in Graniot count as
    deleted: the desired end state is the same.
    """
    remote_ids = _remote_parcel_ids(local or {})
    result: Dict[str, Any] = {
        "local_parcel_id": (local or {}).get("id"),
        "remote_ids": remote_ids,
        "deleted": [],
        "missing": [],
        "failed": [],
        "target": None,
        "local_cleared": False,
    }
    if not remote_ids:
        return result

    target = await _sync_target_for_row(user, local, operation="delete-local-parcel")
    result["target"] = _public_sync_target(target)
    client = _client_for_target(target)

    async def _try_delete(active: GraniotClient, remote_id: str, attempt: str) -> Optional[GraniotAPIError]:
        """Return None when the parcel is gone, or the error that prevented it."""
        try:
            await active.delete(
                f"/api/parcels/{remote_id}/",
                debug_context={
                    "operation": "delete-local-parcel",
                    "attempt": attempt,
                    "local_parcel_id": (local or {}).get("id"),
                    "graniot_parcel_id": remote_id,
                    "mode": target.get("mode"),
                },
            )
            return None
        except GraniotAPIError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 — keep deleting the other parcels
            return GraniotAPIError(500, str(exc))

    # Lots synced before parcels were split per user live in the API key owner's
    # account, where the user's own token/client_id cannot reach them. Only those
    # (no stored account) get a second attempt with the service client.
    legacy_fallback = target.get("mode") != SYNC_MODE_SERVICE and not str(
        (local or {}).get("graniot_account_email") or ""
    ).strip()
    service_client: Optional[GraniotClient] = None

    for remote_id in remote_ids:
        error = await _try_delete(client, remote_id, "target-account")
        if error is not None and legacy_fallback and error.status_code in {403, 404, 410}:
            service_client = service_client or GraniotClient()
            error = await _try_delete(service_client, remote_id, "legacy-service-account")
        if error is None:
            result["deleted"].append(remote_id)
            continue
        if error.status_code in {404, 410}:
            result["missing"].append(remote_id)
            continue
        result["failed"].append({
            "graniot_parcel_id": remote_id,
            "status_code": error.status_code,
            "message": str(error),
        })

    if clear_local and not result["failed"]:
        cleared = _clear_local_graniot_fields(str((local or {}).get("id") or ""), (user or {}).get("id"))
        result["local_cleared"] = cleared is not None
        if cleared is not None:
            result["parcel"] = cleared

    log_event({
        "event": "dataris.delete_local_parcel.finished",
        "operation": "delete-local-parcel",
        "local_parcel_id": (local or {}).get("id"),
        "target": _public_sync_target(target),
        "remote_ids": remote_ids,
        "deleted": result["deleted"],
        "missing": result["missing"],
        "failed": safe_payload(result["failed"]),
    })
    return result


@router.delete("/parcels/sync-local/{parcel_id}")
async def unsync_local_parcel(
    parcel_id: str,
    user_id: Optional[str] = Query(default=None, description="Dueño del lote (solo para gestores de lotes)"),
    authorization: Optional[str] = Header(default=None),
):
    """Remove the lot from Graniot without deleting it in Dataris."""
    user = _acting_user(authorization, user_id)
    with LOCK:
        db = read_db()
        local = next(
            (p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]),
            None,
        )
        snapshot = dict(local) if local else None
    if not snapshot:
        raise HTTPException(status_code=404, detail="Lote local no encontrado")

    result = await delete_parcel_from_graniot(user, snapshot)
    if result.get("failed"):
        first = result["failed"][0]
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Graniot no pudo eliminar el lote: {first.get('message')}",
                "result": result,
            },
        )
    if not result.get("remote_ids"):
        return {"data": {**result, "message": "El lote no estaba sincronizado con Graniot"}, "error": None}
    return {"data": result, "error": None}


async def _probe_target_account(target: Dict[str, Any]) -> Dict[str, Any]:
    """What the target account actually sees in Graniot (support diagnostics).

    Useful to confirm that a lot landed in the user's own account: the partner
    API key can list every client's parcels, so counting them with the key
    proves nothing — asking *as the account* does.
    """
    client = _client_for_target(target)
    probe: Dict[str, Any] = {"mode": target.get("mode")}
    for label, path in (("farms", "/api/farms/"), ("parcels", "/api/parcels/")):
        try:
            payload = await client.get(path, debug_context={"operation": "probe-parcel-sync-target"})
            items = _items(payload)
            probe[label] = len(items)
            probe[f"{label}_sample"] = [
                {
                    "id": item.get("id"),
                    "name": (item.get("properties") or {}).get("name") if isinstance(item.get("properties"), dict) else item.get("name"),
                }
                for item in items[-5:]
            ]
        except Exception as exc:  # noqa: BLE001 — diagnostics must not fail the request
            probe[label] = None
            probe[f"{label}_error"] = str(exc)[:300]
    return probe


async def _fetch_farm_managers(client: GraniotClient, limit: int = 40) -> Dict[str, Any]:
    """Gestores dados de alta dentro de las fincas de la cuenta.

    Son personas creadas *dentro* de una cuenta de Graniot y NO aparecen en
    ``/api/accounts/``: no tienen portal propio, así que sus emails no sirven
    para repartir los lotes por usuario. Se listan solo como diagnóstico, para
    distinguirlos de las cuentas reales.
    """
    result: Dict[str, Any] = {"farms": 0, "managers": [], "error": None}
    try:
        farms = _items(await client.get("/api/farms/", debug_context={"operation": "list-farm-managers"}))
    except Exception as exc:  # noqa: BLE001 — diagnóstico, nunca rompe la respuesta
        result["error"] = str(exc)[:300]
        return result

    result["farms"] = len(farms)
    seen: Dict[str, Dict[str, Any]] = {}
    for farm in farms[:limit]:
        farm_id = _clean_id(farm.get("id"))
        if not farm_id:
            continue
        try:
            managers = _items(await client.get(
                f"/api/farms/{farm_id}/managers/",
                debug_context={"operation": "list-farm-managers", "farm_id": farm_id},
            ))
        except Exception:
            continue
        for manager in managers:
            # Graniot no documenta con exactitud esta respuesta: se recogen los
            # campos habituales para poder identificar a la persona.
            email = next(
                (
                    str(manager.get(field)).strip()
                    for field in ("email", "user_email", "username", "account_email")
                    if manager.get(field)
                ),
                None,
            )
            name = next(
                (
                    str(manager.get(field)).strip()
                    for field in ("name", "full_name", "first_name", "user", "manager")
                    if manager.get(field)
                ),
                None,
            )
            # Cada "manager" es en realidad un vínculo cuenta↔finca: agruparlos
            # por account_id dice cuántas cuentas de Graniot tienen fincas, que
            # puede ser más que las que /api/accounts/ deja ver.
            account_id = _clean_id(manager.get("account_id"))
            key = str(account_id or email or manager.get("key") or name or json.dumps(manager, sort_keys=True, default=str))
            entry = seen.setdefault(key, {
                "account_id": account_id,
                "id": manager.get("id"),
                "name": name,
                "email": email,
                "fields": sorted(manager.keys()),
                "farms": [],
            })
            entry["farms"].append(farm.get("name") or farm_id)
        if managers and not result.get("sample"):
            result["sample"] = safe_payload(managers[0])
    result["managers"] = list(seen.values())
    return result


@router.get("/accounts")
async def list_graniot_accounts(
    refresh: bool = Query(default=True),
    include_managers: bool = Query(default=False),
    probe_client_id: Optional[str] = Query(default=None, description="Comprueba qué ve la API key actuando como este client_id"),
    authorization: Optional[str] = Header(default=None),
):
    """Cuentas de Graniot y qué usuario de Dataris recibe cada una (solo admin).

    Sirve para ver de un vistazo qué usuarios pueden recibir sus lotes en su
    propio portal y cuáles no tienen cuenta en Graniot todavía. Nunca devuelve
    ``account_access`` ni el ``auth_id`` del portal.
    """
    db = read_db()
    require_admin_context(authorization, db)

    client = GraniotClient()
    try:
        accounts = await _fetch_all_accounts(client, operation="list-graniot-accounts")
    except Exception as exc:
        _raise_graniot_error(exc)

    dataris_users = {
        str(user.get("email") or "").strip().lower(): user
        for user in (db.get("users") or [])
        if user.get("email")
    }
    matched_emails = set()
    items: List[Dict[str, Any]] = []
    for account in accounts:
        email = str(account.get("account_email") or "").strip().lower()
        if email:
            matched_emails.add(email)
        claims = _jwt_payload(account.get("account_access")) or _jwt_payload(
            _embed_url_auth_token(account.get("embedded_url") or "")
        )
        items.append({
            "id": account.get("id"),
            "account_email": account.get("account_email"),
            "client_id": _account_client_id(account),
            "has_account_token": bool(_account_access_token(account)),
            "dataris_user": bool(email and email in dataris_users),
            # Qué identificadores trae el token: Graniot solo acepta como
            # client_id el id numérico del usuario.
            "token_claims": sorted(claims.keys()),
            "token_ids": {
                key: claims.get(key)
                for key in ("user_id", "uid", "sub", "account_id", "id")
                if claims.get(key) is not None
            },
        })
    items.sort(key=lambda item: str(item.get("account_email") or "").lower())

    users_without_account = sorted(email for email in dataris_users if email not in matched_emails)
    data: Dict[str, Any] = {
        "count": len(items),
        "accounts": items,
        "dataris_users_total": len(dataris_users),
        "dataris_users_with_account": sum(1 for item in items if item["dataris_user"]),
        "dataris_users_without_account": users_without_account,
    }
    if include_managers:
        managers = await _fetch_farm_managers(client)
        known_ids = {str(item.get("id")) for item in items if item.get("id")}
        managers["accounts_with_farms"] = len({
            entry.get("account_id") for entry in managers.get("managers") or [] if entry.get("account_id")
        })
        managers["accounts_not_listed"] = sorted({
            str(entry.get("account_id"))
            for entry in managers.get("managers") or []
            if entry.get("account_id") and str(entry.get("account_id")) not in known_ids
        })
        data["farm_managers"] = managers
    if probe_client_id:
        # Los vínculos de finca identifican a la cuenta por email, no por el
        # "acc-<uuid>" de /api/accounts/. Esto comprueba con qué identificador
        # acepta Graniot actuar en nombre de otra cuenta.
        data["client_id_probe"] = await _probe_target_account({
            "mode": SYNC_MODE_CLIENT_ID,
            "client_id": str(probe_client_id).strip(),
        })
        data["client_id_probe"]["client_id"] = str(probe_client_id).strip()
    return {"data": data, "error": None}


@router.post("/accounts")
async def create_graniot_account(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Da de alta en Graniot la cuenta de un email (solo admin).

    Es lo que permite que un usuario de Dataris reciba sus lotes en su propio
    portal sin pedírselo al comercial de Graniot: hasta que su cuenta aparece en
    ``/api/accounts/`` no hay forma de actuar en su nombre.

    La respuesta indica si la cuenta recién listada **ve las fincas que ya tenía
    esa persona**; si viniera vacía sería una cuenta nueva distinta, y conviene
    saberlo antes de mandarle lotes.
    """
    db = read_db()
    require_admin_context(authorization, db)

    email = str(payload.get("account_email") or payload.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Indica el correo de la cuenta de Graniot")

    client = GraniotClient()
    try:
        existing = _raw_account_for_email(await _fetch_all_accounts(client, operation="create-graniot-account"), email)
        created = existing
        if not existing:
            created = await client.post(
                "/api/accounts/",
                json_body={"account_email": email},
                params=None,
                debug_context={"operation": "create-graniot-account", "email": email},
            )
            error = _payload_error_message(created)
            if error:
                raise GraniotAPIError(400, error, created)
            if isinstance(created, list):
                created = _raw_account_for_email(created, email) or (created[0] if created else None)
    except Exception as exc:
        _raise_graniot_error(exc)

    if not isinstance(created, dict) or not created.get("account_email"):
        raise HTTPException(status_code=502, detail="Graniot no devolvió la cuenta creada")

    _cache_delete_prefix(_EMBED_ACCOUNTS_CACHE_KEY)
    target = {
        "mode": SYNC_MODE_TOKEN if _account_access_token(created) else SYNC_MODE_CLIENT_ID,
        "access_token": _account_access_token(created),
        "client_id": _account_client_id(created),
        "account_id": _account_id_value(created),
    }
    probe = await _probe_target_account(target) if (target["access_token"] or target["client_id"]) else None

    return {
        "data": {
            "already_existed": bool(existing),
            "account_email": created.get("account_email"),
            "account_id": _account_id_value(created),
            "client_id": _account_client_id(created),
            "has_account_token": bool(_account_access_token(created)),
            # Fincas/parcelas que ve la cuenta: si están vacías cuando la
            # persona ya tenía fincas, Graniot creó una cuenta distinta.
            "sees": probe,
        },
        "error": None,
    }


@router.delete("/accounts/{account_id}")
async def delete_graniot_account(
    account_id: str,
    authorization: Optional[str] = Header(default=None),
):
    """Da de baja una cuenta de Graniot creada desde Dataris (solo admin).

    Permite deshacer un alta equivocada. No toca las fincas ni las parcelas de
    la cuenta: solo retira el acceso que Dataris había dado de alta.
    """
    db = read_db()
    require_admin_context(authorization, db)

    client = GraniotClient()
    try:
        await client.delete(
            f"/api/accounts/{account_id}/",
            debug_context={"operation": "delete-graniot-account", "account_id": account_id},
        )
    except GraniotAPIError as exc:
        if exc.status_code not in {404, 410}:
            _raise_graniot_error(exc)
    except Exception as exc:
        _raise_graniot_error(exc)

    _cache_delete_prefix(_EMBED_ACCOUNTS_CACHE_KEY)
    return {"data": {"account_id": account_id, "deleted": True}, "error": None}


@router.get("/embed/links")
async def list_embed_links(
    refresh: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None),
):
    """Quién ve su propio portal satelital y quién no (solo admin).

    Cruza tres padrones que Graniot mantiene separados: los usuarios de Dataris,
    los dueños de finca de Graniot (``/api/company/farms/``) y las cuentas de
    mapa embebido (``/api/accounts/``). Es la vista que dice, de un vistazo, a
    qué cliente le falta portal y por qué.
    """
    db = read_db()
    require_admin_context(authorization, db)

    try:
        census = await _fetch_company_farms_index(refresh=refresh)
        accounts = await _fetch_embed_accounts(refresh=refresh)
    except Exception as exc:
        _raise_graniot_error(exc)

    links = {
        str(link.get("user_email") or "").strip().lower(): link
        for link in _embed_links(db)
    }
    template = settings.GRANIOT_EMBED_ALIAS_TEMPLATE or "dataris-embed+{uid}@dataris.es"
    accounts_by_email = {
        str(account.get("account_email") or "").strip().lower(): account
        for account in _items(accounts)
    }

    items: List[Dict[str, Any]] = []
    for user in db.get("users") or []:
        email = str(user.get("email") or "").strip().lower()
        if not email:
            continue
        platform_user = census.get(email)
        link = links.get(email)
        account = (
            _account_by_id(accounts, (link or {}).get("account_id"))
            or accounts_by_email.get(email)
            or (accounts_by_email.get(_embed_alias_for(platform_user, email)) if platform_user else None)
        )
        if account:
            portal = "own_account" if not platform_user or account.get("account_email") == email else "provisioned"
        elif platform_user:
            portal = "pending_provision"
        else:
            portal = "service_fallback"
        items.append({
            "user_id": user.get("id"),
            "user_email": email,
            "portal": portal,
            "account_id": (account or {}).get("id"),
            "account_email": (account or {}).get("account_email"),
            "platform_user_id": (platform_user or {}).get("user_id"),
            "platform_farms": len((platform_user or {}).get("farms") or []),
            "expected_alias": _embed_alias_for(platform_user, email) if platform_user else None,
            "link": _public_embed_link(link),
        })
    items.sort(key=lambda item: (item["portal"], item["user_email"]))

    dataris_emails = {item["user_email"] for item in items}
    return {
        "data": {
            "count": len(items),
            "users": items,
            # Dueños de finca en Graniot que aún no son usuarios de Dataris: son
            # los clientes a los que todavía no se les ha dado acceso aquí.
            "platform_users_without_dataris_user": [
                {
                    "email": email,
                    "platform_user_id": entry.get("user_id"),
                    "platform_farms": len(entry.get("farms") or []),
                }
                for email, entry in sorted(census.items())
                if email not in dataris_emails
            ],
            "embed_accounts": sorted(accounts_by_email),
            "alias_template": template,
            "autoprovision_enabled": bool(settings.GRANIOT_EMBED_AUTOPROVISION_ENABLED),
        },
        "error": None,
    }


@router.post("/embed/links")
async def provision_embed_link(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Crea (o repara) el portal satelital propio de un usuario (solo admin).

    Da de alta su cuenta de mapa embebido si no la tiene y le asigna las fincas
    de su usuario de plataforma de Graniot. Es idempotente: repetirlo solo añade
    las fincas que falten, así que sirve también como "volver a sincronizar".
    """
    db = read_db()
    require_admin_context(authorization, db)

    email = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    user_id = str(payload.get("user_id") or "").strip()
    if not email and user_id:
        owner = next((u for u in db.get("users") or [] if str(u.get("id")) == user_id), None)
        if not owner:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        email = str(owner.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Indica el correo del usuario")

    # El correo del usuario de plataforma puede diferir del de Dataris (otra
    # cuenta de la misma persona): quien administra puede indicarlo a mano.
    platform_email = str(payload.get("platform_email") or "").strip().lower() or email
    try:
        platform_user = await _platform_user_for_email(platform_email, refresh=bool(payload.get("refresh")))
        result = await _provision_embed_account(
            email,
            platform_user=platform_user,
            provisioned_by="admin",
        )
    except Exception as exc:
        _raise_graniot_error(exc)

    if not result.get("provisioned") and result.get("reason") == "no_platform_farms_for_email":
        raise HTTPException(
            status_code=404,
            detail=(
                f"{platform_email} no aparece como responsable de ninguna finca en Graniot, "
                "así que no hay fincas que mostrar en su mapa embebido."
            ),
        )
    if not result.get("provisioned"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Graniot no permitió crear el portal")
    return {"data": result, "error": None}


@router.delete("/embed/links/{user_email}")
async def delete_embed_link(
    user_email: str,
    delete_account: bool = Query(default=False, description="Borrar también la cuenta embebida en Graniot"),
    unlink_farms: bool = Query(default=False, description="Retirar en Graniot las fincas asignadas a esa cuenta"),
    authorization: Optional[str] = Header(default=None),
):
    """Deshace el vínculo de un usuario con su portal embebido (solo admin)."""
    db = read_db()
    require_admin_context(authorization, db)

    link = _find_embed_link(db, user_email)
    if not link:
        raise HTTPException(status_code=404, detail="Ese usuario no tiene un portal vinculado")

    account_id = str(link.get("account_id") or "").strip()
    removed_farms: List[Any] = []
    if unlink_farms and account_id:
        client = GraniotClient()
        for farm_id in link.get("farm_ids") or []:
            try:
                if await unlink_farm_from_account(client, farm_id, account_id):
                    removed_farms.append(farm_id)
            except Exception as exc:  # noqa: BLE001 — la baja local no depende de esto
                log_event({
                    "event": "dataris.graniot.embed_provision.unlink_failed",
                    "operation": "graniot-embed-unlink",
                    "farm_id": farm_id,
                    "account_id": account_id,
                    "message": str(exc)[:300],
                })

    deleted_account = False
    if delete_account and account_id:
        try:
            await GraniotClient().delete(
                f"/api/accounts/{account_id}/",
                debug_context={"operation": "graniot-embed-unlink", "account_id": account_id},
            )
            deleted_account = True
        except GraniotAPIError as exc:
            if exc.status_code not in {404, 410}:
                _raise_graniot_error(exc)
            deleted_account = True
        except Exception as exc:
            _raise_graniot_error(exc)

    _delete_embed_link(user_email)
    _cache_delete_prefix(_EMBED_ACCOUNTS_CACHE_KEY)
    return {
        "data": {
            "user_email": str(user_email).strip().lower(),
            "unlinked": True,
            "account_deleted": deleted_account,
            "farms_unlinked": removed_farms,
        },
        "error": None,
    }


@router.get("/parcels/sync-target")
async def get_parcel_sync_target(
    refresh: bool = Query(default=False),
    probe: bool = Query(default=False),
    probe_mode: Optional[str] = Query(default=None, description="token|client_id|service: fuerza el modo solo en la sonda"),
    user_id: Optional[str] = Query(default=None, description="Consultar la cuenta de otro usuario (solo gestores de lotes)"),
    authorization: Optional[str] = Header(default=None),
):
    """Diagnostics: which Graniot account receives this user's lots."""
    user = _acting_user(authorization, user_id)
    target = await _resolve_sync_target(
        (user or {}).get("email"),
        refresh=refresh,
        operation="resolve-parcel-sync-target",
    )
    data = {
        **_public_sync_target(target),
        "per_user_enabled": bool(settings.GRANIOT_PARCEL_SYNC_PER_USER_ENABLED),
        "autosync_enabled": bool(settings.GRANIOT_PARCEL_AUTOSYNC_ENABLED),
        "autodelete_enabled": bool(settings.GRANIOT_PARCEL_AUTODELETE_ENABLED),
        "requires_account": bool(settings.GRANIOT_PARCEL_SYNC_REQUIRE_ACCOUNT),
        "sync_mode_setting": str(settings.GRANIOT_PARCEL_SYNC_MODE or "auto"),
    }
    if probe:
        # probe_mode permite comprobar el camino alternativo (por ejemplo si el
        # `client_id` privilegiado alcanza la cuenta) sin cambiar la
        # configuración del entorno: la sonda solo lee.
        probed = dict(target)
        requested = str(probe_mode or "").strip().lower()
        if requested in {SYNC_MODE_TOKEN, SYNC_MODE_CLIENT_ID, SYNC_MODE_SERVICE}:
            probed["mode"] = requested
        data["probe"] = await _probe_target_account(probed)
    return {"data": data, "error": None}


# ---- Dataris Graniot NDVI map-layer orchestration -------------------------
# The satellite UI should not guess WMS access keys or layer identifiers. This
# endpoint resolves the local parcel against Graniot, recovers the signed WMS
# template returned by Graniot, and returns render-ready overlays. The frontend
# only paints the resulting image URLs over the bounds returned here.

DEFAULT_NDVI_LAYER_KEY = "7a66c49e-acdb-46c6-aea4-505fdf3edf48"
DEFAULT_NDVI_WMS_LAYER = "NDVI"
DEFAULT_NDVI_RESOLUTION_ID = 1
DEFAULT_NDVI_RESOLUTION_KEY = "80f07c38-39b9-4df9-8c0b-a586e52b2843"


# Confirmed Graniot catalog values from /api/layers/layers-platform/ and
# /api/layers/get_wms_layers/.  The important distinction is:
# - `key` is required by /parcels/{id}/layers/{layer_key}/statistics and json-index.
# - `wms_layer` is required by /api/wms/?layers=... .
# The resolver below prevents a UI selection like "NDVI_UAV" from being used on
# a parcel that only has Sentinel resolution 1 images, which caused the all-blue
# raster that was observed in production.
GRANIOT_KNOWN_INDEX_LAYERS: List[Dict[str, Any]] = [
    {
        "family": "NDVI",
        "source": "sentinel",
        "key": "7a66c49e-acdb-46c6-aea4-505fdf3edf48",
        "wms_layer": "NDVI",
        "name": "NDVI",
        "resolution_id": 1,
        "resolution_key": "80f07c38-39b9-4df9-8c0b-a586e52b2843",
        "resolution_label": "10x10 meters",
        "priority": 10,
    },
    {
        "family": "GNDVI",
        "source": "sentinel",
        "key": "686f6293-e092-44b3-842f-2ad22867b167",
        "wms_layer": "GNDVI",
        "name": "GNDVI",
        "resolution_id": 1,
        "resolution_key": "80f07c38-39b9-4df9-8c0b-a586e52b2843",
        "resolution_label": "10x10 meters",
        "priority": 10,
    },
    {
        "family": "NDVI",
        "source": "planet",
        "key": "ea30b1ef-26e0-4743-beb2-3c12da6a2bb9",
        "wms_layer": "NDVI_PLANET",
        "name": "NDVI_PLANET",
        "resolution_id": 2,
        "resolution_key": "1df21923-9466-4859-86e6-c0d18b3dc9ec",
        "resolution_label": "3x3 meters",
        "priority": 20,
    },
    {
        "family": "GNDVI",
        "source": "planet",
        "key": "647fe571-a753-4b8f-a9b1-431dee3c192a",
        "wms_layer": "GNDVI_PLANET",
        "name": "GNDVI_PLANET",
        "resolution_id": 2,
        "resolution_key": "1df21923-9466-4859-86e6-c0d18b3dc9ec",
        "resolution_label": "3x3 meters",
        "priority": 20,
    },
    {
        "family": "NDVI",
        "source": "planet4",
        "key": "90211e25-3e1d-4b98-b747-bcd132d2f605",
        "wms_layer": "NDVI_PLANET4",
        "name": "NDVI_PLANET4",
        "resolution_id": 6,
        "resolution_key": "c13d3296-dbdb-4af2-b22e-9893b019926a",
        "resolution_label": "3x3 meters (4bands)",
        "priority": 30,
    },
    {
        "family": "NDVI",
        "source": "superresolution",
        "key": "01cf0b02-6d62-46ef-aeed-e8de477a5b55",
        "wms_layer": "NDVI_SUPERRESOLUTION",
        "name": "NDVI_SUPERRESOLUTION",
        "resolution_id": 12,
        "resolution_key": "c1ebf592-31e5-4f00-9072-49f473c3d438",
        "resolution_label": "1x1 meter",
        "priority": 40,
    },
    {
        "family": "NDVI",
        "source": "uav",
        "key": "32fc2f64-9c84-42ce-bbd0-3ef48678789d",
        "wms_layer": "NDVI_UAV",
        "name": "NDVI_UAV",
        "resolution_id": 7,
        "resolution_key": "e7aed7dc-e439-4b38-9847-66cd94b01ff8",
        "resolution_label": "UAV",
        "priority": 80,
    },
    {
        "family": "NDVI",
        "source": "uav-rgb-ms",
        "key": "c83eee1e-94aa-46a2-a40d-c76878f91fab",
        "wms_layer": "NDVI_UAV_RGB_MS",
        "name": "NDVI_UAV_RGB_MS",
        "resolution_id": 17,
        "resolution_key": "1e412157-ab19-434f-9b74-e72db9e602da",
        "resolution_label": "UAV RGB-MS",
        "priority": 90,
    },
]


def _clean_layer_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _index_family_from_text(value: Any) -> Optional[str]:
    text = _clean_layer_token(value).replace("-", "_").replace(" ", "_")
    if not text:
        return None
    # Check GNDVI before NDVI because GNDVI contains NDVI.
    if "gndvi" in text:
        return "GNDVI"
    if "ndvi" in text:
        return "NDVI"
    return None


def _known_layer_from_request(layer_key: Any, wms_layer: Any) -> Optional[Dict[str, Any]]:
    requested = {_clean_layer_token(layer_key), _clean_layer_token(wms_layer)}
    for layer in GRANIOT_KNOWN_INDEX_LAYERS:
        keys = {
            _clean_layer_token(layer.get("key")),
            _clean_layer_token(layer.get("wms_layer")),
            _clean_layer_token(layer.get("name")),
        }
        if requested & keys:
            return layer
    return None


def _available_resolution_entries_from_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    for candidate in (
        source.get("parcelresolution_set"),
        props.get("parcelresolution_set"),
        raw.get("parcelresolution_set"),
    ):
        candidate = _safe_json_loads(candidate)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _source_has_image_for_resolution(source: Dict[str, Any], resolution_id: Optional[int]) -> bool:
    entries = _available_resolution_entries_from_source(source)
    if not entries or resolution_id is None:
        return True
    for item in entries:
        try:
            if int(item.get("resolution")) == int(resolution_id) and bool(item.get("last_image_date") or item.get("date") or item.get("image_date")):
                return True
        except Exception:
            continue
    return False


def _available_resolution_ids_with_images(sources: List[Dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for source in sources:
        for item in _available_resolution_entries_from_source(source):
            try:
                if item.get("last_image_date") or item.get("date") or item.get("image_date"):
                    ids.add(int(item.get("resolution")))
            except Exception:
                continue
    return ids


def _resolve_requested_graniot_layer(
    *,
    layer_key: str,
    wms_layer: str,
    resolution_id: Optional[int],
    resolution_key: Optional[str],
    sources: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    requested = _known_layer_from_request(layer_key, wms_layer)
    family = (
        (requested or {}).get("family")
        or _index_family_from_text(wms_layer)
        or _index_family_from_text(layer_key)
    )
    available_ids = _available_resolution_ids_with_images(sources)

    if family:
        compatible = [layer for layer in GRANIOT_KNOWN_INDEX_LAYERS if layer.get("family") == family]
        if available_ids:
            compatible_with_image = [layer for layer in compatible if int(layer.get("resolution_id")) in available_ids]
        else:
            compatible_with_image = compatible

        if requested and (not available_ids or int(requested.get("resolution_id")) in available_ids):
            selected = requested
        elif compatible_with_image:
            selected = sorted(compatible_with_image, key=lambda item: int(item.get("priority", 999)))[0]
            if requested and selected.get("key") != requested.get("key"):
                warnings.append(
                    f"La capa solicitada {requested.get('wms_layer')} usa resolución {requested.get('resolution_id')}, "
                    f"pero el lote tiene imágenes en {sorted(available_ids) or 'otra resolución'}. "
                    f"Se usó {selected.get('wms_layer')} para evitar una imagen NDVI incompatible."
                )
        else:
            selected = requested or compatible[0]
            warnings.append(
                f"No se encontró una capa {family} con imagen disponible para las resoluciones del lote; "
                f"se intentará con {selected.get('wms_layer')}."
            )

        return {
            "key": str(selected.get("key") or layer_key or ""),
            "wms_layer": str(selected.get("wms_layer") or wms_layer or selected.get("name") or ""),
            "resolution_id": int(selected.get("resolution_id")) if selected.get("resolution_id") is not None else resolution_id,
            "resolution_key": str(selected.get("resolution_key") or resolution_key or ""),
            "resolution_label": selected.get("resolution_label"),
            "source": selected.get("source"),
            "family": selected.get("family") or family,
            "auto_selected": bool(requested and selected.get("key") != requested.get("key")),
        }

    return {
        "key": layer_key,
        "wms_layer": wms_layer or DEFAULT_NDVI_WMS_LAYER,
        "resolution_id": resolution_id,
        "resolution_key": resolution_key,
        "family": None,
        "auto_selected": False,
    }


def _local_parcel_geometry(row: Dict[str, Any]) -> Optional[Any]:
    for key in ("geometry_geojson", "geojson", "feature_collection", "geometry"):
        value = row.get(key)
        value = _safe_json_loads(value)
        if not value:
            continue
        try:
            if isinstance(value, dict) and value.get("type") == "FeatureCollection":
                geoms = []
                for feature in value.get("features") or []:
                    geom = feature.get("geometry") if isinstance(feature, dict) else None
                    if geom:
                        shp = _normalize_geometry_axes(shapely_shape(geom))
                        if not shp.is_empty:
                            geoms.append(shp)
                return unary_union(geoms) if geoms else None
            if isinstance(value, dict) and value.get("type") == "Feature":
                geom = value.get("geometry")
                return _normalize_geometry_axes(shapely_shape(geom)) if geom else None
            if isinstance(value, dict) and value.get("type") in {"Polygon", "MultiPolygon", "GeometryCollection"}:
                return _normalize_geometry_axes(shapely_shape(value))
        except Exception:
            continue
    return None


def _feature_geometry(item: Dict[str, Any]) -> Optional[Any]:
    try:
        geom = item.get("geometry")
        if not geom:
            props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
            geom = props.get("geometry")
        if not geom:
            return None
        shp = _normalize_geometry_axes(shapely_shape(geom))
        return shp if not shp.is_empty else None
    except Exception:
        return None


def _geometry_match_score(local_geom: Any, candidate_geom: Any) -> float:
    try:
        if not local_geom or not candidate_geom or local_geom.is_empty or candidate_geom.is_empty:
            return 0.0
        if not local_geom.intersects(candidate_geom):
            return 0.0
        intersection_area = float(local_geom.intersection(candidate_geom).area)
        if intersection_area <= 0:
            return 0.0
        local_area = max(float(local_geom.area), 1e-12)
        candidate_area = max(float(candidate_geom.area), 1e-12)
        # Use the best of coverage and IoU. Coverage helps when Graniot splits a
        # local polygon into several parcels; IoU helps exact one-to-one matches.
        union_area = max(float(local_geom.union(candidate_geom).area), 1e-12)
        coverage_local = intersection_area / local_area
        coverage_candidate = intersection_area / candidate_area
        iou = intersection_area / union_area
        return max(iou, min(coverage_local, coverage_candidate), coverage_local * 0.95)
    except Exception:
        return 0.0


def _graniot_name(value: Dict[str, Any]) -> str:
    props = value.get("properties") if isinstance(value.get("properties"), dict) else {}
    return str(props.get("name") or value.get("name") or "").strip().lower()


def _find_graniot_matches_for_local(local: Dict[str, Any], graniot_payload: Any) -> List[Dict[str, Any]]:
    features = _items(graniot_payload)
    if not features:
        return []

    wanted_id = _normalized_token(local.get("graniot_parcel_id"))
    wanted_name = str(local.get("name") or "").strip().lower()
    local_geom = _local_parcel_geometry(local)

    exact: List[Dict[str, Any]] = []
    if wanted_id:
        exact = [f for f in features if _normalized_token(f.get("id")) == wanted_id]
        # El id guardado puede ser de un lote AJENO que entró por nombre (el
        # «A10» de otra finca a 90 km). Si la parcela de Graniot con ese id está
        # lejos del polígono local, no es este lote: se sigue buscando.
        if exact and local_geom is not None:
            local_bounds = _bounds_dict_from_shapely_bounds(local_geom.bounds)
            exact = [
                f for f in exact
                if _feature_geometry(f) is None
                or _bounds_are_near(local_bounds, _bounds_dict_from_shapely_bounds(_feature_geometry(f).bounds))
            ]
        if exact:
            return exact

    scored: List[tuple[float, Dict[str, Any]]] = []
    if local_geom is not None:
        for feature in features:
            candidate_geom = _feature_geometry(feature)
            if candidate_geom is None:
                continue
            score = _geometry_match_score(local_geom, candidate_geom)
            if score >= 0.05:
                scored.append((score, feature))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            best = scored[0][0]
            # Keep all strong split-parcel matches. For normal one-to-one lots,
            # this returns only the highest scoring feature.
            threshold = max(0.08, min(0.55, best * 0.55))
            return [feature for score, feature in scored if score >= threshold][:12]

    if wanted_name:
        name_matches = [f for f in features if wanted_name and (wanted_name == _graniot_name(f) or wanted_name in _graniot_name(f) or _graniot_name(f) in wanted_name)]
        # Un lote llamado «1» casa por nombre con cualquier parcela cuyo nombre
        # contenga «1», esté donde esté: así entró una parcela de Costa Rica
        # como «subparcela» de lotes de Veracruz, el mapa encuadraba medio
        # continente y la capa quedaba de un píxel. Con geometría local, la
        # pareja por nombre tiene que estar además cerca del lote.
        if local_geom is not None:
            local_bounds = _bounds_dict_from_shapely_bounds(local_geom.bounds)
            name_matches = [
                f for f in name_matches
                if _feature_geometry(f) is None
                or _bounds_are_near(local_bounds, _bounds_dict_from_shapely_bounds(_feature_geometry(f).bounds))
            ]
        if name_matches:
            return name_matches[:12]

    return []


def _bounds_dict_from_shapely_bounds(bounds: Any) -> Optional[Dict[str, float]]:
    try:
        minx, miny, maxx, maxy = [float(v) for v in bounds]
    except Exception:
        return None
    if not all(v == v for v in (minx, miny, maxx, maxy)):
        return None
    return {"west": minx, "south": miny, "east": maxx, "north": maxy}


# Margen en grados (~5 km) con el que una fuente de Graniot todavía se
# considera «del lote». Absorbe recortes y bbox generosos; no absorbe otro país.
_SOURCE_NEAR_LOT_MARGIN_DEG = 0.05


def _bounds_are_near(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]], margin: float = _SOURCE_NEAR_LOT_MARGIN_DEG) -> bool:
    """True si los dos bbox se tocan (con margen). Sin datos, no se descarta."""
    if not a or not b:
        return True
    try:
        return not (
            float(b["west"]) > float(a["east"]) + margin
            or float(b["east"]) < float(a["west"]) - margin
            or float(b["south"]) > float(a["north"]) + margin
            or float(b["north"]) < float(a["south"]) - margin
        )
    except Exception:
        return True


def _source_bounds(source: Dict[str, Any]) -> Optional[Dict[str, float]]:
    return (
        _bbox_from_graniot_bbox(source.get("graniot_bbox"))
        or _bounds_from_wms_template(source.get("graniot_wms_url"))
        or _bounds_from_wms_template(source.get("graniot_image_url"))
    )


def _drop_sources_far_from_lot(local: Dict[str, Any], sources: List[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, Any]]:
    """Quita las fuentes guardadas que no tocan el lote.

    Entraban por nombre parcelas de otra finca (el «A10» de otro usuario a
    90 km) y hasta de otro país: la imagen se pintaba lejos del lote o el mapa
    se alejaba hasta no verse nada. Si no queda ninguna cerca, este lote NO
    tiene parcela en Graniot: se devuelve vacío para que map-layer lo
    sincronice con su geometría real en vez de pintar la de otro."""
    local_geom = _local_parcel_geometry(local)
    if local_geom is None or not sources:
        return sources
    local_bounds = _bounds_dict_from_shapely_bounds(local_geom.bounds)
    if not local_bounds:
        return sources
    kept = [source for source in sources if _bounds_are_near(local_bounds, _source_bounds(source))]
    dropped = len(sources) - len(kept)
    if dropped:
        if kept:
            warnings.append(
                f"Se omitieron {dropped} parcela(s) de Graniot asociadas a este lote que están lejos de su polígono."
            )
        else:
            warnings.append(
                "Las parcelas de Graniot guardadas para este lote están lejos de su polígono; se vuelve a sincronizar con la geometría real."
            )
        log_event({
            "event": "dataris.graniot.map_layer.sources_far_from_lot_dropped",
            "operation": "ndvi-map-layer",
            "local_parcel_id": local.get("id"),
            "dropped": [
                {"graniot_parcel_id": source.get("graniot_parcel_id"), "bounds": _source_bounds(source)}
                for source in sources if source not in kept
            ],
        })
        return kept
    return sources


def _date_from_graniot_sources(sources: List[Dict[str, Any]], preferred_resolution_id: Optional[int] = DEFAULT_NDVI_RESOLUTION_ID) -> Optional[str]:
    dates: List[str] = []
    for source in sources:
        raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
        props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        for candidate in (
            source.get("parcelresolution_set"),
            props.get("parcelresolution_set"),
            raw.get("parcelresolution_set"),
        ):
            date = _latest_image_date_from_resolutions(candidate, preferred_resolution_id)
            if date:
                dates.append(date)
            elif preferred_resolution_id is not None:
                # Fallback to any available image date if the requested resolution
                # has no image but another resolution does.
                any_date = _latest_image_date_from_resolutions(candidate, None)
                if any_date:
                    dates.append(any_date)
    return sorted(set(dates))[-1] if dates else None


def _source_to_map_overlay(
    *,
    local_parcel_id: str,
    source: Dict[str, Any],
    layer_name: str,
    date: Optional[str],
    width: int,
    height: int,
) -> Optional[Dict[str, Any]]:
    access_key = (
        source.get("graniot_wms_access_key")
        or _signed_wms_access_key(source.get("graniot_wms_url"))
        or _signed_wms_access_key(source.get("graniot_image_url"))
        or source.get("graniot_access_key")
    )
    bounds = (
        _bbox_from_graniot_bbox(source.get("graniot_bbox"))
        or _bounds_from_wms_template(source.get("graniot_wms_url"))
        or _bounds_from_wms_template(source.get("graniot_image_url"))
    )
    if not access_key or not bounds:
        return None

    query: Dict[str, Any] = {
        "parcel_id": local_parcel_id,
        "access_key": access_key,
        "layer": layer_name,
        "width": width,
        "height": height,
        "south": bounds["south"],
        "west": bounds["west"],
        "north": bounds["north"],
        "east": bounds["east"],
    }
    if date:
        query["time"] = date
    # Stable cache key shared with the frontend's classic WMS URLs and the
    # background prefetch endpoint. It intentionally avoids signed Graniot
    # tokens because those can rotate/expire while the parcel/layer/date image
    # is still the same visual product.
    query["cache_key"] = f"visual-v6:parcel:{local_parcel_id}:layer:{layer_name}:time:{date or 'latest'}"
    query["cache_v"] = "visual-v6"

    return {
        "id": str(source.get("graniot_parcel_id") or source.get("graniot_parcel_key") or source.get("graniot_access_key") or local_parcel_id),
        "graniot_parcel_id": source.get("graniot_parcel_id"),
        "image_url": f"/api/graniot/wms-proxy?{urlencode(query)}",
        "bounds": bounds,
        "date": date,
        "layer": layer_name,
        "source": "graniot-wms-proxy",
    }


def _persist_graniot_sources(local_parcel_id: str, user_id: str, raw: Any, sources: List[Dict[str, Any]], selected_date: Optional[str]) -> None:
    if not sources:
        return
    public_sources = []
    for source in sources:
        raw_obj = source.get("raw") if isinstance(source.get("raw"), dict) else {}
        props = raw_obj.get("properties") if isinstance(raw_obj.get("properties"), dict) else {}
        public_sources.append({
            "graniot_parcel_id": source.get("graniot_parcel_id"),
            "graniot_parcel_key": source.get("graniot_parcel_key"),
            "graniot_access_key": source.get("graniot_access_key"),
            "graniot_wms_access_key": source.get("graniot_wms_access_key"),
            "graniot_wms_url": source.get("graniot_wms_url"),
            "graniot_image_url": source.get("graniot_image_url"),
            "graniot_geometry": source.get("graniot_geometry"),
            "graniot_bbox": source.get("graniot_bbox"),
            "bounds": _bbox_from_graniot_bbox(source.get("graniot_bbox")) or _bounds_from_wms_template(source.get("graniot_wms_url")) or _bounds_from_wms_template(source.get("graniot_image_url")),
            "name": props.get("name") or raw_obj.get("name"),
            "hectares": props.get("hectares"),
            "parcelresolution_set": props.get("parcelresolution_set") or raw_obj.get("parcelresolution_set"),
            "last_image_date": selected_date or _latest_image_date_from_resolutions(props.get("parcelresolution_set") or raw_obj.get("parcelresolution_set")),
        })

    first = public_sources[0]
    with LOCK:
        db = read_db()
        row = next((p for p in table(db, "parcels") if p.get("id") == local_parcel_id and p.get("user_id") == user_id), None)
        if not row:
            return
        row.update({
            "graniot_parcel_id": first.get("graniot_parcel_id") or row.get("graniot_parcel_id"),
            "graniot_parcel_key": first.get("graniot_parcel_key") or row.get("graniot_parcel_key"),
            "graniot_access_key": first.get("graniot_access_key") or row.get("graniot_access_key"),
            "graniot_wms_access_key": first.get("graniot_wms_access_key") or row.get("graniot_wms_access_key"),
            "graniot_wms_url": first.get("graniot_wms_url") or row.get("graniot_wms_url"),
            "graniot_image_url": first.get("graniot_image_url") or row.get("graniot_image_url"),
            "graniot_parcels": public_sources,
            "graniot_raw": raw,
            "graniot_synced_at": now(),
            "graniot_sync_error": None,
            "updated_at": now(),
        })
        write_db(db)




def _sources_from_local_row(local: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return WMS-capable Graniot sources already stored in a local parcel row."""
    sources: List[Dict[str, Any]] = []
    for item in (local.get("graniot_parcels") or []):
        if isinstance(item, dict):
            sources.append({
                "graniot_parcel_id": item.get("graniot_parcel_id"),
                "graniot_parcel_key": item.get("graniot_parcel_key"),
                "graniot_access_key": item.get("graniot_access_key"),
                "graniot_wms_access_key": item.get("graniot_wms_access_key"),
                "graniot_wms_url": item.get("graniot_wms_url"),
                "graniot_image_url": item.get("graniot_image_url"),
                "graniot_bbox": item.get("graniot_bbox") or item.get("bbox"),
                "parcelresolution_set": item.get("parcelresolution_set"),
                "raw": item.get("raw") or item,
            })

    if not sources:
        raw_source = _wms_data_from_payload(
            local.get("graniot_raw"),
            access_key=local.get("graniot_wms_access_key") or local.get("graniot_access_key"),
            parcel_id=local.get("graniot_parcel_id"),
        )
        if raw_source:
            sources = [raw_source]

    # Rows synchronized by older versions may have only first-level fields.
    if not sources and (local.get("graniot_wms_url") or local.get("graniot_image_url") or local.get("graniot_access_key")):
        sources = [{
            "graniot_parcel_id": local.get("graniot_parcel_id"),
            "graniot_parcel_key": local.get("graniot_parcel_key"),
            "graniot_access_key": local.get("graniot_access_key"),
            "graniot_wms_access_key": local.get("graniot_wms_access_key"),
            "graniot_wms_url": local.get("graniot_wms_url"),
            "graniot_image_url": local.get("graniot_image_url"),
            "graniot_bbox": local.get("graniot_bbox") or local.get("bbox"),
            "parcelresolution_set": local.get("parcelresolution_set"),
            "raw": local,
        }]
    return [source for source in sources if source.get("graniot_wms_url") or source.get("graniot_image_url") or source.get("graniot_wms_access_key") or source.get("graniot_access_key")]


def _statistics_mean_value(statistics: Any) -> Optional[float]:
    def as_float(value: Any) -> Optional[float]:
        try:
            number = float(str(value).replace(",", "."))
            return number if number == number and number not in (float("inf"), float("-inf")) else None
        except Exception:
            return None

    if not isinstance(statistics, dict):
        return None

    for key in ("ndvi_mean", "mean_ndvi", "average_ndvi", "avg_ndvi", "mean", "average"):
        value = as_float(statistics.get(key))
        if value is not None:
            return value

    rows = statistics.get("data")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            basic_stats = row.get("basicStats")
            if isinstance(basic_stats, list):
                for item in basic_stats:
                    if isinstance(item, dict):
                        for key in ("mean", "average", "avg", "value"):
                            value = as_float(item.get(key))
                            if value is not None:
                                return value
            for key in ("mean", "average", "avg", "value"):
                value = as_float(row.get(key))
                if value is not None:
                    return value
    return None


def _persist_satellite_analysis_record(
    *,
    user_id: str,
    local_parcel_id: str,
    local: Dict[str, Any],
    resolved_date: Optional[str],
    resolved_layer: Dict[str, Any],
    overlays: List[Dict[str, Any]],
    statistics: Any,
    warnings: List[str],
    sources: List[Dict[str, Any]],
    render_sources: List[Dict[str, Any]],
    available_resolution_ids: List[int],
) -> None:
    layer_name = str(resolved_layer.get("wms_layer") or resolved_layer.get("family") or DEFAULT_NDVI_WMS_LAYER)
    image_date = resolved_date or datetime.now(timezone.utc).date().isoformat()
    analysis_key = _stable_hash({
        "parcel_id": local_parcel_id,
        "image_date": image_date,
        "layer": layer_name,
        "resolution_id": resolved_layer.get("resolution_id"),
        "overlay_ids": [item.get("id") for item in overlays],
    })
    t = now()
    ndvi_mean = _statistics_mean_value(statistics)
    status = "completed" if overlays else "failed"
    object_path = overlays[0].get("image_url") if overlays else f"graniot/map-layer/{analysis_key}.png"
    bounds = overlays[0].get("bounds") if overlays else local.get("geometry_bounds") or local.get("bounds") or local.get("bbox")
    compared_images = [
        {
            "overlay_id": overlay.get("id"),
            "graniot_parcel_id": overlay.get("graniot_parcel_id"),
            "image_url": overlay.get("image_url"),
            "bounds": overlay.get("bounds"),
            "date": overlay.get("date") or image_date,
            "layer": overlay.get("layer") or layer_name,
            "source": overlay.get("source"),
        }
        for overlay in overlays
    ]

    record = {
        "user_id": user_id,
        "parcel_id": local_parcel_id,
        "image_date": image_date,
        "index_type": layer_name,
        "index": layer_name,
        "image_object_path": object_path,
        "processing_status": status,
        "status": status,
        "bounds": bounds,
        "statistics": statistics if isinstance(statistics, dict) else {},
        "ndvi_mean": ndvi_mean,
        "average_ndvi": ndvi_mean,
        "source": "graniot-map-layer",
        "analysis_key": analysis_key,
        "analysis_type": "satellite_vegetation_health",
        "analysis_payload": {
            "date": image_date,
            "layer": resolved_layer,
            "overlays": compared_images,
            "warnings": warnings,
            "source_count": len(render_sources),
            "available_resolution_ids": available_resolution_ids,
            "source_parcels": [
                {
                    "graniot_parcel_id": source.get("graniot_parcel_id"),
                    "graniot_parcel_key": source.get("graniot_parcel_key"),
                    "last_image_date": _date_from_graniot_sources([source], resolved_layer.get("resolution_id")),
                }
                for source in sources
            ],
            "parcel": {
                "id": local.get("id"),
                "name": local.get("name") or local.get("lote") or local.get("finca"),
                "area": local.get("area"),
            },
        },
        "updated_at": t,
    }

    with LOCK:
        db = read_db()
        rows = table(db, "satellite_images")
        existing = next(
            (
                row for row in rows
                if str(row.get("user_id") or "") == str(user_id)
                and str(row.get("parcel_id") or "") == str(local_parcel_id)
                and row.get("analysis_key") == analysis_key
            ),
            None,
        )
        if existing:
            existing.update(record)
        else:
            rows.append({"id": str(uuid.uuid4()), "created_at": t, **record})
        write_db(db)


async def _attempt_auto_sync_for_map_layer(
    *,
    local_parcel_id: str,
    authorization: Optional[str],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Create/sync the local parcel in Graniot and recover its WMS template.

    A local Dataris polygon cannot show real NDVI until Graniot knows that
    polygon and returns properties.wms_url/properties.image_url. This function is
    intentionally called only after direct id/geometry matching failed.
    """
    try:
        sync_result = await sync_local_parcel(
            local_parcel_id,
            payload={"metadata": {"auto_sync_source": "ndvi-map-layer"}},
            authorization=authorization,
        )
        raw = (sync_result.get("data") or {}).get("graniot") if isinstance(sync_result, dict) else None
        synced_parcel = (sync_result.get("data") or {}).get("parcel") if isinstance(sync_result, dict) else None

        sources: List[Dict[str, Any]] = []
        if isinstance(synced_parcel, dict):
            sources.extend(_sources_from_local_row(synced_parcel))
        if raw:
            recovered = _wms_data_from_payload(raw)
            if recovered:
                sources.append(recovered)
            sources.extend(_all_wms_data_from_payload(raw))

        # Refresh the row after sync because sync_local_parcel persists ids and templates.
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == local_parcel_id), None)
        if row:
            sources.extend(_sources_from_local_row(row))

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for source in sources:
            key = str(source.get("graniot_parcel_id") or source.get("graniot_wms_access_key") or source.get("graniot_access_key") or source.get("graniot_wms_url") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            if source.get("graniot_wms_url") or source.get("graniot_image_url") or source.get("graniot_wms_access_key") or source.get("graniot_access_key"):
                deduped.append(source)
        if deduped:
            warnings.append("El lote local fue sincronizado automáticamente con Graniot para poder solicitar WMS real.")
        else:
            warnings.append("El lote se intentó sincronizar con Graniot, pero la respuesta no incluyó WMS/image_url todavía.")
        return deduped
    except Exception as exc:
        warnings.append(f"No se pudo sincronizar automáticamente el lote con Graniot: {exc}")
        return []

@router.get("/parcels/{local_parcel_id}/ndvi/map-layer")
@router.post("/parcels/{local_parcel_id}/ndvi/map-layer")
async def get_local_parcel_ndvi_map_layer(
    local_parcel_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    layer_key: str = Query(default=DEFAULT_NDVI_LAYER_KEY),
    wms_layer: str = Query(default=DEFAULT_NDVI_WMS_LAYER),
    resolution_id: int = Query(default=DEFAULT_NDVI_RESOLUTION_ID),
    resolution_key: str = Query(default=DEFAULT_NDVI_RESOLUTION_KEY),
    date: Optional[str] = Query(default=None),
    width: int = Query(default=1024),
    height: int = Query(default=1024),
    maxcc: float = Query(default=100),
    include_statistics: bool = Query(default=True),
    auto_sync: bool = Query(default=True),
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    payload = payload or {}
    layer_key = str(payload.get("layer_key") or payload.get("layerKey") or layer_key or DEFAULT_NDVI_LAYER_KEY)
    wms_layer = str(payload.get("wms_layer") or payload.get("wmsLayer") or wms_layer or DEFAULT_NDVI_WMS_LAYER)
    try:
        resolution_id = int(payload.get("resolution_id") or payload.get("resolutionId") or resolution_id or DEFAULT_NDVI_RESOLUTION_ID)
    except Exception:
        resolution_id = DEFAULT_NDVI_RESOLUTION_ID
    resolution_key = str(payload.get("resolution_key") or payload.get("resolutionKey") or resolution_key or DEFAULT_NDVI_RESOLUTION_KEY)
    date = payload.get("date") or date
    try:
        width = int(payload.get("width") or width)
        height = int(payload.get("height") or height)
    except Exception:
        width = height = 1024
    try:
        maxcc = float(payload.get("maxcc") if payload.get("maxcc") is not None else maxcc)
    except Exception:
        maxcc = 100
    if "include_statistics" in payload or "includeStatistics" in payload:
        include_statistics = bool(payload.get("include_statistics", payload.get("includeStatistics")))
    if "auto_sync" in payload or "autoSync" in payload:
        auto_sync = bool(payload.get("auto_sync", payload.get("autoSync")))
    force_refresh = bool(payload.get("force_refresh") or payload.get("forceRefresh"))

    with LOCK:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == local_parcel_id and p.get("user_id") == user["id"]), None)
    if not local:
        raise HTTPException(status_code=404, detail="Lote local no encontrado")
    if payload.get("geometry"):
        local = dict(local)
        local["geometry"] = payload.get("geometry")

    client = GraniotClient()
    warnings: List[str] = []

    map_cache_key = _stable_hash({
        "scope": "graniot-map-layer",
        "user_id": user.get("id"),
        "local_parcel_id": local_parcel_id,
        "layer_key": layer_key,
        "wms_layer": wms_layer,
        "resolution_id": resolution_id,
        "resolution_key": resolution_key,
        "date": date or "latest",
        "width": width,
        "height": height,
        "maxcc": maxcc,
        "include_statistics": include_statistics,
        "local_updated_at": local.get("updated_at"),
        "graniot_synced_at": local.get("graniot_synced_at"),
        "geometry_hash": _stable_hash(payload.get("geometry")) if payload.get("geometry") else None,
    })
    cached_map_layer = None if force_refresh else _cache_get(map_cache_key)
    if cached_map_layer is not None:
        cached_copy = json.loads(json.dumps(cached_map_layer, default=str))
        cached_copy.setdefault("data", {}).setdefault("cache", {})["status"] = "HIT"
        return cached_copy

    # Start with locally stored Graniot WMS sources. If none exist, recover by
    # matching the local polygon against /api/parcels/ FeatureCollection.
    sources: List[Dict[str, Any]] = _drop_sources_far_from_lot(local, _sources_from_local_row(local), warnings)

    # La instantánea guardada durante el sync queda obsoleta con el tiempo: el
    # access_key firmado ROTA (Graniot responde "Invalid access key" con el
    # viejo) y su parcelresolution_set puede decir last_image_date nulo aunque
    # Graniot ya tenga escenas procesadas. Si las fuentes guardadas no reportan
    # NINGUNA fecha de imagen, se refrescan desde /api/parcels/ igual que
    # cuando no hay fuentes; si el refresco falla, se conserva la instantánea.
    snapshot_stale = bool(sources) and _date_from_graniot_sources(sources, resolution_id) is None

    raw_parcels = None
    if not sources or snapshot_stale:
        try:
            raw_parcels = await client.get("/api/parcels/")
            matches = _find_graniot_matches_for_local(local, raw_parcels)
            fresh_sources = [_extract_wms_data_from_parcel_object(match) for match in matches]
            fresh_sources = [source for source in fresh_sources if source.get("graniot_wms_url") or source.get("graniot_image_url")]
            if fresh_sources:
                resolved_date = date or _date_from_graniot_sources(fresh_sources, resolution_id)
                _persist_graniot_sources(local_parcel_id, user["id"], raw_parcels, fresh_sources, resolved_date)
                date = resolved_date
                sources = fresh_sources
            elif not sources:
                warnings.append("No se encontró un lote equivalente en Graniot para esta geometría local.")
        except Exception as exc:
            if sources:
                warnings.append(f"No se pudo refrescar el lote desde Graniot; se usa la última instantánea guardada: {exc}")
            else:
                warnings.append(f"No se pudo recuperar el lote desde Graniot: {exc}")

    if not sources and auto_sync:
        sources = await _attempt_auto_sync_for_map_layer(
            local_parcel_id=local_parcel_id,
            authorization=authorization,
            warnings=warnings,
        )

    if not sources:
        return _cache_set(map_cache_key, {
            "data": {
                "available": False,
                "reason": "Este lote todavía no tiene WMS real de Graniot. No está sincronizado en Graniot o Graniot aún no ha generado image_url/wms_url para esa geometría.",
                "requires_sync": True,
                "overlays": [],
                "warnings": warnings,
                "cache": {"status": "MISS", "ttl_seconds": min(120, GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS)},
            },
            "error": None,
        }, min(120, GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS))

    resolved_layer = _resolve_requested_graniot_layer(
        layer_key=layer_key or DEFAULT_NDVI_LAYER_KEY,
        wms_layer=wms_layer or DEFAULT_NDVI_WMS_LAYER,
        resolution_id=resolution_id,
        resolution_key=resolution_key,
        sources=sources,
        warnings=warnings,
    )
    resolved_resolution_id = resolved_layer.get("resolution_id")

    # Prefer the latest image date actually reported by Graniot for the selected
    # resolution. This avoids asking WMS for the UI calendar date when no image
    # exists there, which can produce misleading single-color rasters.
    graniot_latest_date = _date_from_graniot_sources(sources, resolved_resolution_id)
    # Si el frontend manda una fecha explícita desde el calendario, se respeta.
    # Antes se reemplazaba por la última fecha reportada por Graniot, por eso
    # cambiar fecha podía no cambiar el raster visible.
    resolved_date = date or graniot_latest_date

    sources = _drop_sources_far_from_lot(local, sources, warnings)
    render_sources = [source for source in sources if _source_has_image_for_resolution(source, resolved_resolution_id)]
    if not render_sources:
        render_sources = sources
        if resolved_resolution_id is not None:
            warnings.append(f"El lote no reporta imagen para la resolución {resolved_resolution_id}; se intentará renderizar con la información WMS disponible.")

    # Sin fecha del calendario, se prefiere la escena LIMPIA más reciente sobre
    # la última reportada, que puede estar tapada por nubes (el «rojo plano»).
    # La elección la comparte el wms-proxy, así la etiqueta de fecha, las
    # estadísticas y la imagen pintada corresponden a la misma escena.
    if not date and render_sources:
        scene_source = render_sources[0]
        scene_access_key = (
            scene_source.get("graniot_wms_access_key")
            or _signed_wms_access_key(scene_source.get("graniot_wms_url"))
            or _signed_wms_access_key(scene_source.get("graniot_image_url"))
            or scene_source.get("graniot_access_key")
        )
        scene_graniot_id = scene_source.get("graniot_parcel_id") or local.get("graniot_parcel_id")
        try:
            clear_scene = await _choose_clear_scene_date(
                client,
                parcel_token=local_parcel_id,
                access_key=scene_access_key,
                layer=resolved_layer.get("wms_layer") or DEFAULT_NDVI_WMS_LAYER,
                graniot_parcel_id=str(scene_graniot_id) if scene_graniot_id else None,
                resolution_key=resolved_layer.get("resolution_key"),
                wms_path=_wms_path_from_template(scene_source.get("graniot_wms_url")),
            )
        except Exception:
            clear_scene = None
        if clear_scene and clear_scene != resolved_date:
            warnings.append(
                f"La escena más reciente ({resolved_date or 'sin fecha'}) está cubierta de nubes; "
                f"se muestra la del {clear_scene}, la última con datos útiles."
            )
            resolved_date = clear_scene

    overlays = [
        overlay for overlay in (
            _source_to_map_overlay(
                local_parcel_id=local_parcel_id,
                source=source,
                layer_name=resolved_layer.get("wms_layer") or DEFAULT_NDVI_WMS_LAYER,
                date=resolved_date,
                width=width,
                height=height,
            ) for source in render_sources
        ) if overlay
    ]

    statistics: Any = None
    graniot_parcel_id = render_sources[0].get("graniot_parcel_id") or sources[0].get("graniot_parcel_id") or local.get("graniot_parcel_id")
    if include_statistics and graniot_parcel_id and resolved_layer.get("key"):
        try:
            to_date = resolved_date or datetime.now(timezone.utc).date().isoformat()
            statistics = await client.get(
                f"/api/parcels/{graniot_parcel_id}/layers/{resolved_layer.get('key')}/statistics/",
                params={"from_date": "2020-01-01", "to_date": to_date, "maxcc": maxcc},
            )
        except Exception as exc:
            statistics = {"status": "unavailable", "data": [], "warning": str(exc)}

    if not overlays:
        warnings.append("Graniot devolvió el lote, pero no hay access_key o bounds válidos para construir la imagen WMS.")

    available_resolution_ids = sorted(_available_resolution_ids_with_images(sources))
    _persist_satellite_analysis_record(
        user_id=str(user["id"]),
        local_parcel_id=local_parcel_id,
        local=local,
        resolved_date=resolved_date,
        resolved_layer=resolved_layer,
        overlays=overlays,
        statistics=statistics,
        warnings=warnings,
        sources=sources,
        render_sources=render_sources,
        available_resolution_ids=available_resolution_ids,
    )

    response_payload = {
        "data": {
            "available": bool(overlays),
            "date": resolved_date,
            "layer": {
                "key": resolved_layer.get("key"),
                "wms_layer": resolved_layer.get("wms_layer") or DEFAULT_NDVI_WMS_LAYER,
                "resolution_id": resolved_layer.get("resolution_id"),
                "resolution_key": resolved_layer.get("resolution_key"),
                "resolution_label": resolved_layer.get("resolution_label"),
                "source": resolved_layer.get("source"),
                "family": resolved_layer.get("family"),
                "auto_selected": resolved_layer.get("auto_selected"),
            },
            "overlays": overlays,
            "statistics": statistics,
            "warnings": warnings,
            "source_count": len(render_sources),
            "available_resolution_ids": available_resolution_ids,
            "cache": {"status": "MISS", "ttl_seconds": GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS},
        },
        "error": None,
    }
    return _cache_set(map_cache_key, response_payload, GRANIOT_MAP_LAYER_CACHE_TTL_SECONDS)


@router.get("/parcels/{parcel_id}/resolutions/{resolution_key}/dates")
async def get_dates(
    parcel_id: str,
    resolution_key: str,
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    cache_key = _stable_hash({"scope": "graniot-dates", "parcel_id": parcel_id, "resolution_key": resolution_key})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = GraniotClient()
    try:
        raw = await client.get(f"/api/parcels/{parcel_id}/resolutions/{resolution_key}/dates/")
        return _cache_set(cache_key, {"data": raw, "error": None, "cache": {"status": "MISS", "ttl_seconds": GRANIOT_DATE_CACHE_TTL_SECONDS}}, GRANIOT_DATE_CACHE_TTL_SECONDS)
    except GraniotAPIError as exc:
        # Some Graniot layers/resolutions simply do not expose a date catalog.
        # Returning 200 with an empty list avoids noisy browser 404/500 errors;
        # the frontend will render the layer with the latest image instead.
        if exc.status_code in {400, 404, 500, 502}:
            return _cache_set(cache_key, {"data": [], "error": str(exc), "warning": True, "cache": {"status": "MISS", "ttl_seconds": min(900, GRANIOT_DATE_CACHE_TTL_SECONDS)}}, min(900, GRANIOT_DATE_CACHE_TTL_SECONDS))
        _raise_graniot_error(exc)
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/parcels/{parcel_id}/layers/{layer_key}/json-index")
async def get_json_index(
    parcel_id: str,
    layer_key: str,
    date: str = Query(...),
    nbins: int = Query(default=5),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    client = GraniotClient()
    try:
        raw = await client.get(
            f"/api/parcels/{parcel_id}/layers/{layer_key}/json-index/",
            params={"date": date, "nbins": nbins},
        )
        return {"data": raw, "error": None}
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/parcels/{parcel_id}/layers/{layer_key}/statistics")
async def get_layer_statistics(
    parcel_id: str,
    layer_key: str,
    from_date: str = Query(...),
    to_date: str = Query(...),
    maxcc: float = Query(default=80),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    cache_key = _stable_hash({"scope": "graniot-statistics", "parcel_id": parcel_id, "layer_key": layer_key, "from_date": from_date, "to_date": to_date, "maxcc": maxcc})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = GraniotClient()
    try:
        raw = await client.get(
            f"/api/parcels/{parcel_id}/layers/{layer_key}/statistics/",
            params={"from_date": from_date, "to_date": to_date, "maxcc": maxcc},
        )
        return _cache_set(cache_key, {"data": raw, "error": None, "cache": {"status": "MISS", "ttl_seconds": GRANIOT_STATS_CACHE_TTL_SECONDS}}, GRANIOT_STATS_CACHE_TTL_SECONDS)
    except GraniotAPIError as exc:
        # Statistics are optional in Graniot and can fail for layers that still
        # render correctly as WMS. Do not block the satellite map for this.
        if exc.status_code in {400, 404, 500, 502}:
            return _cache_set(cache_key, {"data": {"status": "unavailable", "data": []}, "error": str(exc), "warning": True, "cache": {"status": "MISS", "ttl_seconds": min(900, GRANIOT_STATS_CACHE_TTL_SECONDS)}}, min(900, GRANIOT_STATS_CACHE_TTL_SECONDS))
        _raise_graniot_error(exc)
    except Exception as exc:
        _raise_graniot_error(exc)




def _query_first(params: Dict[str, List[str]], key: str, default: Optional[str] = None) -> Optional[str]:
    values = params.get(key)
    if not values:
        return default
    return values[0]


async def _warm_wms_overlay_from_url(image_url: str) -> Dict[str, Any]:
    parsed = urlparse(image_url)
    params = parse_qs(parsed.query)
    started = time_module.perf_counter()
    await _wms_proxy_impl(
        parcel_id=_query_first(params, "parcel_id"),
        access_key=_query_first(params, "access_key"),
        layer=_query_first(params, "layer", DEFAULT_NDVI_WMS_LAYER) or DEFAULT_NDVI_WMS_LAYER,
        time=_query_first(params, "time"),
        width=int(_query_first(params, "width", "1024") or 1024),
        height=int(_query_first(params, "height", "1024") or 1024),
        bbox=_query_first(params, "bbox"),
        south=float(_query_first(params, "south")) if _query_first(params, "south") else None,
        west=float(_query_first(params, "west")) if _query_first(params, "west") else None,
        north=float(_query_first(params, "north")) if _query_first(params, "north") else None,
        east=float(_query_first(params, "east")) if _query_first(params, "east") else None,
        cache_key=_query_first(params, "cache_key"),
    )
    return {"ok": True, "duration_ms": round((time_module.perf_counter() - started) * 1000, 2)}


@router.post("/satellite/prefetch")
async def prefetch_satellite_cache(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Warm Graniot metadata and WMS image cache without blocking the map UI.

    The frontend calls this endpoint in the background. It resolves the same
    map-layer payload used by the UI and then asks the local WMS proxy to fetch
    each raster once. Subsequent ImageOverlay requests are served from /tmp cache
    on the Cloud Run instance instead of hitting Graniot again.
    """
    user = _require_user(authorization)
    requested_ids = payload.get("parcel_ids") or payload.get("parcelIds") or []
    if isinstance(requested_ids, str):
        requested_ids = [requested_ids]
    requested_ids = [str(value) for value in requested_ids if value]

    max_parcels = max(1, min(int(payload.get("max_parcels") or payload.get("maxParcels") or 8), 24))
    layer_key = str(payload.get("layer_key") or payload.get("layerKey") or DEFAULT_NDVI_LAYER_KEY)
    wms_layer = str(payload.get("wms_layer") or payload.get("wmsLayer") or DEFAULT_NDVI_WMS_LAYER)
    resolution_id = int(payload.get("resolution_id") or payload.get("resolutionId") or DEFAULT_NDVI_RESOLUTION_ID)
    resolution_key = str(payload.get("resolution_key") or payload.get("resolutionKey") or DEFAULT_NDVI_RESOLUTION_KEY)
    date = payload.get("date") or None
    width = max(128, min(int(payload.get("width") or 1024), 2048))
    height = max(128, min(int(payload.get("height") or 1024), 2048))

    with LOCK:
        db = read_db()
        user_parcels = [p for p in table(db, "parcels") if p.get("user_id") == user["id"]]

    if requested_ids:
        selected = [p for p in user_parcels if str(p.get("id")) in set(requested_ids)]
    else:
        selected = [p for p in user_parcels if _sources_from_local_row(p)]

    selected = selected[:max_parcels]
    if not selected:
        return {"data": {"queued": False, "parcel_count": 0, "image_count": 0, "results": []}, "error": None}

    semaphore = asyncio.Semaphore(max(1, GRANIOT_WMS_PREFETCH_CONCURRENCY))
    results: List[Dict[str, Any]] = []

    async def warm_parcel(parcel: Dict[str, Any]) -> Dict[str, Any]:
        parcel_id = str(parcel.get("id"))
        try:
            layer_payload = await get_local_parcel_ndvi_map_layer(
                parcel_id,
                layer_key=layer_key,
                wms_layer=wms_layer,
                resolution_id=resolution_id,
                resolution_key=resolution_key,
                date=str(date) if date else None,
                width=width,
                height=height,
                maxcc=100,
                include_statistics=False,
                auto_sync=False,
                authorization=authorization,
            )
            data = layer_payload.get("data") if isinstance(layer_payload, dict) else {}
            overlays = data.get("overlays") if isinstance(data, dict) else []
            overlays = overlays if isinstance(overlays, list) else []
            warmed: List[Dict[str, Any]] = []
            for overlay in overlays[:4]:
                image_url = overlay.get("image_url") if isinstance(overlay, dict) else None
                if not image_url:
                    continue
                async with semaphore:
                    try:
                        warmed.append(await _warm_wms_overlay_from_url(str(image_url)))
                    except Exception as exc:
                        warmed.append({"ok": False, "message": str(exc), "exception_type": type(exc).__name__})
            return {"parcel_id": parcel_id, "available": bool(data.get("available")), "image_count": len(warmed), "images": warmed}
        except Exception as exc:
            return {"parcel_id": parcel_id, "available": False, "image_count": 0, "error": str(exc), "exception_type": type(exc).__name__}

    # Warm a handful of parcels concurrently. The frontend does not wait for this
    # request, but keeping it real work instead of a detached task avoids relying
    # on Cloud Run CPU after response.
    results = await asyncio.gather(*(warm_parcel(parcel) for parcel in selected))
    image_count = sum(int(item.get("image_count") or 0) for item in results if isinstance(item, dict))
    return {
        "data": {
            "queued": False,
            "parcel_count": len(selected),
            "image_count": image_count,
            "results": results,
            "cache_dir": str(_WMS_CACHE_DIR),
        },
        "error": None,
    }


@router.get("/wms-url")
def build_wms_proxy_url(
    parcel_id: Optional[str] = Query(default=None),
    access_key: Optional[str] = Query(default=None),
    layer: str = Query(...),
    time: Optional[str] = Query(default=None),
    width: int = Query(default=768),
    height: int = Query(default=768),
    south: Optional[float] = Query(default=None),
    north: Optional[float] = Query(default=None),
    west: Optional[float] = Query(default=None),
    east: Optional[float] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    if not access_key and parcel_id:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
        if local:
            template = _wms_template_from_local(local)
            access_key = _signed_wms_access_key(template) or local.get("graniot_wms_access_key") or local.get("graniot_access_key") or local.get("graniot_parcel_key")
    if not access_key:
        raise HTTPException(status_code=400, detail="El lote no está sincronizado con Graniot o no tiene access_key")

    params: Dict[str, Any] = {
        "parcel_id": parcel_id or "",
        "access_key": access_key,
        "layer": layer,
        "width": width,
        "height": height,
    }
    if time:
        params["time"] = time
    bbox = _bbox_from_bounds(south, west, north, east)
    if bbox:
        params.update({"south": south, "west": west, "north": north, "east": east})

    return {"data": {"url": f"/api/graniot/wms-proxy?{urlencode(params)}"}, "error": None}


@router.get("/wms-proxy")
async def wms_proxy(
    parcel_id: Optional[str] = Query(default=None),
    access_key: Optional[str] = Query(default=None),
    layer: str = Query(...),
    time: Optional[str] = Query(default=None),
    width: int = Query(default=768),
    height: int = Query(default=768),
    bbox: Optional[str] = Query(default=None),
    south: Optional[float] = Query(default=None),
    north: Optional[float] = Query(default=None),
    west: Optional[float] = Query(default=None),
    east: Optional[float] = Query(default=None),
    cache_key: Optional[str] = Query(default=None),
):
    request_id = uuid.uuid4().hex[:12]
    started = time_module.perf_counter()
    _wms_cloud_log(
        logging.WARNING,
        "start",
        request_id=request_id,
        parcel_id=parcel_id,
        layer=layer,
        time=time,
        width=width,
        height=height,
        has_bbox_param=bool(bbox),
        bounds={"south": south, "west": west, "north": north, "east": east},
        access_key=_wms_token_info(access_key),
    )
    try:
        response = await _wms_proxy_impl(
            parcel_id=parcel_id,
            access_key=access_key,
            layer=layer,
            time=time,
            width=width,
            height=height,
            bbox=bbox,
            south=south,
            north=north,
            west=west,
            east=east,
            cache_key=cache_key,
        )
        duration_ms = round((time_module_perf_counter() - started) * 1000, 2)
        _wms_cloud_log(
            logging.WARNING,
            "success",
            request_id=request_id,
            parcel_id=parcel_id,
            layer=layer,
            time=time,
            duration_ms=duration_ms,
            response_media_type=getattr(response, "media_type", None),
        )
        try:
            if isinstance(response, Response):
                response.headers["X-Dataris-WMS-Request-ID"] = request_id
        except Exception:
            pass
        return response
    except HTTPException as exc:
        duration_ms = round((time_module_perf_counter() - started) * 1000, 2)
        _wms_cloud_log(
            logging.ERROR if int(getattr(exc, "status_code", 500) or 500) >= 500 else logging.WARNING,
            "http_exception",
            request_id=request_id,
            parcel_id=parcel_id,
            layer=layer,
            time=time,
            status_code=exc.status_code,
            duration_ms=duration_ms,
            detail=_wms_json_safe(exc.detail),
        )
        if int(getattr(exc, "status_code", 500) or 500) >= 500:
            raise HTTPException(
                status_code=exc.status_code,
                detail=_wms_http_detail_with_request_id(exc.detail, request_id),
            ) from exc
        raise
    except Exception as exc:
        duration_ms = round((time_module_perf_counter() - started) * 1000, 2)
        _wms_cloud_exception(
            "unhandled_exception",
            exc,
            request_id=request_id,
            parcel_id=parcel_id,
            layer=layer,
            time=time,
            duration_ms=duration_ms,
            bounds={"south": south, "west": west, "north": north, "east": east},
            access_key=_wms_token_info(access_key),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Error interno en WMS proxy. Busca este request_id en Cloud Run con WMS_PROXY.",
                "request_id": request_id,
                "exception_type": type(exc).__name__,
                "error": str(exc),
            },
        ) from exc


# Alias used so the wrapper above can log every failure with a request_id and full traceback.
time_module_perf_counter = time_module.perf_counter


def _graniot_refs_of_local_row(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Pares (id de Graniot, parcel_key) del lote y de sus subparcelas."""
    refs: List[Tuple[str, str]] = [(
        str(row.get("graniot_parcel_id") or ""),
        _normalized_token(row.get("graniot_parcel_key")),
    )]
    subparcels = row.get("graniot_parcels")
    if isinstance(subparcels, list):
        for item in subparcels:
            if not isinstance(item, dict):
                continue
            refs.append((
                str(item.get("graniot_parcel_id") or item.get("id") or ""),
                _normalized_token(item.get("graniot_parcel_key") or item.get("key")),
            ))
    return refs


def _find_local_parcel_for_wms(parcel_id: Optional[str], access_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Localiza el lote de Dataris al que pertenece una petición WMS.

    El identificador que llega en la URL no siempre es el id local: el módulo
    Satélite envía el id de la parcela **en Graniot**. Buscando solo por el id
    local nos quedábamos sin fila, y sin fila no hay plantilla ni forma de
    renovar la access_key: Graniot rechazaba los 32 intentos con
    "Invalid access key." y el usuario recibía un 502 opaco.

    Por eso buscamos también por los identificadores de Graniot (del lote y de
    sus subparcelas) y por el parcel_key que viaja dentro de la propia
    access_key firmada, que es el dato más fiable porque lo emite Graniot.
    """
    if not parcel_id and not access_key:
        return None

    db = read_db()
    rows = table(db, "parcels")

    wanted_id = str(parcel_id or "").strip()
    if wanted_id:
        row = next((p for p in rows if str(p.get("id") or "") == wanted_id), None)
        if row:
            return row

    wanted_key = _normalized_token(
        _parcel_key_from_signed_wms_access_key(access_key)
        or (access_key if _is_uuid_like(access_key) else "")
    )
    if not wanted_id and not wanted_key:
        return None

    for row in rows:
        for graniot_id, parcel_key in _graniot_refs_of_local_row(row):
            if wanted_id and graniot_id and graniot_id == wanted_id:
                return row
            if wanted_key and parcel_key and parcel_key == wanted_key:
                return row
    return None


# --- Elección de escena por calidad -----------------------------------------
#
# Graniot sirve siempre la escena más reciente cuando la petición WMS no lleva
# `time`, y su parámetro MAXCC no filtra nada (verificado en vivo): una pasada
# nublada produce un raster casi monocolor —el «rojo plano» que el cliente veía
# como avería—. La escena buena suele estar unos días atrás, y Graniot sí la
# sirve si se le pide con `time=YYYY-MM-DD`.
#
# Estrategia: cuando el llamador no fija fecha, se recorren las fechas reales
# del catálogo del lote (de la más nueva a la más vieja) sondeando una imagen
# pequeña por fecha, y gana la primera que no esté plana. La elección se
# recuerda unas horas por lote+capa para que el sondeo no se repita en cada
# carga. Una fecha pedida explícitamente por el usuario NUNCA se toca.

SCENE_CHOICE_TTL_SECONDS = int(os.environ.get("GRANIOT_SCENE_CHOICE_TTL_SECONDS", str(6 * 3600)))
SCENE_CHOICE_NEGATIVE_TTL_SECONDS = int(os.environ.get("GRANIOT_SCENE_CHOICE_NEGATIVE_TTL_SECONDS", str(2 * 3600)))
# Umbral medido sobre lotes reales: las escenas nubladas dan 6-10 colores y las
# despejadas 96 o más; 25 deja margen por ambos lados.
SCENE_FLAT_MAX_COLORS = int(os.environ.get("GRANIOT_SCENE_FLAT_MAX_COLORS", "25"))
SCENE_PROBE_MAX_DATES = int(os.environ.get("GRANIOT_SCENE_PROBE_MAX_DATES", "6"))
SCENE_PROBE_SIZE = 256


def _scene_choice_cache_key(parcel_token: Optional[str], access_key: Optional[str], layer: Optional[str]) -> str:
    """Clave estable por lote+capa: el parcel_key firmado no cambia al re-firmar."""
    token = _normalized_token(
        str(parcel_token or "")
        or _parcel_key_from_signed_wms_access_key(access_key)
        or str(access_key or "")
    )
    return _stable_hash({"scope": "graniot-scene-choice", "parcel": token, "layer": str(layer or "").strip().upper()})


def _scene_looks_flat(content: Optional[bytes]) -> bool:
    """¿El raster es casi monocolor? Señal de escena nublada.

    Solo devuelve True con certeza (hay píxeles visibles y muy pocos colores):
    ante cualquier duda —imagen ilegible, vacía, sin píxeles— responde False
    para no descartar escenas válidas.
    """
    if not content:
        return False
    try:
        import io as _io

        from PIL import Image

        with Image.open(_io.BytesIO(content)) as raw_image:
            image = raw_image.convert("RGBA")
        # NEAREST: un reescalado con interpolación mezcla el borde del polígono
        # con la transparencia e inventa cientos de colores intermedios, y una
        # escena nublada real dejaba de parecer plana.
        image.thumbnail((SCENE_PROBE_SIZE, SCENE_PROBE_SIZE), Image.NEAREST)
        colors: set = set()
        visible = 0
        for pixel in image.getdata():
            # Solo los píxeles opacos: el borde anti-aliased no es dato.
            if pixel[3] < 200:
                continue
            visible += 1
            # En cubetas de 8 niveles: el ruido de compresión no cuenta como
            # color; la diferencia real entre 6-10 colores (nubes) y ~100
            # (escena útil) sobrevive de sobra.
            colors.add((pixel[0] // 8, pixel[1] // 8, pixel[2] // 8))
            if len(colors) >= SCENE_FLAT_MAX_COLORS:
                return False
        return visible > 0
    except Exception:
        return False


async def _list_scene_dates(client: GraniotClient, graniot_parcel_id: Optional[str], resolution_key: Optional[str]) -> List[str]:
    """Fechas con imagen del lote, de la más nueva a la más vieja."""
    if not graniot_parcel_id:
        return []
    resolution = str(resolution_key or DEFAULT_NDVI_RESOLUTION_KEY).strip() or DEFAULT_NDVI_RESOLUTION_KEY
    try:
        raw = await client.get(
            f"/api/parcels/{graniot_parcel_id}/resolutions/{resolution}/dates/",
            debug_context={"operation": "scene-choice-dates", "graniot_parcel_id": graniot_parcel_id},
        )
    except Exception:
        return []
    items = raw.get("data") if isinstance(raw, dict) else raw
    dates: List[str] = []
    for item in items if isinstance(items, list) else []:
        value = item.get("date") if isinstance(item, dict) else item
        if value:
            dates.append(str(value)[:10])
    return dates


async def _probe_scene(
    client: GraniotClient,
    *,
    wms_path: str,
    access_key: str,
    layer: str,
    scene_date: str,
) -> Optional[bytes]:
    """Imagen pequeña de una fecha concreta; None si Graniot no da un raster."""
    params = _clean_wms_params({
        "access_key": access_key,
        "layers": layer,
        "response_format": "image/png",
        "time": scene_date,
        "width": SCENE_PROBE_SIZE,
        "height": SCENE_PROBE_SIZE,
    })
    try:
        raw = await client.binary_get(
            wms_path,
            params=params,
            use_auth=False,
            include_client_id=False,
            debug_context={"operation": "scene-choice-probe", "layer": layer, "time": scene_date},
        )
    except Exception:
        return None
    if _is_image_response(raw):
        return raw.content
    return None


async def _choose_clear_scene_date(
    client: GraniotClient,
    *,
    parcel_token: Optional[str],
    access_key: Optional[str],
    layer: Optional[str],
    graniot_parcel_id: Optional[str],
    resolution_key: Optional[str] = None,
    wms_path: str = "/api/wms/",
    latest_content: Optional[bytes] = None,
) -> Optional[str]:
    """Fecha de la primera escena útil, o None para conservar el comportamiento
    de siempre (última escena).

    `latest_content`, si viene, es el raster de la escena más reciente ya
    descargado: si no está plano no hay nada que elegir. Sin él, se sondea
    también la fecha más nueva del catálogo.

    El resultado (incluido el negativo «todas nubladas») se cachea por
    lote+capa: el sondeo cuesta una petición pequeña a Graniot por fecha.
    """
    clean_layer = str(layer or "").strip()
    clean_key = str(access_key or "").strip()
    if not clean_layer or not clean_key or not graniot_parcel_id:
        return None

    choice_key = _scene_choice_cache_key(parcel_token, clean_key, clean_layer)
    cached = _cache_get(choice_key)
    if isinstance(cached, dict):
        return cached.get("date") or None

    if latest_content is not None and not _scene_looks_flat(latest_content):
        _cache_set(choice_key, {"date": ""}, SCENE_CHOICE_TTL_SECONDS)
        return None

    dates = await _list_scene_dates(client, graniot_parcel_id, resolution_key)
    candidates = dates[: SCENE_PROBE_MAX_DATES + 1]
    if latest_content is not None and candidates:
        # La más nueva ya se descargó y estaba plana: no se sondea dos veces.
        candidates = candidates[1:]

    chosen: Optional[str] = None
    probed = 0
    for scene_date in candidates:
        if probed >= SCENE_PROBE_MAX_DATES:
            break
        probed += 1
        content = await _probe_scene(
            client,
            wms_path=wms_path,
            access_key=clean_key,
            layer=clean_layer,
            scene_date=scene_date,
        )
        if content is None:
            continue
        if not _scene_looks_flat(content):
            chosen = scene_date
            break

    log_event({
        "event": "dataris.graniot.scene_choice",
        "operation": "scene-choice",
        "graniot_parcel_id": graniot_parcel_id,
        "layer": clean_layer,
        "dates_available": len(dates),
        "probed": probed,
        "chosen": chosen,
    })
    _cache_set(
        choice_key,
        {"date": chosen or ""},
        SCENE_CHOICE_TTL_SECONDS if chosen else SCENE_CHOICE_NEGATIVE_TTL_SECONDS,
    )
    return chosen


async def _wms_proxy_impl(
    parcel_id: Optional[str] = None,
    access_key: Optional[str] = None,
    layer: str = "",
    time: Optional[str] = None,
    width: int = 768,
    height: int = 768,
    bbox: Optional[str] = None,
    south: Optional[float] = None,
    north: Optional[float] = None,
    west: Optional[float] = None,
    east: Optional[float] = None,
    cache_key: Optional[str] = None,
):
    """Proxy WMS for Leaflet ImageOverlay.

    Leaflet cannot attach secure Graniot headers to an image request. This
    endpoint asks Graniot for the raster server-side and returns the PNG/JPEG to
    the browser. It first reuses the exact image_url/Geometry returned by
    Graniot, then falls back to BBOX variants for older local rows.
    """
    clean_layer = str(layer or "").strip()
    if not clean_layer:
        raise HTTPException(status_code=400, detail="layer requerido")

    # Keep the requested image size in a safe range. Huge WMS images can fail or
    # make the browser look like the layer is broken.
    width = max(128, min(int(width or 768), 2048))
    height = max(128, min(int(height or 768), 2048))

    # Sin fecha pedida, se respeta la escena limpia ya elegida para este
    # lote+capa (si la hay): así el disco cachea directamente la imagen buena.
    # Va antes de calcular la clave de caché para que la clave lleve la fecha.
    caller_pinned_time = bool(str(time or "").strip())
    if not caller_pinned_time:
        cached_scene_choice = _cache_get(_scene_choice_cache_key(parcel_id, access_key, clean_layer))
        if isinstance(cached_scene_choice, dict) and cached_scene_choice.get("date"):
            time = cached_scene_choice["date"]

    stable_wms_cache_key = _wms_cache_public_key(
        cache_key=cache_key,
        parcel_id=parcel_id,
        access_key=access_key,
        layer=clean_layer,
        time=time,
        width=width,
        height=height,
        bbox=bbox,
        south=south,
        west=west,
        north=north,
        east=east,
    )
    cached_wms = _read_wms_disk_cache(stable_wms_cache_key)
    if cached_wms is not None:
        _wms_cloud_log(logging.WARNING, "cache_hit", parcel_id=parcel_id, layer=clean_layer, time=time, cache_key=stable_wms_cache_key)
        return cached_wms

    # read_db() carga el JSON completo de la plataforma desde Neon de forma
    # síncrona: en el hilo del event loop congelaba /health y Azure reiniciaba
    # el contenedor con las imágenes a medio servir.
    local: Optional[Dict[str, Any]] = await run_in_threadpool(_find_local_parcel_for_wms, parcel_id, access_key)
    if local and not access_key:
        access_key = (
            local.get("graniot_wms_access_key")
            or local.get("graniot_access_key")
            or local.get("graniot_parcel_key")
        )

    access_key = str(access_key or "").strip()
    if not access_key:
        raise HTTPException(status_code=400, detail="access_key requerido")

    client = GraniotClient()

    local_graniot_parcel_id = _graniot_parcel_id_for_wms_request(local, access_key)
    template = _wms_template_from_local(
        local,
        access_key=access_key,
        graniot_parcel_id=local_graniot_parcel_id,
    )
    recovered_wms_data: Optional[Dict[str, Any]] = None

    template_params = _query_params_from_wms_template(template)
    signed_template_access_key = _signed_wms_access_key(template) or _signed_wms_access_key(template_params.get("access_key") or template_params.get("ACCESS_KEY"))

    # Rows synchronized before this fix can have a template with Geometry/BBOX
    # but no signed WMS token, or can receive the UUID-like parcel key from the
    # frontend. In both cases /api/wms/ returns {"Invalid access key."}.
    #
    # Important: Graniot signed WMS access_key values can also expire even when
    # they still look structurally valid. The browser URL may therefore contain
    # a signed token that decodes correctly but Graniot rejects with
    # "Invalid access key.". To avoid leaving the satellite layer broken, refresh
    # the WMS metadata from Graniot once per proxy request when a local parcel is
    # available, then prefer the freshly recovered signed key over the key that
    # came from the frontend/local cache.
    incoming_parcel_key_from_signed = _parcel_key_from_signed_wms_access_key(access_key)
    template_parcel_key_from_signed = _parcel_key_from_signed_wms_access_key(signed_template_access_key)
    template_mismatch = bool(
        incoming_parcel_key_from_signed
        and template_parcel_key_from_signed
        and _normalized_token(incoming_parcel_key_from_signed) != _normalized_token(template_parcel_key_from_signed)
    )

    recovery_reasons: List[str] = []
    if not signed_template_access_key:
        recovery_reasons.append("missing_signed_template_key")
    if template_mismatch:
        recovery_reasons.append("template_signed_key_mismatch")
    if _is_uuid_like(access_key):
        recovery_reasons.append("uuid_like_access_key")
    if signed_template_access_key and not _is_uuid_like(access_key):
        recovery_reasons.append("refresh_possible_expired_signed_key")

    needs_signed_recovery = bool(local) and bool(recovery_reasons) and (bool(access_key) or bool(local_graniot_parcel_id))
    if needs_signed_recovery:
        _wms_cloud_log(
            logging.WARNING,
            "refresh_wms_metadata",
            parcel_id=parcel_id,
            layer=clean_layer,
            time=time,
            reasons=recovery_reasons,
            local_graniot_parcel_id=local_graniot_parcel_id,
            request_access_key=_wms_token_info(access_key),
            template_access_key=_wms_token_info(signed_template_access_key),
        )
        recovered_wms_data = await _recover_wms_data_shared(
            client,
            access_key=access_key,
            graniot_parcel_id=local_graniot_parcel_id,
        )
        if recovered_wms_data:
            recovered_template = recovered_wms_data.get("graniot_wms_url")
            if isinstance(recovered_template, str) and recovered_template.strip():
                template = recovered_template.strip()
                template_params = _query_params_from_wms_template(template)
                signed_template_access_key = _signed_wms_access_key(template) or _signed_wms_access_key(template_params.get("access_key") or template_params.get("ACCESS_KEY"))
            if not signed_template_access_key:
                recovered_key = recovered_wms_data.get("graniot_wms_access_key") or recovered_wms_data.get("graniot_access_key")
                if recovered_key and not _is_uuid_like(recovered_key):
                    signed_template_access_key = str(recovered_key).strip()
            _store_recovered_wms_data_in_background(local, recovered_wms_data)
            _wms_cloud_log(
                logging.WARNING,
                "refresh_wms_metadata_success",
                parcel_id=parcel_id,
                layer=clean_layer,
                time=time,
                recovered_template=bool(recovered_template),
                recovered_access_key=_wms_token_info(signed_template_access_key),
                recovered_parcel_id=recovered_wms_data.get("graniot_parcel_id"),
                recovered_parcel_key=recovered_wms_data.get("graniot_parcel_key"),
            )
        else:
            _wms_cloud_log(
                logging.WARNING,
                "refresh_wms_metadata_empty",
                parcel_id=parcel_id,
                layer=clean_layer,
                time=time,
                reasons=recovery_reasons,
                local_graniot_parcel_id=local_graniot_parcel_id,
            )

    # Prefer a freshly recovered signed key because the access_key sent by the
    # frontend can be expired even if it still has a valid-looking signed format.
    # If recovery did not find a new key, keep the previous behavior.
    if recovered_wms_data and signed_template_access_key:
        access_key = _choose_wms_access_key(signed_template_access_key, access_key)
    else:
        access_key = _choose_wms_access_key(access_key, signed_template_access_key)
    wms_path = _wms_path_from_template(template)

    bbox_values = _bbox_values_from_bounds(south, west, north, east)
    if not bbox_values and recovered_wms_data:
        bbox_values = _bbox_from_graniot_bbox(recovered_wms_data.get("graniot_bbox"))

    bbox_latlon = bbox
    if not bbox_latlon and bbox_values:
        bbox_latlon = f"{bbox_values['south']},{bbox_values['west']},{bbox_values['north']},{bbox_values['east']}"
    elif not bbox_latlon:
        bbox_latlon = _bbox_from_bounds(south, west, north, east)

    bbox_lonlat = None
    if bbox_values:
        bbox_lonlat = f"{bbox_values['west']},{bbox_values['south']},{bbox_values['east']},{bbox_values['north']}"
    else:
        bbox_lonlat = _bbox_lonlat_from_bounds(south, west, north, east)

    layer_candidates = _layer_identifier_candidates(clean_layer)
    variants: List[Dict[str, Any]] = []
    for candidate_layer in layer_candidates:
        variants.extend(_build_wms_param_variants(
            template_params=template_params,
            access_key=access_key,
            layer=candidate_layer,
            time=time,
            width=width,
            height=height,
            bbox_latlon=bbox_latlon,
            bbox_lonlat=bbox_lonlat,
        ))

    # Si el usuario eligió una fecha que Graniot todavía no tiene para ese lote,
    # primero intentamos la fecha solicitada y luego agregamos variantes sin
    # `time` como respaldo. Así la vista nunca queda en blanco/estancada: si la
    # fecha exacta existe se usa, y si no existe se muestra la última imagen
    # disponible mientras el cache queda caliente.
    if time:
        for candidate_layer in layer_candidates:
            variants.extend(_build_wms_param_variants(
                template_params=template_params,
                access_key=access_key,
                layer=candidate_layer,
                time=None,
                width=width,
                height=height,
                bbox_latlon=bbox_latlon,
                bbox_lonlat=bbox_lonlat,
            ))

    variants = _dedupe_wms_variants(variants)[:32]

    if not variants:
        raise HTTPException(status_code=400, detail="No se pudo construir la solicitud WMS para Graniot")

    errors: List[Dict[str, Any]] = []

    # /api/wms/ is documented by Graniot as access_key-based and public
    # (`security: - {}`). Try without auth first to match the spec and to avoid
    # Graniot returning JSON/500/502 because of an unexpected API-key header.
    # Authenticated requests remain as fallback for private deployments.
    try_auth_fallback = bool(getattr(settings, "GRANIOT_WMS_TRY_AUTH_FALLBACK", False))
    auth_modes = (False, True) if try_auth_fallback else (False,)

    for variant_index, params in enumerate(variants, start=1):
        for use_auth in auth_modes:
            try:
                log_event({
                    "event": "dataris.graniot.wms_proxy.request",
                    "operation": "wms-proxy",
                    "local_parcel_id": parcel_id,
                    "layer": clean_layer,
                    "time": time,
                    "variant_index": variant_index,
                    "use_auth": use_auth,
                    "path": wms_path,
                    "has_template": bool(template),
                    "recovered_template": bool(recovered_wms_data),
                    "layer_candidates": layer_candidates,
                    "has_geometry": bool(params.get("Geometry") or params.get("geometry")),
                    "has_bbox": bool(params.get("BBOX") or params.get("bbox")),
                    "params": safe_payload(params),
                })

                _wms_cloud_log(
                    logging.WARNING,
                    "attempt",
                    parcel_id=parcel_id,
                    layer=clean_layer,
                    time=time,
                    variant_index=variant_index,
                    use_auth=use_auth,
                    path=wms_path,
                    has_template=bool(template),
                    recovered_template=bool(recovered_wms_data),
                    has_geometry=bool(params.get("Geometry") or params.get("geometry")),
                    has_bbox=bool(params.get("BBOX") or params.get("bbox")),
                    param_keys=sorted([str(k) for k in params.keys()]),
                )
                raw = await client.binary_get(
                    wms_path,
                    params=params,
                    use_auth=use_auth,
                    debug_context={
                        "operation": "wms-proxy",
                        "local_parcel_id": parcel_id,
                        "layer": clean_layer,
                        "time": time,
                        "variant_index": variant_index,
                        "use_auth": use_auth,
                        "has_template": bool(template),
                        "recovered_template": bool(recovered_wms_data),
                        "layer_candidates": layer_candidates,
                        "has_geometry": bool(params.get("Geometry") or params.get("geometry")),
                        "has_bbox": bool(params.get("BBOX") or params.get("bbox")),
                    },
                    include_client_id=False,
                )

                media_type = raw.headers.get("content-type") or "image/png"
                _wms_cloud_log(
                    logging.WARNING if _is_image_response(raw) else logging.ERROR,
                    "attempt_response",
                    parcel_id=parcel_id,
                    layer=clean_layer,
                    time=time,
                    variant_index=variant_index,
                    use_auth=use_auth,
                    status_code=getattr(raw, "status_code", None),
                    content_type=media_type,
                    content_length=len(getattr(raw, "content", b"") or b""),
                    is_image=_is_image_response(raw),
                )
                if not _is_image_response(raw):
                    preview = _response_preview_text(raw)
                    errors.append({
                        "variant_index": variant_index,
                        "use_auth": use_auth,
                        "status_code": raw.status_code,
                        "content_type": media_type,
                        "preview": preview,
                    })
                    continue

                # No descartamos imágenes válidas por color. En algunos lotes el
                # índice NDVI/GNDVI puede ser predominantemente rojo por datos reales
                # o por la paleta de Graniot. Rechazarlo aquí provocaba que el
                # navegador se quedara esperando y mostrara "Cargando imagen
                # satelital" sin terminar. La corrección visual se hace evitando
                # pintar overlays de map-layer transformados como fuente principal,
                # no bloqueando PNGs válidos de Graniot.

                request_bounds = _bounds_from_wms_params(params)
                clip_bounds = request_bounds or bbox_values or _clip_bounds_from_context(template, bbox_values, local)

                # La optimización debe cachear y precargar la misma imagen que
                # Graniot mostraba antes, no transformar el raster en una máscara
                # sólida del polígono. Por eso el recorte backend queda apagado
                # por defecto. Si en algún despliegue se necesita, puede activarse
                # explícitamente con GRANIOT_WMS_BACKEND_MASK_ENABLED=true.
                if GRANIOT_WMS_BACKEND_MASK_ENABLED:
                    clip_geometry, clip_geometry_source = _clip_geometry_from_payload(
                        local,
                        template_params,
                        recovered_wms_data,
                        access_key=access_key,
                        graniot_parcel_id=local_graniot_parcel_id,
                    )
                    content, final_media_type, backend_clip_applied, clip_info = _apply_backend_polygon_mask(
                        raw.content,
                        media_type=media_type,
                        bounds=clip_bounds,
                        geometry=clip_geometry,
                    )
                else:
                    clip_geometry_source = "disabled"
                    content = raw.content
                    final_media_type = media_type
                    backend_clip_applied = False
                    clip_info = {"reason": "backend_mask_disabled_preserve_original_graniot_raster"}

                log_event({
                    "event": "dataris.graniot.wms_proxy.backend_clip_applied" if backend_clip_applied else "dataris.graniot.wms_proxy.backend_clip_skipped",
                    "operation": "wms-proxy",
                    "local_parcel_id": parcel_id,
                    "layer": clean_layer,
                    "time": time,
                    "variant_index": variant_index,
                    "bounds": clip_bounds,
                    "request_bounds": request_bounds,
                    "geometry_source": clip_geometry_source,
                    "clip_info": safe_payload(clip_info),
                    "source_media_type": media_type,
                    "output_media_type": final_media_type,
                })

                # Escena nublada: si el llamador no fijó fecha y la última
                # escena salió plana, se cambia por la primera limpia del
                # catálogo. El detector solo actúa con certeza (ver
                # _scene_looks_flat); si no hay escena mejor, se sirve esta.
                if not caller_pinned_time and not time and _scene_looks_flat(content):
                    chosen_scene = await _choose_clear_scene_date(
                        client,
                        parcel_token=parcel_id,
                        access_key=params.get("access_key") or access_key,
                        layer=params.get("layers") or clean_layer,
                        graniot_parcel_id=local_graniot_parcel_id,
                        wms_path=wms_path,
                        latest_content=content,
                    )
                    if chosen_scene:
                        swap_params = _clean_wms_params({
                            "access_key": params.get("access_key") or access_key,
                            "layers": params.get("layers") or clean_layer,
                            "response_format": "image/png",
                            "time": chosen_scene,
                            "width": width,
                            "height": height,
                        })
                        try:
                            swapped = await client.binary_get(
                                wms_path,
                                params=swap_params,
                                use_auth=False,
                                include_client_id=False,
                                debug_context={
                                    "operation": "wms-proxy-scene-swap",
                                    "local_parcel_id": parcel_id,
                                    "layer": clean_layer,
                                    "time": chosen_scene,
                                },
                            )
                            if _is_image_response(swapped):
                                content = swapped.content
                                final_media_type = swapped.headers.get("content-type") or final_media_type
                                _wms_cloud_log(
                                    logging.WARNING,
                                    "scene_swapped_for_clouds",
                                    parcel_id=parcel_id,
                                    layer=clean_layer,
                                    time=chosen_scene,
                                    content_length=len(content or b""),
                                )
                        except Exception as exc:
                            _wms_cloud_exception(
                                "scene_swap_failed",
                                exc,
                                parcel_id=parcel_id,
                                layer=clean_layer,
                                time=chosen_scene,
                            )

                _write_wms_disk_cache(stable_wms_cache_key, content, final_media_type)
                return _response_with_cache_headers(Response(
                    content=content,
                    media_type=final_media_type,
                ), "MISS")
            except GraniotAPIError as exc:
                error_item = {
                    "variant_index": variant_index,
                    "use_auth": use_auth,
                    "status_code": exc.status_code,
                    "message": str(exc),
                    "payload": safe_payload(exc.payload),
                }
                errors.append(error_item)
                _wms_cloud_log(
                    logging.ERROR,
                    "attempt_graniot_api_error",
                    parcel_id=parcel_id,
                    layer=clean_layer,
                    time=time,
                    **error_item,
                )
                continue
            except Exception as exc:
                error_item = {
                    "variant_index": variant_index,
                    "use_auth": use_auth,
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                }
                errors.append(error_item)
                _wms_cloud_exception(
                    "attempt_exception",
                    exc,
                    parcel_id=parcel_id,
                    layer=clean_layer,
                    time=time,
                    variant_index=variant_index,
                    use_auth=use_auth,
                )
                continue

    # Último recurso ante "Invalid access key": la clave firmada de Graniot
    # caduca, y la que guardamos durante el sync puede tener semanas. Repetir
    # las 32 variantes con la misma clave muerta no cambia nada, así que se pide
    # una recién firmada y se prueba UNA vez con la forma mínima, que es la
    # única que Graniot necesita (el polígono viaja dentro de la access_key).
    status_errors = [item for item in errors if item.get("status_code")]
    all_invalid_key = bool(status_errors) and all(
        "invalid access key" in str(item.get("message") or "").lower()
        for item in status_errors
    )
    if all_invalid_key:
        refreshed = await _recover_wms_data_shared(
            client,
            access_key=access_key,
            graniot_parcel_id=local_graniot_parcel_id,
            force=True,
        )
        fresh_key = ""
        if refreshed:
            fresh_key = str(
                _signed_wms_access_key(refreshed.get("graniot_wms_url"))
                or _signed_access_key_value(refreshed.get("graniot_wms_access_key"))
                or _signed_access_key_value(refreshed.get("graniot_access_key"))
                or ""
            ).strip()
            _store_recovered_wms_data_in_background(local, refreshed)

        if fresh_key and _normalized_token(fresh_key) != _normalized_token(access_key):
            retry_params = _clean_wms_params({
                "access_key": fresh_key,
                "layers": layer_candidates[0] if layer_candidates else clean_layer,
                "response_format": "image/png",
                "time": time,
            })
            _wms_cloud_log(
                logging.WARNING,
                "retry_with_fresh_key",
                parcel_id=parcel_id,
                layer=clean_layer,
                time=time,
                expired_access_key=_wms_token_info(access_key),
                fresh_access_key=_wms_token_info(fresh_key),
            )
            try:
                raw = await client.binary_get(
                    wms_path,
                    params=retry_params,
                    use_auth=False,
                    include_client_id=False,
                    debug_context={
                        "operation": "wms-proxy-retry-fresh-key",
                        "local_parcel_id": parcel_id,
                        "layer": clean_layer,
                        "time": time,
                    },
                )
                if _is_image_response(raw):
                    media_type = raw.headers.get("content-type") or "image/png"
                    _write_wms_disk_cache(stable_wms_cache_key, raw.content, media_type)
                    _wms_cloud_log(
                        logging.WARNING,
                        "retry_with_fresh_key_success",
                        parcel_id=parcel_id,
                        layer=clean_layer,
                        time=time,
                        content_length=len(raw.content or b""),
                    )
                    return _response_with_cache_headers(Response(
                        content=raw.content,
                        media_type=media_type,
                    ), "MISS")
                errors.append({
                    "variant_index": "retry_fresh_key",
                    "status_code": getattr(raw, "status_code", None),
                    "message": _response_preview_text(raw),
                })
            except Exception as exc:
                errors.append({
                    "variant_index": "retry_fresh_key",
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                })
                _wms_cloud_exception(
                    "retry_with_fresh_key_failed",
                    exc,
                    parcel_id=parcel_id,
                    layer=clean_layer,
                    time=time,
                )
        else:
            # Graniot no devuelve una clave nueva para esta parcela: el problema
            # ya no es la caducidad, es que el lote perdió su parcela allí. Un
            # 502 haría pensar en una avería; esto es un lote por resincronizar.
            log_event({
                "event": "dataris.graniot.wms_proxy.parcel_gone",
                "operation": "wms-proxy",
                "local_parcel_id": parcel_id,
                "graniot_parcel_id": local_graniot_parcel_id,
                "layer": clean_layer,
                "time": time,
                "found_local_row": bool(local),
                "recovered": bool(refreshed),
            })
            raise HTTPException(status_code=409, detail={
                "message": (
                    "Este lote ya no tiene una parcela válida en Graniot: la clave de acceso "
                    "caducó y Graniot no devuelve una nueva. Vuelve a sincronizar el lote "
                    "desde el panel de lotes."
                ),
                "requires_resync": True,
                "local_parcel_id": parcel_id,
                "graniot_parcel_id": local_graniot_parcel_id,
            })

    log_event({
        "event": "dataris.graniot.wms_proxy.failed",
        "operation": "wms-proxy",
        "local_parcel_id": parcel_id,
        "layer": clean_layer,
        "time": time,
        "has_template": bool(template),
        "recovered_template": bool(recovered_wms_data),
        "layer_candidates": layer_candidates,
        "errors": safe_payload(errors[-8:]),
    })

    hint = (
        "Graniot no devolvió una imagen para esta capa. "
        "Revisa en la respuesta JSON de esta solicitud los attempts; ahora el proxy intenta primero la forma oficial /api/wms/?access_key=...&layers=..."
    )
    _wms_cloud_log(
        logging.ERROR,
        "all_attempts_failed",
        parcel_id=parcel_id,
        layer=clean_layer,
        time=time,
        has_template=bool(template),
        recovered_template=bool(recovered_wms_data),
        layer_candidates=layer_candidates,
        attempts=errors[-8:],
    )
    raise HTTPException(status_code=502, detail={"message": hint, "attempts": errors[-8:]})
