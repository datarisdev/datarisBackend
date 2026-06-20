from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Column, DateTime, Float, ForeignKey,
    Index, Integer, String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.models.base import Base


class HarvestSession(Base):
    __tablename__ = "harvest_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parcel_id = Column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name = Column(String(255), nullable=False)
    # "COSECHA_MECANICA" | "YIELD_MONITOR" | "UNKNOWN"
    source_format = Column(String(50), nullable=False, default="COSECHA_MECANICA")

    # Metadata extraída del CSV
    equipo = Column(String(255), nullable=True)
    operador = Column(String(255), nullable=True)
    responsable = Column(String(255), nullable=True)
    zafra = Column(String(100), nullable=True)
    finca = Column(String(255), nullable=True)
    lote = Column(String(255), nullable=True)
    fecha_inicio = Column(DateTime, nullable=True)
    fecha_fin = Column(DateTime, nullable=True)

    # Stats pre-calculadas al momento del upload
    total_points = Column(Integer, nullable=False, default=0)
    bbox = Column(JSONB, nullable=True)   # {min_lng, min_lat, max_lng, max_lat}
    stats = Column(JSONB, nullable=True)  # promedios, distribución por estado, etc.

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    points = relationship(
        "HarvestPoint",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class HarvestPoint(Base):
    """Un punto GPS de telemetría de cosechadora.

    Se usa BigInteger autoincrement como PK (no UUID) para maximizar rendimiento
    en bulk inserts de 50k+ filas y permitir downsampling eficiente por stride.
    """

    __tablename__ = "harvest_points"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("harvest_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Coordenadas
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=True)

    # Telemetría cosechadora
    velocidad_kmh = Column(Float, nullable=True)
    presion_cortador_bar = Column(Float, nullable=True)
    tch = Column(Float, nullable=True)   # Tons/hectárea
    tah = Column(Float, nullable=True)   # Tons/área/hora
    rpm = Column(Integer, nullable=True)
    combustible = Column(Float, nullable=True)
    calidad_gps = Column(Integer, nullable=True)
    piloto_automatico = Column(String(50), nullable=True)
    modo_corte = Column(String(100), nullable=True)

    # Campos yield monitor
    rendimiento_kg_ha = Column(Float, nullable=True)
    peso_carga_kg = Column(Float, nullable=True)
    ancho_faena_cm = Column(Float, nullable=True)

    # Estado calculado: cosechando | traslado | ralenti | detenido
    estado = Column(String(20), nullable=False, default="detenido")

    session = relationship("HarvestSession", back_populates="points")

    __table_args__ = (
        Index("ix_harvest_points_session", "session_id"),
        Index("ix_harvest_points_session_lat_lng", "session_id", "lat", "lng"),
        Index("ix_harvest_points_session_estado", "session_id", "estado"),
    )
