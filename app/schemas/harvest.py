from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_validator


class HarvestSessionStats(BaseModel):
    avg_velocidad: Optional[float] = None
    avg_tch: Optional[float] = None
    avg_tah: Optional[float] = None
    avg_rpm: Optional[float] = None
    avg_presion: Optional[float] = None
    avg_combustible: Optional[float] = None
    pct_cosechando: Optional[float] = None
    pct_traslado: Optional[float] = None
    pct_ralenti: Optional[float] = None
    pct_detenido: Optional[float] = None
    pct_piloto_auto: Optional[float] = None
    eficiencia_field: Optional[float] = None  # pct_cosechando


class HarvestSessionOut(BaseModel):
    id: str
    user_id: str
    parcel_id: Optional[str] = None
    name: str
    source_format: str
    equipo: Optional[str] = None
    operador: Optional[str] = None
    responsable: Optional[str] = None
    zafra: Optional[str] = None
    finca: Optional[str] = None
    lote: Optional[str] = None
    fecha_inicio: Optional[datetime] = None
    fecha_fin: Optional[datetime] = None
    total_points: int
    bbox: Optional[dict[str, float]] = None
    stats: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @field_validator("id", "user_id", "parcel_id", mode="before")
    @classmethod
    def uuid_to_str(cls, v: Any) -> Optional[str]:
        return str(v) if v is not None else None


class HarvestSessionList(BaseModel):
    sessions: list[HarvestSessionOut]
    total: int


class HarvestUploadResponse(BaseModel):
    session: HarvestSessionOut
    warnings: list[str] = []
