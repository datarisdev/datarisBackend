"""EOS Data Analytics satellite endpoints.

Exposes the EOSDA API Connect provider (scene search, index map-layer and
vegetation-index statistics) for parcels owned by the current user. The EOS API
key stays server-side; the frontend only talks to these endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.routers.sentinel2 import _get_owned_parcel, _parse_target_date
from app.services.eos import service as eos_service
from app.services.eos.client import EOSApiError, EOSNotConfigured

router = APIRouter(prefix="/eos", tags=["EOS Satellite"])
logger = logging.getLogger(__name__)


def _handle_eos_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, EOSNotConfigured):
        return HTTPException(status_code=503, detail="El proveedor satelital EOS no está configurado.")
    if isinstance(exc, EOSApiError):
        logger.warning("EOS API error: %s", exc)
        return HTTPException(status_code=502, detail="No se pudo consultar el proveedor satelital EOS.")
    logger.exception("EOS unexpected error")
    return HTTPException(status_code=500, detail="Error inesperado consultando EOS.")


@router.get("/status")
def status() -> dict[str, Any]:
    return {"data": eos_service.status()}


@router.get("/parcels/{parcel_id}/scenes")
def scenes(
    parcel_id: UUID | str,
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    max_cloud: Optional[float] = Query(default=None, ge=0, le=100),
    limit: int = Query(default=60, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    try:
        results = eos_service.list_scenes(
            parcel["geometry"],
            date_from=date_from,
            date_to=date_to,
            cloud_max=max_cloud,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_eos_errors(exc) from exc
    return {"data": {"scenes": results, "count": len(results)}}


@router.get("/parcels/{parcel_id}/map-layer")
def map_layer(
    parcel_id: UUID | str,
    index: str = Query(default="NDVI"),
    date: Optional[str] = Query(default=None),
    max_cloud: Optional[float] = Query(default=None, ge=0, le=100),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    target_date = _parse_target_date(date)
    try:
        result = eos_service.build_map_layer(
            parcel["geometry"],
            index=index,
            target_date=target_date,
            cloud_max=max_cloud,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_eos_errors(exc) from exc
    return {"data": result}


class PrefetchRequest(BaseModel):
    parcel_ids: list[str]
    index: Optional[str] = "NDVI"
    date: Optional[str] = None


@router.post("/parcels/prefetch")
def prefetch(
    body: PrefetchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Calienta en segundo plano la caché de la capa para unos pocos lotes del
    usuario, para que el siguiente click sea casi instantáneo. Acotado para no
    saturar el límite de EOS."""
    target_date = _parse_target_date(body.date)
    parcels: list[tuple[str, Any]] = []
    for pid in (body.parcel_ids or [])[:12]:
        try:
            parcel = _get_owned_parcel(db, pid, current_user)
        except HTTPException:
            continue  # lote inexistente o de otro usuario: se ignora
        parcels.append((str(parcel.get("id") or pid), parcel.get("geometry")))
    if parcels:
        background_tasks.add_task(
            eos_service.prefetch_map_layers,
            parcels,
            index=body.index,
            target_date=target_date,
        )
    return {"data": {"queued": len(parcels)}}


@router.get("/parcels/{parcel_id}/statistics")
def statistics(
    parcel_id: UUID | str,
    indices: str = Query(default="NDVI", description="Índices separados por coma, máximo 3"),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    index_list = [i.strip() for i in (indices or "NDVI").split(",") if i.strip()]
    try:
        result = eos_service.statistics_series(
            parcel["geometry"],
            indices=index_list,
            date_start=date_from,
            date_end=date_to,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_eos_errors(exc) from exc
    return {"data": result}


@router.get("/statistics/tasks/{task_id}")
def statistics_task(
    task_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        result = eos_service.statistics_by_task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_eos_errors(exc) from exc
    return {"data": result}
