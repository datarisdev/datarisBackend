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
    nested_data = statistics.get("data")
    if isinstance(nested_data, dict):
        return _mean_value(nested_data)
    return None


def _record_date(row: dict[str, Any]) -> datetime:
    for key in ("compared_at", "updated_at", "created_at", "image_date", "date"):
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


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_overlay(row: dict[str, Any]) -> dict[str, Any]:
    overlays = row.get("overlays")
    if isinstance(overlays, list):
        for overlay in overlays:
            if isinstance(overlay, dict):
                return overlay
    overlay = row.get("overlay")
    if isinstance(overlay, dict):
        return overlay
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


def _overlay_from_side(side: dict[str, Any], suffix: str = "") -> dict[str, Any]:
    overlay = _first_overlay(side)
    image_url = side.get("image_url") or overlay.get("image_url") or side.get("url") or side.get("public_url")
    bounds = side.get("bounds") or overlay.get("bounds")
    return {
        "id": overlay.get("id") or f"satellite-comparison-{suffix or _stable_hash(side)[:18]}",
        "image_url": image_url,
        "bounds": bounds,
        "date": overlay.get("date") or side.get("image_date") or side.get("date"),
        "layer": overlay.get("layer") or side.get("index_type") or side.get("index"),
        "source": overlay.get("source") or side.get("source") or "sentinel2-comparison",
        "coverage_percent": overlay.get("coverage_percent") if overlay.get("coverage_percent") is not None else side.get("coverage_percent"),
        "unavailable_percent": overlay.get("unavailable_percent") if overlay.get("unavailable_percent") is not None else side.get("unavailable_percent"),
        "is_partial": bool(overlay.get("is_partial", side.get("is_partial", False))),
        "quality_placeholder_applied": bool(overlay.get("quality_placeholder_applied", side.get("quality_placeholder_applied", False))),
        "quality_mask_applied": bool(overlay.get("quality_mask_applied", side.get("quality_mask_applied", False))),
    }


def _public_layer_record(row: dict[str, Any]) -> dict[str, Any]:
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
        "unavailable_percent": overlay.get("unavailable_percent") if overlay.get("unavailable_percent") is not None else record.get("unavailable_percent"),
        "is_partial": bool(overlay.get("is_partial", record.get("is_partial", False))),
        "quality_placeholder_applied": bool(overlay.get("quality_placeholder_applied", record.get("quality_placeholder_applied", False))),
        "quality_mask_applied": bool(overlay.get("quality_mask_applied", record.get("quality_mask_applied", False))),
    }
    return _json_safe(record)


