from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Response
from shapely.geometry import mapping, shape as shapely_shape
from shapely.ops import unary_union

from app.api.routers.compat import LOCK, bearer_user, now, read_db, table, write_db
from app.services.graniot_client import GraniotAPIError, GraniotClient, GraniotNotConfigured

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


def _normalize_layer(layer: Dict[str, Any]) -> Dict[str, Any]:
    displayed = layer.get("displayed_name") or layer.get("display_name") or layer.get("name") or "Capa"
    key = str(layer.get("key") or layer.get("layer_key") or layer.get("name") or layer.get("id") or "")
    resolution = layer.get("layer_resolution") or layer.get("resolution") or layer.get("resolution_name")
    return {
        "id": layer.get("id"),
        "key": key,
        "name": layer.get("name") or displayed,
        "displayed_name": displayed,
        "label": displayed,
        "color": layer.get("color"),
        "legend": layer.get("legend"),
        "config": layer.get("config"),
        "layer_stats": layer.get("layer_stats"),
        "layer_resolution": resolution,
        "resolution": layer.get("resolution"),
        "is_sentinel": layer.get("is_sentinel"),
        "is_active": layer.get("is_active"),
        "is_experimental": layer.get("is_experimental"),
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


def _extract_graniot_ids(response: Any) -> Dict[str, Any]:
    obj = response
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        obj = response["data"]
    if isinstance(obj, dict) and isinstance(obj.get("results"), list) and obj["results"]:
        obj = obj["results"][0]
    props = obj.get("properties") if isinstance(obj, dict) else {}
    props = props or {}
    return {
        "graniot_parcel_id": obj.get("id") if isinstance(obj, dict) else None,
        "graniot_parcel_key": obj.get("key") or props.get("key") if isinstance(obj, dict) else None,
        "graniot_access_key": props.get("key") or obj.get("key") if isinstance(obj, dict) else None,
        "graniot_wms_url": props.get("wms_url") if isinstance(props, dict) else None,
    }


def _public_parcel(row: Dict[str, Any]) -> Dict[str, Any]:
    return dict(row)


@router.get("/status")
def status(authorization: Optional[str] = Header(default=None)):
    _require_user(authorization)
    client = GraniotClient()
    return {
        "data": {
            "configured": client.is_configured,
            "base_url": client.base_url,
            "auth_header": client.auth_header,
            "client_id_configured": bool(client.client_id),
        },
        "error": None,
    }


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
        path = "/api/layers/layers-platform/" if platform else "/api/layers/"
        try:
            raw = await client.get(path, params={"resolution_id": resolution_id, "resolution_name": resolution_name})
        except GraniotAPIError:
            # Some API keys may not have access to the platform-only list.
            # Fall back to the public layer list declared in the OpenAPI spec.
            raw = await client.get("/api/layers/", params={"resolution_id": resolution_id, "resolution_name": resolution_name})
        layers = [_normalize_layer(item) for item in _items(raw)]
        layers.sort(key=lambda x: (str(x.get("layer_resolution") or ""), str(x.get("displayed_name") or "")))
        return {"data": layers, "raw": raw, "error": None, "count": len(layers)}
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

    feature_payload = {
        "type": "Feature",
        "geometry": main_geometry,
        "properties": {
            "name": payload.get("name") or local.get("name") or "Lote DATARIS",
            "is_active": True,
            "metadata": metadata,
        },
    }
    fallback_payload = {
        "name": payload.get("name") or local.get("name") or "Lote DATARIS",
        "parcelGeoJson": feature_collection,
        "metadata": metadata,
    }

    try:
        if local.get("graniot_parcel_id") and payload.get("force_update"):
            raw = await client.patch(f"/api/parcels/{local['graniot_parcel_id']}/", json_body=fallback_payload)
        else:
            try:
                raw = await client.post("/api/parcels/", json_body=feature_payload)
            except GraniotAPIError:
                raw = await client.post("/api/parcels/", json_body=fallback_payload)

        ids = _extract_graniot_ids(raw)
        t = now()
        with LOCK:
            db = read_db()
            row = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
            if not row:
                raise HTTPException(status_code=404, detail="Lote local no encontrado")
            row.update({
                **{k: v for k, v in ids.items() if v is not None},
                "graniot_synced_at": t,
                "graniot_sync_error": None,
                "graniot_raw": raw,
                "updated_at": t,
            })
            write_db(db)
            result = _public_parcel(row)
        return {"data": {"parcel": result, "graniot": raw}, "error": None}
    except Exception as exc:
        error_message = str(exc)
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
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    if not access_key and parcel_id:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id and p.get("user_id") == user["id"]), None)
        if local:
            access_key = local.get("graniot_access_key") or local.get("graniot_parcel_key")
    if not access_key:
        raise HTTPException(status_code=400, detail="El lote no está sincronizado con Graniot o no tiene access_key")

    base = "/api/graniot/wms-proxy"
    query = f"parcel_id={parcel_id or ''}&access_key={access_key}&layer={layer}&width={width}&height={height}"
    if time:
        query += f"&time={time}"
    return {"data": {"url": f"{base}?{query}"}, "error": None}


@router.get("/wms-proxy")
async def wms_proxy(
    parcel_id: Optional[str] = Query(default=None),
    access_key: Optional[str] = Query(default=None),
    layer: str = Query(...),
    time: Optional[str] = Query(default=None),
    width: int = Query(default=768),
    height: int = Query(default=768),
):
    """Image proxy for Leaflet ImageOverlay.

    Leaflet cannot attach Authorization headers to image overlays, so this route
    resolves the Graniot access_key on the backend and returns the binary image.
    """
    if not access_key and parcel_id:
        db = read_db()
        local = next((p for p in table(db, "parcels") if p.get("id") == parcel_id), None)
        if local:
            access_key = local.get("graniot_access_key") or local.get("graniot_parcel_key")

    if not access_key:
        raise HTTPException(status_code=400, detail="access_key requerido")

    client = GraniotClient()
    try:
        raw = await client.binary_get(
            "/api/wms/",
            params={
                "access_key": access_key,
                "layers": layer,
                "time": time,
                "width": width,
                "height": height,
                "response_format": "image/png",
            },
        )
        media_type = raw.headers.get("content-type") or "image/png"
        return Response(content=raw.content, media_type=media_type)
    except Exception as exc:
        _raise_graniot_error(exc)
