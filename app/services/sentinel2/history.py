from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.api.routers import compat as compat_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "."))
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _mean_value(statistics: Any) -> Optional[float]:
    if not isinstance(statistics, dict):
        return None
    for key in ("ndvi_mean", "mean_ndvi", "average_ndvi", "avg_ndvi", "mean", "average"):
        value = _as_float(statistics.get(key))
        if value is not None:
            return value
    return None


def _record_date(row: dict[str, Any]) -> datetime:
    for key in ("updated_at", "created_at", "image_date", "date"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            text = str(raw)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            value = datetime.fromisoformat(text)
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


def _first_overlay(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("analysis_payload")
    if not isinstance(payload, dict):
        return {}
    overlays = payload.get("overlays")
    if isinstance(overlays, list):
        for overlay in overlays:
            if isinstance(overlay, dict):
                return overlay
    overlay = payload.get("overlay")
    return overlay if isinstance(overlay, dict) else {}


def _public_record(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    overlay = _first_overlay(record)
    image_url = record.get("image_url") or overlay.get("image_url") or record.get("url") or record.get("public_url")
    bounds = record.get("bounds") or overlay.get("bounds")
    if image_url:
        record["image_url"] = image_url
    if bounds:
        record["bounds"] = bounds
    record["overlay"] = {
        "id": overlay.get("id") or f"history-{record.get('id')}",
        "image_url": image_url,
        "bounds": bounds,
        "date": overlay.get("date") or record.get("image_date"),
        "layer": overlay.get("layer") or record.get("index_type") or record.get("index"),
        "source": overlay.get("source") or record.get("source"),
        "coverage_percent": overlay.get("coverage_percent") if overlay.get("coverage_percent") is not None else record.get("coverage_percent"),
        "is_partial": bool(overlay.get("is_partial", record.get("is_partial", False))),
    }
    return _json_safe(record)


def persist_satellite_analysis_record(
    *,
    user_id: str,
    parcel: dict[str, Any],
    index_key: str,
    result: dict[str, Any],
    requested_date: Optional[str] = None,
    warnings: Optional[Iterable[str]] = None,
    source: str = "sentinel2-map-layer",
) -> Optional[dict[str, Any]]:
    """Persist a completed Sentinel-2 render in compat storage.

    The production dashboard, work-area history and current frontend read the
    compat state table. Local/GCS cache generation alone is not enough: every
    usable layer must also be registered here. Records are upserted by a stable
    analysis key so prefetching and repeated map visits do not inflate storage.
    """
    if not result.get("available"):
        return None

    parcel_id = str(parcel.get("id") or "")
    if not parcel_id or not user_id:
        return None

    image_date = str(result.get("date") or requested_date or datetime.now(timezone.utc).date().isoformat())[:10]
    object_path = str(result.get("object_path") or "")
    image_url = str(result.get("image_url") or "")
    processing_version = str(result.get("processing_version") or "")
    bounds = result.get("bounds") if isinstance(result.get("bounds"), dict) else None
    statistics = result.get("statistics") if isinstance(result.get("statistics"), dict) else {}
    normalized_index = str(index_key or "NDVI").upper()
    analysis_key = _stable_hash(
        {
            "provider": "sentinel-2-l2a",
            "user_id": str(user_id),
            "parcel_id": parcel_id,
            "image_date": image_date,
            "index_type": normalized_index,
            "object_path": object_path or image_url,
            "processing_version": processing_version,
        }
    )
    timestamp = _now()
    mean_value = _mean_value(statistics)
    ndvi_mean = mean_value if normalized_index == "NDVI" else None
    overlay = {
        "id": f"sentinel2-history-{analysis_key[:20]}",
        "image_url": image_url,
        "bounds": bounds,
        "date": image_date,
        "layer": normalized_index,
        "source": result.get("source") or source,
        "coverage_percent": result.get("coverage_percent"),
        "is_partial": bool(result.get("is_partial", False)),
    }
    record = {
        "user_id": str(user_id),
        "parcel_id": parcel_id,
        "image_date": image_date,
        "index_type": normalized_index,
        "index": normalized_index,
        "image_url": image_url,
        "image_object_path": object_path or image_url,
        "processing_status": "completed",
        "status": "completed",
        "cloud_coverage": result.get("cloud_coverage"),
        "bounds": bounds,
        "statistics": statistics,
        "ndvi_mean": ndvi_mean,
        "average_ndvi": ndvi_mean,
        "coverage_percent": result.get("coverage_percent"),
        "is_partial": bool(result.get("is_partial", False)),
        "source_count": result.get("source_count", 1),
        "scene_ids": result.get("scene_ids") or [],
        "processing_version": processing_version,
        "source": source,
        "analysis_key": analysis_key,
        "analysis_type": "satellite_vegetation_health" if normalized_index == "NDVI" else "satellite_index",
        "analysis_payload": {
            "requested_date": requested_date,
            "resolved_date": image_date,
            "layer": normalized_index,
            "overlays": [overlay],
            "warnings": list(warnings or result.get("warnings") or []),
            "source_count": result.get("source_count", 1),
            "scene_ids": result.get("scene_ids") or [],
            "coverage_percent": result.get("coverage_percent"),
            "is_partial": bool(result.get("is_partial", False)),
            "processing_version": processing_version,
            "parcel": {
                "id": parcel_id,
                "name": parcel.get("name") or parcel.get("lote") or parcel.get("finca"),
                "finca": parcel.get("finca"),
                "lote": parcel.get("lote"),
                "area": parcel.get("area") or parcel.get("area_ha") or parcel.get("hectareas"),
            },
        },
        "updated_at": timestamp,
    }

    with compat_store.LOCK:
        db = compat_store.read_db()
        rows = compat_store.table(db, "satellite_images")
        existing = next(
            (
                row
                for row in rows
                if str(row.get("user_id") or "") == str(user_id)
                and str(row.get("parcel_id") or "") == parcel_id
                and str(row.get("analysis_key") or "") == analysis_key
            ),
            None,
        )
        if existing:
            existing.update(_json_safe(record))
            stored = existing
        else:
            stored = {"id": str(uuid.uuid4()), "created_at": timestamp, **_json_safe(record)}
            rows.append(stored)
        compat_store.write_db(db)

    return _public_record(stored)


def list_satellite_analysis_history(
    *,
    user_id: str,
    parcel_id: Optional[str] = None,
    index_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    normalized_index = str(index_type or "").upper().strip()
    with compat_store.LOCK:
        db = compat_store.read_db()
        rows = [dict(row) for row in compat_store.table(db, "satellite_images")]

    filtered = []
    for row in rows:
        if str(row.get("user_id") or "") != str(user_id):
            continue
        if parcel_id and str(row.get("parcel_id") or "") != str(parcel_id):
            continue
        row_index = str(row.get("index_type") or row.get("index") or "").upper().strip()
        if normalized_index and row_index != normalized_index:
            continue
        filtered.append(row)

    filtered.sort(key=_record_date, reverse=True)
    safe_limit = max(1, min(int(limit or 100), 500))
    return [_public_record(row) for row in filtered[:safe_limit]]


def summarize_satellite_analysis_history(*, user_id: str, parcel_id: Optional[str] = None) -> dict[str, Any]:
    rows = list_satellite_analysis_history(user_id=user_id, parcel_id=parcel_id, limit=500)
    latest = rows[0] if rows else None
    ndvi_rows = [row for row in rows if str(row.get("index_type") or "").upper() == "NDVI" and _as_float(row.get("ndvi_mean")) is not None]
    coverage = [_as_float(row.get("coverage_percent")) for row in rows]
    coverage = [value for value in coverage if value is not None]
    return _json_safe(
        {
            "total_analyses": len(rows),
            "ndvi_analyses": len(ndvi_rows),
            "average_ndvi": round(sum(float(row["ndvi_mean"]) for row in ndvi_rows) / len(ndvi_rows), 4) if ndvi_rows else None,
            "average_coverage_percent": round(sum(coverage) / len(coverage), 2) if coverage else None,
            "partial_analyses": len([row for row in rows if row.get("is_partial")]),
            "last_analysis_at": latest.get("image_date") if latest else None,
            "last_index_type": latest.get("index_type") if latest else None,
        }
    )
