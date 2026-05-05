from __future__ import annotations

import os
import json
import re
from io import BytesIO
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Body, Header, HTTPException, Query, Response
from shapely.geometry import box as shapely_box, mapping, shape as shapely_shape
from shapely.ops import transform, unary_union
from shapely import wkt as shapely_wkt
from PIL import Image, ImageChops, ImageDraw

from app.api.routers.compat import LOCK, bearer_user, now, read_db, table, write_db
from app.core.config import settings
from app.services.graniot_client import GraniotAPIError, GraniotClient, GraniotNotConfigured
from app.services.graniot_debug import clear_logs, get_log_file_path, log_event, read_logs, safe_payload

router = APIRouter(prefix="/graniot", tags=["Graniot"])


def _require_user(authorization: Optional[str]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


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
        for key in ("results", "data", "items", "layers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [payload]
    return []


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


def _normalize_layer(layer: Dict[str, Any]) -> Dict[str, Any]:
    displayed = layer.get("displayed_name") or layer.get("display_name") or layer.get("label") or layer.get("name") or "Capa"
    key = str(layer.get("key") or layer.get("layer_key") or layer.get("name") or layer.get("id") or "")
    name = layer.get("name") or displayed
    resolution = layer.get("layer_resolution") or layer.get("resolution_name") or layer.get("resolution")
    wms_layer = layer.get("wms_layer") or layer.get("wms_name") or layer.get("layer") or layer.get("layers") or key or name
    return {
        "id": layer.get("id"),
        "key": key,
        "name": name,
        "displayed_name": displayed,
        "label": displayed,
        "wms_layer": str(wms_layer) if wms_layer is not None else key,
        "color": layer.get("color"),
        "legend": layer.get("legend"),
        "config": layer.get("config"),
        "layer_stats": layer.get("layer_stats"),
        "layer_resolution": resolution,
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
    wanted_id = _normalized_token(graniot_parcel_id)
    keys = [
        data.get("graniot_access_key"),
        data.get("graniot_wms_access_key"),
        data.get("graniot_parcel_key"),
        _access_key_from_wms_template(data.get("graniot_wms_url")),
    ]
    ids = [data.get("graniot_parcel_id")]
    if wanted_key and any(_normalized_token(value) == wanted_key for value in keys):
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
        for item in subparcels:
            if not isinstance(item, dict):
                continue
            template = item.get("graniot_wms_url") or item.get("wms_url") or item.get("image_url")
            if isinstance(template, str) and template.strip() and not fallback_subparcel_template:
                fallback_subparcel_template = template.strip()
            data = {
                "graniot_access_key": item.get("graniot_access_key"),
                "graniot_wms_access_key": item.get("graniot_wms_access_key"),
                "graniot_parcel_key": item.get("graniot_parcel_key"),
                "graniot_parcel_id": item.get("graniot_parcel_id"),
                "graniot_wms_url": template,
            }
            if template and _wms_data_matches_requested(data, access_key=access_key, graniot_parcel_id=graniot_parcel_id):
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


def _is_uuid_like(value: Any) -> bool:
    clean = str(value or "").strip()
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", clean))


def _signed_wms_access_key(value: Any) -> Optional[str]:
    key = _access_key_from_wms_template(value)
    if key and not _is_uuid_like(key):
        return key
    return None

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
    wanted_id = _normalized_token(parcel_id)

    keys = [
        item.get("access_key"),
        item.get("key"),
        props.get("access_key"),
        props.get("key"),
    ]
    ids = [item.get("id"), props.get("id"), item.get("parcel_id"), props.get("parcel_id")]

    if wanted_key and any(_normalized_token(value) == wanted_key for value in keys):
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


def _public_graniot_subparcels(payload: Any) -> List[Dict[str, Any]]:
    public_items: List[Dict[str, Any]] = []
    for data in _all_wms_data_from_payload(payload):
        bounds = (
            _bbox_from_graniot_bbox(data.get("graniot_bbox"))
            or _bounds_from_wms_template(data.get("graniot_wms_url"))
            or _bounds_from_wms_template(data.get("graniot_image_url"))
        )
        public_items.append({
            "graniot_parcel_id": data.get("graniot_parcel_id"),
            "graniot_parcel_key": data.get("graniot_parcel_key"),
            "graniot_access_key": data.get("graniot_access_key"),
            "graniot_wms_access_key": data.get("graniot_wms_access_key"),
            "graniot_wms_url": data.get("graniot_wms_url"),
            "graniot_image_url": data.get("graniot_image_url"),
            "graniot_geometry": data.get("graniot_geometry"),
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


def _store_recovered_wms_data(local: Optional[Dict[str, Any]], data: Optional[Dict[str, Any]]) -> None:
    if not local or not data:
        return
    updates = {k: v for k, v in {
        "graniot_parcel_id": data.get("graniot_parcel_id"),
        "graniot_parcel_key": data.get("graniot_parcel_key"),
        "graniot_access_key": data.get("graniot_wms_access_key") or data.get("graniot_access_key"),
        "graniot_wms_access_key": data.get("graniot_wms_access_key"),
        "graniot_wms_url": data.get("graniot_wms_url"),
        "graniot_bbox": data.get("graniot_bbox"),
        "graniot_geometry": data.get("graniot_geometry"),
    }.items() if v not in (None, "")}
    if not updates:
        return
    try:
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == local.get("id")), None)
            if not row:
                return
            row.update(updates)
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


def _layer_identifier_candidates(layer: str) -> List[str]:
    """Try WMS layer aliases used by Graniot in different endpoints/UI labels."""
    raw = str(layer or "").strip()
    if not raw:
        return []

    compact = raw.upper().replace(" ", "_").replace("-", "_")
    aliases: Dict[str, List[str]] = {
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
    }

    candidates: List[str] = [raw]
    candidates.extend(aliases.get(compact, []))
    candidates.extend([
        raw.replace("_", "-"),
        raw.replace("-", "_"),
        raw.upper(),
    ])

    # Keep request volume reasonable. The WMS proxy will combine each layer with
    # several URL shapes, so only send a small set of meaningful layer names.
    deduped: List[str] = []
    seen = set()
    for item in candidates:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            deduped.append(value)
            seen.add(key)
        if len(deduped) >= 5:
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

    The OpenAPI spec supplied by Graniot documents `/api/wms/` as a custom
    endpoint, not a standard OGC GetMap endpoint. It requires `access_key` and
    `layers`, and optionally accepts `width`, `height`, `time`, `bands`,
    `content`, `evalscript_url` and `response_format`.

    The previous implementation tried WMS/OGC-style parameters first
    (`SERVICE`, `REQUEST`, `BBOX`, `LAYERS`, `FORMAT`, `MAXCC`, `WARNINGS`). In
    this environment Graniot returns 500/502 for those extra parameters, so the
    browser received JSON instead of a PNG. Keep the official minimal request as
    the first variants and use legacy/template variants only as fallbacks.
    """
    layer_value = str(layer or "").strip()
    template_access_key = template_params.get("access_key") or template_params.get("ACCESS_KEY")
    # Prefer the signed WMS token from Graniot's image_url. The UUID-like
    # parcel key stored in Dataris is valid for metadata matching, but /api/wms/
    # rejects it with "Invalid access key.".
    access_key_value = str(template_access_key or access_key or "").strip()

    variants: List[Dict[str, Any]] = []

    geometry = template_params.get("Geometry") or template_params.get("geometry")
    bbox_from_template = template_params.get("BBOX") or template_params.get("bbox")

    if template_params and (geometry or bbox_from_template):
        # 1) Prefer the exact Graniot image_url template because it carries the
        # same Geometry/BBOX used by Graniot's UI. This keeps the raster aligned
        # with the parcel and avoids requesting a broader image.
        exact_template = dict(template_params)
        exact_template["access_key"] = access_key_value
        exact_template["layers"] = layer_value
        exact_template.setdefault("response_format", "image/png")
        if time:
            exact_template["time"] = time
        variants.append(exact_template)

    # 2) Official Graniot WMS request, exactly as documented. This remains as a
    # fallback for deployments that reject legacy GetMap-style params.
    official_minimal = {
        "access_key": access_key_value,
        "layers": layer_value,
        "response_format": "image/png",
    }
    if time:
        official_minimal["time"] = time
    variants.append(official_minimal)

    # 3) Same documented request with explicit image size.
    official_sized = dict(official_minimal)
    official_sized.update({"width": width, "height": height})
    variants.append(official_sized)

    if template_params:
        # 4) Sanitized version of Graniot's image_url. Preserve only parameters
        # documented for /api/wms/ and drop unsupported OGC query string fields.
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
        })
        if time:
            sanitized_template["time"] = time
        variants.append(sanitized_template)

    # 5) Legacy Graniot-style fallback with Geometry/BBOX, used only if the
    # documented endpoint variants failed.
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
    if bbox_from_template:
        graniot_style["BBOX"] = bbox_from_template
    elif bbox_latlon:
        graniot_style["BBOX"] = bbox_latlon
    variants.append(graniot_style)

    # 6) Standard WMS fallbacks for any Graniot environment that still accepts
    # GetMap-style calls.
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


def _fix_polygonal_geometry(geom: Any) -> Optional[Any]:
    if not geom or getattr(geom, "is_empty", True):
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
    except Exception:
        pass
    parts = _iter_polygon_parts(geom)
    if not parts:
        return None
    try:
        return unary_union(parts)
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

    try:
        minx, miny, maxx, maxy = geom.bounds
        # Graniot's image_url currently serializes Geometry as LAT LON WKT, e.g.
        # POLYGON ((14.33 -91.41, ...)). Shapely reads the first coordinate as X,
        # so swap axes when X looks like latitude and Y looks like longitude.
        x_looks_like_lat = -90 <= minx <= 90 and -90 <= maxx <= 90
        y_looks_like_lon = abs(miny) > 90 or abs(maxy) > 90
        if x_looks_like_lat and y_looks_like_lon:
            geom = transform(lambda x, y, z=None: (y, x) if z is None else (y, x, z), geom)
    except Exception:
        pass

    return _fix_polygonal_geometry(geom)


def _clip_geometry_from_payload(local: Optional[Dict[str, Any]], template_params: Dict[str, Any], recovered_wms_data: Optional[Dict[str, Any]]) -> tuple[Optional[Any], str]:
    """Return the geometry used to clip the WMS image.

    The mask must follow the polygon that the user sees in Dataris. Earlier
    versions preferred Graniot's Geometry template; that can be a broader
    raster footprint and it made the layer bleed outside the local dashed
    polygon. Prefer the local GeoJSON and only use Graniot's WKT/geometry as a
    fallback when the local parcel has no valid geometry.
    """
    if local:
        for source_name, candidate in (
            ("local.geometry", local.get("geometry")),
            ("local.graniot_geometry", local.get("graniot_geometry")),
        ):
            geom = _geometry_from_geojson_value(candidate)
            if geom is not None:
                return geom, source_name

        raw_data = _wms_data_from_payload(
            local.get("graniot_raw"),
            access_key=local.get("graniot_parcel_key"),
            parcel_id=local.get("graniot_parcel_id"),
        )
        if raw_data:
            geom = _geometry_from_geojson_value(raw_data.get("graniot_geometry"))
            if geom is not None:
                return geom, "local.graniot_raw.geometry"

    for source_name, candidate in (
        ("recovered.graniot_geometry", recovered_wms_data.get("graniot_geometry") if recovered_wms_data else None),
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

    The mask must use the same coordinate extent as the returned image. Using
    the template BBOX while the request used another BBOX shifts the mask and
    causes apparent holes/bleeding.
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
        if version.startswith("1.3") and "4326" in crs:
            south, west, north, east = a, b, c, d
        else:
            west, south, east, north = a, b, c, d
        if south is None or west is None or north is None or east is None:
            return None
        if not (south < north and west < east):
            return None
        return {"south": float(south), "west": float(west), "north": float(north), "east": float(east)}
    except Exception:
        return None


def _pixel_points_from_polygon(poly: Any, bounds: Dict[str, float], width: int, height: int, scale: int = 1) -> List[tuple[int, int]]:
    south = float(bounds["south"])
    north = float(bounds["north"])
    west = float(bounds["west"])
    east = float(bounds["east"])
    lon_span = east - west
    lat_span = north - south
    if lon_span <= 0 or lat_span <= 0:
        return []
    points = []
    for lon, lat in list(poly.exterior.coords):
        x = ((float(lon) - west) / lon_span) * width * scale
        y = ((north - float(lat)) / lat_span) * height * scale
        # Keep a tiny padding around the image to avoid cutting a border pixel
        # from polygons that lie exactly on the WMS bbox.
        x = max(-2 * scale, min((width + 2) * scale, x))
        y = max(-2 * scale, min((height + 2) * scale, y))
        points.append((int(round(x)), int(round(y))))
    return points


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

    # Backend mask in the exact WMS response coordinate system. Use a higher
    # scale for stable edges, but DO NOT replace the original alpha. Graniot PNGs
    # may contain RGB values in fully transparent pixels; replacing alpha makes
    # those hidden pixels visible as solid purple/green blobs. Multiplying alpha
    # clips the raster without inventing pixels that Graniot did not render.
    scale = 4 if max(width, height) <= 2048 else 2
    mask = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(mask)
    drew_any = False
    part_count = 0

    for poly in parts:
        points = _pixel_points_from_polygon(poly, bounds, width, height, scale=scale)
        if len(points) >= 3:
            # Draw only exterior rings. Interior rings from local parcel uploads
            # are frequently subdivision artifacts, not real holes in Graniot.
            draw.polygon(points, fill=255)
            drew_any = True
            part_count += 1

    if not drew_any:
        return content, media_type, False, {"reason": "mask_draw_empty", "bounds": bounds}

    if scale != 1:
        try:
            resampling = Image.Resampling.LANCZOS
        except AttributeError:
            resampling = Image.LANCZOS
        mask = mask.resize((width, height), resampling)
        # Hard threshold: no leaks outside the local polygon. A low threshold
        # keeps border pixels filled without exposing distant transparent RGB.
        mask = mask.point(lambda value: 255 if value >= 24 else 0)

    original_alpha = image.getchannel("A")
    final_alpha = ImageChops.multiply(original_alpha, mask)
    image.putalpha(final_alpha)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), "image/png", True, {
        "width": width,
        "height": height,
        "parts": part_count,
        "bounds": bounds,
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

async def _create_default_farm(client: GraniotClient) -> str:
    name = settings.GRANIOT_DEFAULT_FARM_NAME or "Dataris"
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
    if geom_type == "Polygon" and isinstance(coordinates, list):
        return [{"type": "Polygon", "coordinates": coordinates}]

    if geom_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons: List[Dict[str, Any]] = []
        for polygon_coordinates in coordinates:
            if isinstance(polygon_coordinates, list) and polygon_coordinates:
                polygons.append({"type": "Polygon", "coordinates": polygon_coordinates})
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


def _build_graniot_single_parcel_payload(parcels_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a single-parcel PATCH payload for already-synced local parcels."""
    parcels = parcels_payload.get("parcels") if isinstance(parcels_payload, dict) else []
    if not isinstance(parcels, list) or not parcels:
        raise HTTPException(status_code=400, detail="No hay lote para actualizar en Graniot")
    first = parcels[0]
    return {
        "farm": parcels_payload.get("farm"),
        "name": first.get("name"),
        "metadata": first.get("metadata") or {},
        "geom": first.get("geom"),
    }


async def _resolve_farm_id(client: GraniotClient, local: Dict[str, Any], payload: Dict[str, Any]) -> str:
    # Acepta camelCase y snake_case porque el frontend puede enviar farmId,
    # mientras que Graniot y el backend usan farm_id/graniot_farm_id.
    candidates = [
        payload.get("graniot_farm_id"),
        payload.get("farm_id"),
        payload.get("farmId"),
        payload.get("farmID"),
        payload.get("farm"),
        local.get("graniot_farm_id"),
        local.get("farm_id"),
        settings.GRANIOT_DEFAULT_FARM_ID,
    ]
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

    return await _create_default_farm(client)


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
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    client = GraniotClient()
    try:
        # Prefer the endpoint that Graniot exposes specifically for WMS-capable layers.
        # This keeps the satellite UI aligned with the layers that can actually be rendered.
        attempts = [
            ("/api/layers/get_wms_layers/", {}),
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
                    layers = [_normalize_layer(item) for item in items]
                    def _priority(value: Any) -> int:
                        try:
                            return 999999 if value is None else int(value)
                        except Exception:
                            return 999999

                    layers.sort(key=lambda x: (
                        _priority(x.get("menu_priority")),
                        str(x.get("layer_resolution") or ""),
                        str(x.get("displayed_name") or ""),
                    ))
                    return {"data": layers, "raw": raw, "error": None, "count": len(layers), "source_path": path}
            except Exception as exc:
                last_error = exc
                continue

        if last_error:
            raise last_error
        return {"data": [], "raw": raw, "error": None, "count": 0}
    except Exception as exc:
        _raise_graniot_error(exc)


@router.get("/layers/resolutions")
async def list_resolutions(
    search: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    client = GraniotClient()
    try:
        raw = await client.get("/api/layersresolution/", params={"search": search})
        resolutions = [_normalize_resolution(item) for item in _items(raw)]
        return {"data": resolutions, "raw": raw, "error": None, "count": len(resolutions)}
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


@router.post("/parcels/sync-local/{parcel_id}")
async def sync_local_parcel(
    parcel_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    client = GraniotClient()

    with LOCK:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
    if not local:
        raise HTTPException(status_code=404, detail="Lote local no encontrado")

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
        "local_existing_graniot": {
            "graniot_farm_id": local.get("graniot_farm_id"),
            "graniot_parcel_id": local.get("graniot_parcel_id"),
            "graniot_parcel_key": local.get("graniot_parcel_key"),
            "has_access_key": bool(local.get("graniot_access_key")),
        },
        "feature_count": len(feature_collection.get("features") or []),
        "main_geometry_type": main_geometry.get("type"),
    })

    try:
        farm_id = await _resolve_farm_id(client, local, payload)

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

        if local.get("graniot_parcel_id") and payload.get("force_update"):
            patch_payload = _build_graniot_single_parcel_payload(graniot_payload)
            raw = await client.patch(
                f"/api/parcels/{local['graniot_parcel_id']}/",
                json_body=patch_payload,
                params=None,
                debug_context={
                    "operation": "sync-local-parcel",
                    "attempt": "force-update-confirmed-geom-payload",
                    "local_parcel_id": parcel_id,
                    "farm_id": farm_id,
                    "farm_ref": farm_ref,
                },
            )
            last_payload_error = _payload_error_message(raw)
            if last_payload_error:
                raise GraniotAPIError(400, last_payload_error, raw)
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
                "updated_at": t,
            })
            write_db(db)
            result = _public_parcel(row)
        return {"data": {"parcel": result, "graniot": raw, "farm_id": farm_id}, "error": None}
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


@router.get("/parcels/{parcel_id}/resolutions/{resolution_key}/dates")
async def get_dates(
    parcel_id: str,
    resolution_key: str,
    authorization: Optional[str] = Header(default=None),
):
    _require_user(authorization)
    client = GraniotClient()
    try:
        raw = await client.get(f"/api/parcels/{parcel_id}/resolutions/{resolution_key}/dates/")
        return {"data": raw, "error": None}
    except GraniotAPIError as exc:
        # Some Graniot layers/resolutions simply do not expose a date catalog.
        # Returning 200 with an empty list avoids noisy browser 404/500 errors;
        # the frontend will render the layer with the latest image instead.
        if exc.status_code in {400, 404, 500, 502}:
            return {"data": [], "error": str(exc), "warning": True}
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
    client = GraniotClient()
    try:
        raw = await client.get(
            f"/api/parcels/{parcel_id}/layers/{layer_key}/statistics/",
            params={"from_date": from_date, "to_date": to_date, "maxcc": maxcc},
        )
        return {"data": raw, "error": None}
    except GraniotAPIError as exc:
        # Statistics are optional in Graniot and can fail for layers that still
        # render correctly as WMS. Do not block the satellite map for this.
        if exc.status_code in {400, 404, 500, 502}:
            return {"data": {"status": "unavailable", "data": []}, "error": str(exc), "warning": True}
        _raise_graniot_error(exc)
    except Exception as exc:
        _raise_graniot_error(exc)


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

    local: Optional[Dict[str, Any]] = None
    if parcel_id:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id), None)
        if local and not access_key:
            access_key = local.get("graniot_access_key") or local.get("graniot_parcel_key")

    access_key = str(access_key or "").strip()
    if not access_key:
        raise HTTPException(status_code=400, detail="access_key requerido")

    client = GraniotClient()

    local_graniot_parcel_id = str(local.get("graniot_parcel_id")) if local and local.get("graniot_parcel_id") else None
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
    # frontend. In both cases /api/wms/ returns {"Invalid access key."}. Recover
    # the current properties.image_url from Graniot and use its signed token.
    needs_signed_recovery = bool(local) and not signed_template_access_key and (bool(access_key) or bool(local_graniot_parcel_id))
    if needs_signed_recovery:
        recovered_wms_data = await _recover_wms_data_from_graniot(
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
            _store_recovered_wms_data(local, recovered_wms_data)

    if signed_template_access_key:
        access_key = str(signed_template_access_key).strip()
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
    variants = _dedupe_wms_variants(variants)[:18]

    if not variants:
        raise HTTPException(status_code=400, detail="No se pudo construir la solicitud WMS para Graniot")

    errors: List[Dict[str, Any]] = []

    # /api/wms/ is documented by Graniot as access_key-based and public
    # (`security: - {}`). Try without auth first to match the spec and to avoid
    # Graniot returning JSON/500/502 because of an unexpected API-key header.
    # Authenticated requests remain as fallback for private deployments.
    for variant_index, params in enumerate(variants, start=1):
        for use_auth in (False, True):
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

                request_bounds = _bounds_from_wms_params(params)
                clip_bounds = request_bounds or bbox_values or _clip_bounds_from_context(template, bbox_values, local)
                clip_geometry, clip_geometry_source = _clip_geometry_from_payload(local, template_params, recovered_wms_data)
                content, final_media_type, backend_clip_applied, clip_info = _apply_backend_polygon_mask(
                    raw.content,
                    media_type=media_type,
                    bounds=clip_bounds,
                    geometry=clip_geometry,
                )

                if backend_clip_applied:
                    log_event({
                        "event": "dataris.graniot.wms_proxy.backend_clip_applied",
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
                else:
                    log_event({
                        "event": "dataris.graniot.wms_proxy.backend_clip_skipped",
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
                    })

                return Response(
                    content=content,
                    media_type=final_media_type,
                    headers={"Cache-Control": "public, max-age=300"},
                )
            except GraniotAPIError as exc:
                errors.append({
                    "variant_index": variant_index,
                    "use_auth": use_auth,
                    "status_code": exc.status_code,
                    "message": str(exc),
                    "payload": safe_payload(exc.payload),
                })
                continue
            except Exception as exc:
                errors.append({
                    "variant_index": variant_index,
                    "use_auth": use_auth,
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                })
                continue

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
    raise HTTPException(status_code=502, detail={"message": hint, "attempts": errors[-8:]})
