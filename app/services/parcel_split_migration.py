"""Divide lotes legacy multi-parcela en filas individuales de `parcels`.

Los uploads anteriores al split por parcela (PR #89) guardaron el lote completo
como UNA fila cuya geometría es un FeatureCollection con un feature por
parcela. Toda la vista satelital (selección, raster, estadísticas y
comparación) opera por fila de `parcels`, así que el clic en cualquier parcela
seleccionaba y comparaba el lote entero. Aquí se aplica a las filas existentes
el mismo modelo que ya usa la ingesta: una fila por parcela detectada.

Idempotente: tras dividir no quedan filas multi-feature y la pasada siguiente
es un no-op. Los ids nuevos son deterministas (uuid5 del id padre + índice)
para que varias instancias concurrentes converjan al mismo resultado.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from app.utils.geojson_normalizer import normalize_record_geometries

_SPLIT_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "dataris:parcel-split")

# Rasters/jobs generados para el lote completo dejan de tener sentido una vez
# dividido; se limpian para que el historial satelital no apunte a ids muertos.
_STALE_PARCEL_CACHE_TABLES = ("satellite_images", "satellite_jobs")


def _normalized_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _lot_key(row: Dict[str, Any]) -> str:
    # Réplica de parcel_lot_key de compat.py (no se importa para evitar ciclo).
    for field in ("lote", "codigo", "name"):
        key = _normalized_key(row.get(field))
        if key:
            return key
    return ""


def _polygonal_features(row: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    geometry = row.get("geometry_geojson") or row.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "FeatureCollection":
        return None
    features = geometry.get("features")
    if not isinstance(features, list) or len(features) <= 1:
        return None
    polygonal = [
        feature
        for feature in features
        if isinstance(feature, dict)
        and isinstance(feature.get("geometry"), dict)
        and feature["geometry"].get("type") in {"Polygon", "MultiPolygon"}
    ]
    return polygonal if len(polygonal) > 1 else None


def _feature_finca(properties: Dict[str, Any]) -> str:
    for key, value in (properties or {}).items():
        if _normalized_key(key) == "finca":
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _unique_label(label: str, finca: str, used_keys: Set[str]) -> str:
    # dedupe_user_parcels colapsa filas con el mismo lote/codigo/name por
    # usuario, así que un nombre repetido haría desaparecer parcelas del API.
    candidate = label
    if _normalized_key(candidate) in used_keys and finca:
        candidate = f"{label} ({finca})"
    base = candidate
    suffix = 2
    while _normalized_key(candidate) in used_keys:
        candidate = f"{base} {suffix}"
        suffix += 1
    used_keys.add(_normalized_key(candidate))
    return candidate


def _split_parcel_row(
    row: Dict[str, Any],
    features: List[Dict[str, Any]],
    used_keys: Set[str],
    timestamp: str,
) -> List[Dict[str, Any]]:
    from app.services.telemetry.parcel_upload import _feature_label

    parent_id = str(row.get("id") or "")
    parent_name = str(row.get("name") or "").strip() or "Parcela"
    children: List[Dict[str, Any]] = []
    for index, feature in enumerate(features):
        properties = feature.get("properties") or {}
        finca = _feature_finca(properties)
        label = _unique_label(_feature_label(properties, index, parent_name), finca, used_keys)
        feature_collection = {"type": "FeatureCollection", "features": [feature]}
        children.append(normalize_record_geometries("parcels", {
            "id": str(uuid.uuid5(_SPLIT_ID_NAMESPACE, f"{parent_id}:{index}")),
            "user_id": row.get("user_id"),
            "name": label,
            "geometry": feature_collection,
            "geometry_geojson": feature_collection,
            "geometry_source_crs": row.get("geometry_source_crs") or "EPSG:4326",
            "finca": finca or parent_name,
            "file_url": row.get("file_url"),
            "created_at": row.get("created_at") or timestamp,
            "updated_at": timestamp,
        }))
    return children


def split_multi_feature_parcels(db: Dict[str, Any], *, timestamp: str) -> bool:
    """Divide en sitio las filas multi-feature. Devuelve True si cambió algo."""
    tables = db.get("tables") or {}
    parcels = tables.get("parcels")
    if not isinstance(parcels, list) or not parcels:
        return False

    to_split: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for row in parcels:
        features = _polygonal_features(row)
        if features:
            to_split.append((row, features))
    if not to_split:
        return False

    split_ids = {str(row.get("id") or "") for row, _ in to_split}
    remaining = [row for row in parcels if str(row.get("id") or "") not in split_ids]

    used_keys_by_user: Dict[str, Set[str]] = {}
    for row in remaining:
        used_keys_by_user.setdefault(str(row.get("user_id") or ""), set()).add(_lot_key(row))

    new_rows: List[Dict[str, Any]] = []
    for row, features in to_split:
        used_keys = used_keys_by_user.setdefault(str(row.get("user_id") or ""), set())
        new_rows.extend(_split_parcel_row(row, features, used_keys, timestamp))

    tables["parcels"] = remaining + new_rows

    for table_name in _STALE_PARCEL_CACHE_TABLES:
        rows = tables.get(table_name)
        if isinstance(rows, list):
            kept = [r for r in rows if str(r.get("parcel_id") or "") not in split_ids]
            if len(kept) != len(rows):
                tables[table_name] = kept
    return True
