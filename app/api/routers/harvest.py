"""Router de cosecha mecánica.

Endpoints:
  POST   /harvest/upload         – Sube y parsea un CSV; crea HarvestSession + HarvestPoints en batch
  GET    /harvest/               – Lista sesiones del usuario
  GET    /harvest/{id}           – Detalle de sesión con stats
  GET    /harvest/{id}/geojson   – GeoJSON optimizado para Mapbox GL (con bbox, zoom y filtros)
  DELETE /harvest/{id}           – Elimina sesión y puntos (CASCADE)

Optimizaciones de rendimiento:
- Batch insert de 2 000 filas por bloque (50k puntos → 25 operaciones vs 50k individuales)
- Downsampling por zoom: stride 1/4/16 según nivel
- Filtrado espacial por bbox antes de devolver el GeoJSON
- GZip compresión ya habilitado en main.py (GZipMiddleware)
- Stats pre-computadas en upload, nunca recalculadas en lectura
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.harvest import HarvestPoint, HarvestSession
from app.models.user import User
from app.schemas.harvest import HarvestSessionList, HarvestSessionOut, HarvestUploadResponse
from app.services.harvest.csv_parser import (
    compute_bbox,
    compute_stats,
    parse_harvest_csv,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/harvest", tags=["harvest"])

_BATCH_SIZE = 2_000
_MAX_FILE_MB = 50
_STRIDE_BY_ZOOM: dict[int, int] = {
    0: 64, 1: 64, 2: 32, 3: 32, 4: 16, 5: 16,
    6: 16, 7: 8,  8: 8,  9: 4,  10: 4, 11: 4,
    12: 2, 13: 2, 14: 1, 15: 1, 16: 1,
}


# ── Upload ─────────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=HarvestUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_harvest_csv(
    file: UploadFile = File(...),
    name: str = Form(...),
    parcel_id: Optional[str] = Form(None),
    zafra: Optional[str] = Form(None),
    responsable: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    # Validar extensión
    fname = (file.filename or "").lower()
    if not (fname.endswith(".csv") or fname.endswith(".txt")):
        raise HTTPException(400, "Solo se aceptan archivos CSV (.csv, .txt)")

    content = await file.read()
    if len(content) > _MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"El archivo supera el límite de {_MAX_FILE_MB} MB")

    warnings: list[str] = []

    try:
        points, fmt, meta = parse_harvest_csv(content)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        logger.exception("Error parseando CSV de cosecha")
        raise HTTPException(500, f"Error procesando el archivo: {exc}")

    if not points:
        raise HTTPException(422, "El CSV no contiene puntos GPS válidos")

    if len(points) > 200_000:
        warnings.append(f"El CSV tiene {len(points):,} puntos; se cargaron todos.")

    # Pre-calcular stats y bbox
    stats = compute_stats(points, fmt)
    bbox = compute_bbox(points)

    parcel_uuid = None
    if parcel_id:
        try:
            parcel_uuid = uuid.UUID(parcel_id)
        except ValueError:
            warnings.append("parcel_id no es un UUID válido; se ignoró.")

    session = HarvestSession(
        user_id=current_user.id,
        parcel_id=parcel_uuid,
        name=name,
        source_format=fmt,
        equipo=meta.get("equipo"),
        operador=meta.get("operador"),
        responsable=responsable or meta.get("responsable"),
        zafra=zafra or meta.get("zafra"),
        finca=meta.get("finca"),
        lote=meta.get("lote"),
        total_points=len(points),
        bbox=bbox,
        stats=stats,
    )
    db.add(session)
    db.flush()  # obtiene session.id sin commit

    # Bulk insert en batches para máximo rendimiento
    session_id = session.id
    for i in range(0, len(points), _BATCH_SIZE):
        batch = [
            {**p, "session_id": session_id}
            for p in points[i : i + _BATCH_SIZE]
        ]
        db.execute(insert(HarvestPoint), batch)

    db.commit()
    db.refresh(session)

    return {"session": HarvestSessionOut.model_validate(session), "warnings": warnings}


# ── Listar sesiones ────────────────────────────────────────────────────────────

@router.get("/", response_model=HarvestSessionList)
def list_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    sessions = (
        db.query(HarvestSession)
        .filter(HarvestSession.user_id == current_user.id)
        .order_by(HarvestSession.created_at.desc())
        .all()
    )
    return {"sessions": [HarvestSessionOut.model_validate(s) for s in sessions], "total": len(sessions)}


# ── Detalle de sesión ──────────────────────────────────────────────────────────

@router.get("/{session_id}", response_model=HarvestSessionOut)
def get_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    session = (
        db.query(HarvestSession)
        .filter(HarvestSession.id == session_id, HarvestSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    return HarvestSessionOut.model_validate(session)


# ── GeoJSON optimizado ─────────────────────────────────────────────────────────

@router.get("/{session_id}/geojson")
def get_harvest_geojson(
    session_id: uuid.UUID,
    zoom: Optional[int] = None,
    bbox: Optional[str] = None,
    estado: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Devuelve GeoJSON para Mapbox GL con todas las optimizaciones:

    - bbox: "min_lng,min_lat,max_lng,max_lat" — filtro espacial
    - zoom: nivel de zoom Mapbox (0-22) — downsampling automático
    - estado: cosechando|traslado|ralenti|detenido — filtro de estado
    """
    session = (
        db.query(HarvestSession)
        .filter(HarvestSession.id == session_id, HarvestSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Sesión no encontrada")

    query = db.query(HarvestPoint).filter(HarvestPoint.session_id == session_id)

    # Filtro espacial por bbox
    if bbox:
        try:
            min_lng, min_lat, max_lng, max_lat = [float(x) for x in bbox.split(",")]
            query = query.filter(
                HarvestPoint.lat >= min_lat,
                HarvestPoint.lat <= max_lat,
                HarvestPoint.lng >= min_lng,
                HarvestPoint.lng <= max_lng,
            )
        except ValueError:
            raise HTTPException(400, "bbox debe tener formato: min_lng,min_lat,max_lng,max_lat")

    # Filtro de estado
    if estado and estado in ("cosechando", "traslado", "ralenti", "detenido"):
        query = query.filter(HarvestPoint.estado == estado)

    # Cargar puntos ordenados por id (para que el downsampling sea determinista)
    points = query.order_by(HarvestPoint.id).all()

    # Downsampling por zoom — devuelve cada stride-avo punto
    stride = _STRIDE_BY_ZOOM.get(zoom or 16, 1) if zoom is not None else 1
    if stride > 1:
        points = points[::stride]

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [p.lng, p.lat]},
            "properties": {
                "id": p.id,
                "estado": p.estado,
                "velocidad": round(p.velocidad_kmh, 2) if p.velocidad_kmh is not None else None,
                "tch": round(p.tch, 2) if p.tch is not None else None,
                "tah": round(p.tah, 2) if p.tah is not None else None,
                "rpm": p.rpm,
                "presion": round(p.presion_cortador_bar, 2) if p.presion_cortador_bar is not None else None,
                "combustible": round(p.combustible, 2) if p.combustible is not None else None,
                "piloto_auto": p.piloto_automatico,
                "rendimiento": round(p.rendimiento_kg_ha, 2) if p.rendimiento_kg_ha is not None else None,
            },
        }
        for p in points
    ]

    return JSONResponse(
        content={
            "type": "FeatureCollection",
            "features": features,
            "_meta": {
                "total_returned": len(features),
                "stride": stride,
                "session_total": session.total_points,
            },
        }
    )


# ── Eliminar sesión ────────────────────────────────────────────────────────────

@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    session = (
        db.query(HarvestSession)
        .filter(HarvestSession.id == session_id, HarvestSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    db.delete(session)
    db.commit()