def _normalize_comparison_side(raw: Any, *, label: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    map_layer = raw.get("map_layer") if isinstance(raw.get("map_layer"), dict) else {}
    overlay = _first_overlay(raw) or _first_overlay(map_layer)
    layer_meta = map_layer.get("layer") if isinstance(map_layer.get("layer"), dict) else {}
    statistics = _first_dict(raw.get("statistics"), map_layer.get("statistics"))
    index_type = str(
        raw.get("index_type")
        or raw.get("index")
        or raw.get("layer_key")
        or layer_meta.get("key")
        or overlay.get("layer")
        or "NDVI"
    ).upper().strip()
    image_date = str(raw.get("image_date") or raw.get("date") or map_layer.get("date") or overlay.get("date") or "")[:10]
    image_url = str(raw.get("image_url") or overlay.get("image_url") or "")
    bounds = raw.get("bounds") if isinstance(raw.get("bounds"), dict) else overlay.get("bounds")
    if not isinstance(bounds, dict):
        bounds = None
    coverage_percent = raw.get("coverage_percent")
    if coverage_percent is None:
        coverage_percent = map_layer.get("coverage_percent")
    if coverage_percent is None:
        coverage_percent = overlay.get("coverage_percent")
    unavailable_percent = raw.get("unavailable_percent")
    if unavailable_percent is None:
        unavailable_percent = map_layer.get("unavailable_percent")
    if unavailable_percent is None:
        unavailable_percent = overlay.get("unavailable_percent")
    is_partial = bool(raw.get("is_partial", map_layer.get("is_partial", overlay.get("is_partial", False))))
    quality_placeholder_applied = bool(raw.get("quality_placeholder_applied", map_layer.get("quality_placeholder_applied", overlay.get("quality_placeholder_applied", False))))
    quality_mask_applied = bool(raw.get("quality_mask_applied", map_layer.get("quality_mask_applied", overlay.get("quality_mask_applied", False))))
    mean_value = _mean_value(statistics)
    ndvi_mean = mean_value if index_type == "NDVI" else None
    source_count = raw.get("source_count") if raw.get("source_count") is not None else map_layer.get("source_count")
    scene_ids = raw.get("scene_ids") or map_layer.get("scene_ids") or []
    processing_version = raw.get("processing_version") or map_layer.get("processing_version")
    clean_overlay = {
        "id": overlay.get("id") or f"comparison-{label.lower()}-{_stable_hash({'image_url': image_url, 'date': image_date, 'index': index_type})[:18]}",
        "image_url": image_url,
        "bounds": bounds,
        "date": image_date,
        "layer": index_type,
        "source": overlay.get("source") or raw.get("source") or "sentinel2-comparison",
        "coverage_percent": coverage_percent,
        "unavailable_percent": unavailable_percent,
        "is_partial": is_partial,
        "quality_placeholder_applied": quality_placeholder_applied,
        "quality_mask_applied": quality_mask_applied,
    }
    return _json_safe(
        {
            "label": label,
            "image_date": image_date,
            "date": image_date,
            "index_type": index_type,
            "index": index_type,
            "image_url": image_url,
            "bounds": bounds,
            "statistics": statistics,
            "ndvi_mean": ndvi_mean,
            "average_ndvi": ndvi_mean,
            "cloud_coverage": raw.get("cloud_coverage") if raw.get("cloud_coverage") is not None else map_layer.get("cloud_coverage"),
            "coverage_percent": coverage_percent,
            "unavailable_percent": unavailable_percent,
            "is_partial": is_partial,
            "quality_placeholder_applied": quality_placeholder_applied,
            "quality_mask_applied": quality_mask_applied,
            "source_count": source_count or 1,
            "scene_ids": scene_ids,
            "processing_version": processing_version,
            "overlay": clean_overlay,
            "overlays": [clean_overlay],
        }
    )


def satellite_comparison_sides(row: dict[str, Any]) -> list[dict[str, Any]]:
    sides: list[dict[str, Any]] = []
    for label, key in (("A", "left"), ("B", "right")):
        side = row.get(key)
        if isinstance(side, dict):
            normalized = _normalize_comparison_side(side, label=label)
            if normalized:
                sides.append(normalized)
    if sides:
        return sides
    payload = row.get("analysis_payload")
    if isinstance(payload, dict):
        for label, key in (("A", "left"), ("B", "right")):
            side = payload.get(key)
            if isinstance(side, dict):
                normalized = _normalize_comparison_side(side, label=label)
                if normalized:
                    sides.append(normalized)
    return sides


def representative_satellite_comparison_side(row: dict[str, Any]) -> dict[str, Any]:
    sides = satellite_comparison_sides(row)
    if not sides:
        return {}
    # Prefer the most recent image. If both dates are equal, keep the right side
    # because it represents the user's final selection in the comparison view.
    return max(enumerate(sides), key=lambda item: (_record_date(item[1]), item[0]))[1]


def _public_comparison_record(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    sides = satellite_comparison_sides(record)
    left = sides[0] if sides else {}
    right = sides[1] if len(sides) > 1 else {}
    representative = representative_satellite_comparison_side(record)
    overlay = _overlay_from_side(representative, str(record.get("id") or "latest")) if representative else {}
    overlays = [_overlay_from_side(side, f"{record.get('id')}-{index}") for index, side in enumerate(sides)]
    if left:
        record["left"] = left
    if right:
        record["right"] = right
    record.update(
        {
            "analysis_type": "satellite_comparison",
            "comparison_type": "satellite_comparison",
            "image_date": representative.get("image_date") or record.get("image_date"),
            "index_type": representative.get("index_type") or record.get("index_type"),
            "index": representative.get("index_type") or record.get("index"),
            "image_url": representative.get("image_url") or record.get("image_url"),
            "bounds": representative.get("bounds") or record.get("bounds"),
            "statistics": representative.get("statistics") or record.get("statistics") or {},
            "ndvi_mean": representative.get("ndvi_mean") if representative.get("ndvi_mean") is not None else record.get("ndvi_mean"),
            "average_ndvi": representative.get("ndvi_mean") if representative.get("ndvi_mean") is not None else record.get("average_ndvi"),
            "cloud_coverage": representative.get("cloud_coverage") if representative.get("cloud_coverage") is not None else record.get("cloud_coverage"),
            "coverage_percent": representative.get("coverage_percent") if representative.get("coverage_percent") is not None else record.get("coverage_percent"),
            "unavailable_percent": representative.get("unavailable_percent") if representative.get("unavailable_percent") is not None else record.get("unavailable_percent"),
            "is_partial": bool(representative.get("is_partial", record.get("is_partial", False))),
            "quality_placeholder_applied": bool(representative.get("quality_placeholder_applied", record.get("quality_placeholder_applied", False))),
            "quality_mask_applied": bool(representative.get("quality_mask_applied", record.get("quality_mask_applied", False))),
            "overlay": overlay,
            "overlays": overlays,
            "comparison": {
                "left_date": left.get("image_date"),
                "right_date": right.get("image_date"),
                "left_index": left.get("index_type"),
                "right_index": right.get("index_type"),
            },
        }
    )
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
    """Persist one generated layer as an internal reusable cache record.

    These rows intentionally stay separate from user-facing history. Opening a
    layer, requesting statistics or preloading available dates should not be
    presented to the client as a completed comparison.
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
        "id": f"sentinel2-cache-{analysis_key[:20]}",
        "image_url": image_url,
        "bounds": bounds,
        "date": image_date,
        "layer": normalized_index,
        "source": result.get("source") or source,
        "coverage_percent": result.get("coverage_percent"),
        "unavailable_percent": result.get("unavailable_percent"),
        "is_partial": bool(result.get("is_partial", False)),
        "quality_placeholder_applied": bool(result.get("quality_placeholder_applied", False)),
        "quality_mask_applied": bool(result.get("quality_mask_applied", False)),
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
        "unavailable_percent": result.get("unavailable_percent"),
        "is_partial": bool(result.get("is_partial", False)),
        "quality_placeholder_applied": bool(result.get("quality_placeholder_applied", False)),
        "quality_mask_applied": bool(result.get("quality_mask_applied", False)),
        "source_count": result.get("source_count", 1),
        "scene_ids": result.get("scene_ids") or [],
        "processing_version": processing_version,
        "source": source,
        "analysis_key": analysis_key,
        "analysis_type": "satellite_cache_layer",
        "analysis_payload": {
            "requested_date": requested_date,
            "resolved_date": image_date,
            "layer": normalized_index,
            "overlays": [overlay],
            "warnings": list(warnings or result.get("warnings") or []),
            "source_count": result.get("source_count", 1),
            "scene_ids": result.get("scene_ids") or [],
            "coverage_percent": result.get("coverage_percent"),
            "unavailable_percent": result.get("unavailable_percent"),
            "is_partial": bool(result.get("is_partial", False)),
            "quality_placeholder_applied": bool(result.get("quality_placeholder_applied", False)),
            "quality_mask_applied": bool(result.get("quality_mask_applied", False)),
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

    return _public_layer_record(stored)


def list_satellite_analysis_history(
    *,
    user_id: str,
    parcel_id: Optional[str] = None,
    index_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return generated cache layers for internal date catalogs and reuse."""
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
    return [_public_layer_record(row) for row in filtered[:safe_limit]]


def persist_satellite_comparison_record(
    *,
    user_id: str,
    parcel: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    max_cloud_coverage: Optional[float] = None,
    source: str = "satellite-comparison",
) -> Optional[dict[str, Any]]:
    """Persist one user-visible comparison event with both reviewed images."""
    parcel_id = str(parcel.get("id") or "")
    if not user_id or not parcel_id:
        return None
    normalized_left = _normalize_comparison_side(left, label="A")
    normalized_right = _normalize_comparison_side(right, label="B")
    if not normalized_left or not normalized_right:
        return None
    if not normalized_left.get("image_url") or not normalized_right.get("image_url"):
        return None

    comparison_key = _stable_hash(
        {
            "user_id": str(user_id),
            "parcel_id": parcel_id,
            "left": {
                "date": normalized_left.get("image_date"),
                "index": normalized_left.get("index_type"),
                "image_url": normalized_left.get("image_url"),
            },
            "right": {
                "date": normalized_right.get("image_date"),
                "index": normalized_right.get("index_type"),
                "image_url": normalized_right.get("image_url"),
            },
        }
    )
    timestamp = _now()
    representative = max(enumerate([normalized_left, normalized_right]), key=lambda item: (_record_date(item[1]), item[0]))[1]
    record = {
        "user_id": str(user_id),
        "parcel_id": parcel_id,
        "analysis_type": "satellite_comparison",
        "comparison_type": "satellite_comparison",
        "processing_status": "completed",
        "status": "completed",
        "comparison_key": comparison_key,
        "analysis_key": comparison_key,
        "compared_at": timestamp,
        "updated_at": timestamp,
        "source": source,
        "title": f"Comparación satelital {normalized_left.get('index_type')} {normalized_left.get('image_date')} vs {normalized_right.get('index_type')} {normalized_right.get('image_date')}",
        "left": normalized_left,
        "right": normalized_right,
        "date_from": normalized_left.get("image_date"),
        "date_to": normalized_right.get("image_date"),
        "index_types": list(dict.fromkeys([normalized_left.get("index_type"), normalized_right.get("index_type")])),
        "image_date": representative.get("image_date"),
        "index_type": representative.get("index_type"),
        "index": representative.get("index_type"),
        "image_url": representative.get("image_url"),
        "bounds": representative.get("bounds"),
        "statistics": representative.get("statistics") or {},
        "ndvi_mean": representative.get("ndvi_mean"),
        "average_ndvi": representative.get("ndvi_mean"),
        "cloud_coverage": representative.get("cloud_coverage"),
        "coverage_percent": representative.get("coverage_percent"),
        "unavailable_percent": representative.get("unavailable_percent"),
        "is_partial": bool(representative.get("is_partial", False)),
        "quality_placeholder_applied": bool(representative.get("quality_placeholder_applied", False)),
        "quality_mask_applied": bool(representative.get("quality_mask_applied", False)),
        "max_cloud_coverage": max_cloud_coverage,
        "analysis_payload": {
            "left": normalized_left,
            "right": normalized_right,
            "parcel": {
                "id": parcel_id,
                "name": parcel.get("name") or parcel.get("lote") or parcel.get("finca"),
                "finca": parcel.get("finca"),
                "lote": parcel.get("lote"),
                "area": parcel.get("area") or parcel.get("area_ha") or parcel.get("hectareas"),
            },
        },
    }

    with compat_store.LOCK:
        db = compat_store.read_db()
        rows = compat_store.table(db, "satellite_comparisons")
        existing = next(
            (
                row
                for row in rows
                if str(row.get("user_id") or "") == str(user_id)
                and str(row.get("parcel_id") or "") == parcel_id
                and str(row.get("comparison_key") or "") == comparison_key
            ),
            None,
        )
        if existing:
            existing.update(_json_safe(record))
            existing["open_count"] = int(existing.get("open_count") or 1) + 1
            stored = existing
        else:
            stored = {"id": str(uuid.uuid4()), "created_at": timestamp, "open_count": 1, **_json_safe(record)}
            rows.append(stored)
        compat_store.write_db(db)

    return _public_comparison_record(stored)


def list_satellite_comparison_history(
    *,
    user_id: str,
    parcel_id: Optional[str] = None,
    index_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    normalized_index = str(index_type or "").upper().strip()
    with compat_store.LOCK:
        db = compat_store.read_db()
        rows = [dict(row) for row in compat_store.table(db, "satellite_comparisons")]

    filtered: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("user_id") or "") != str(user_id):
            continue
        if parcel_id and str(row.get("parcel_id") or "") != str(parcel_id):
            continue
        indices = {str(side.get("index_type") or "").upper().strip() for side in satellite_comparison_sides(row)}
        if normalized_index and normalized_index not in indices:
            continue
        filtered.append(row)
    filtered.sort(key=_record_date, reverse=True)
    safe_limit = max(1, min(int(limit or 100), 500))
    return [_public_comparison_record(row) for row in filtered[:safe_limit]]


def summarize_satellite_comparison_history(*, user_id: str, parcel_id: Optional[str] = None) -> dict[str, Any]:
    rows = list_satellite_comparison_history(user_id=user_id, parcel_id=parcel_id, limit=500)
    latest = rows[0] if rows else None
    ndvi_values: list[float] = []
    coverage_values: list[float] = []
    partial = 0
    for row in rows:
        if row.get("is_partial"):
            partial += 1
        for side in satellite_comparison_sides(row):
            if str(side.get("index_type") or "").upper() == "NDVI":
                value = _as_float(side.get("ndvi_mean"))
                if value is not None:
                    ndvi_values.append(value)
            coverage = _as_float(side.get("coverage_percent"))
            if coverage is not None:
                coverage_values.append(coverage)
    return _json_safe(
        {
            "total_analyses": len(rows),
            "ndvi_analyses": len(ndvi_values),
            "average_ndvi": round(sum(ndvi_values) / len(ndvi_values), 4) if ndvi_values else None,
            "average_coverage_percent": round(sum(coverage_values) / len(coverage_values), 2) if coverage_values else None,
            "partial_analyses": partial,
            "last_analysis_at": latest.get("compared_at") if latest else None,
            "last_index_type": latest.get("index_type") if latest else None,
        }
    )


# Backwards-compatible name retained for existing imports. The public history
# endpoint now uses comparison history; generated layers remain internal.
def summarize_satellite_analysis_history(*, user_id: str, parcel_id: Optional[str] = None) -> dict[str, Any]:
    return summarize_satellite_comparison_history(user_id=user_id, parcel_id=parcel_id)
