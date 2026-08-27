"""Modelo de datos de la Bitácora de Campo.

`category`, `status` y `source` se guardan como texto y no como ENUM de
PostgreSQL a propósito: el enum nativo obliga a una migración por cada valor
nuevo y ya dio problemas de serialización en ml_training (ver el docstring de
`alembic/versions/8e85bcc901c8_ml_training_tables.py`). La validación de los
valores admitidos vive en `schemas.py`, que es donde entra el dato externo.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base

# El vocabulario de la bitácora vive en `catalog.py`, sin dependencias del
# ORM, para que los cálculos y el conector de AgtechApps puedan importarlo
# sin cargar los modelos. Se reexporta aquí porque es donde lo buscan el
# resto de módulos desde que existe la bitácora.
from app.modules.field_log.catalog import (  # noqa: F401,E402
    CATEGORY_LABELS,
    CYCLE_STATUSES,
    ENTRY_SOURCES,
    LOG_CATEGORIES,
)


class CropCycle(Base):
    """Un ciclo productivo sobre una parcela (o sobre una sección de ella).

    Sustituye funcionalmente a `parcel_crops`, que es 1-a-1 con la parcela y
    por eso no permite histórico. `parcel_crops` se mantiene intacto para no
    romper sus endpoints.
    """

    __tablename__ = "crop_cycles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    parcel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    crop_type: Mapped[str] = mapped_column(String(120), nullable=False)
    variety: Mapped[str | None] = mapped_column(String(180), nullable=True)
    template_key: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Sección/sub-lote opcional dentro de la parcela: una "validación" de un
    # centro de desarrollo tecnológico es un ensayo sobre parte del lote, pero
    # un productor no subdivide nada y deja estos dos campos vacíos.
    section_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    section_geometry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    area_ha: Mapped[float | None] = mapped_column(Float, nullable=True)
    planting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    harvest_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="MXN")

    target_price_per_ton: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_yield_ton_ha: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_yield_ton_ha: Mapped[float | None] = mapped_column(Float, nullable=True)

    budget_per_ha: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Atributos del ciclo que en la hoja viven sueltos entre los bloques
    # (tipo de labranza, % de cobertura, nº de camas, densidades, fórmula NPK).
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    parcel = relationship("Parcel")
    user = relationship("User")
    entries = relationship(
        "FieldLogEntry",
        back_populates="cycle",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    phenology = relationship(
        "PhenologyRecord",
        back_populates="cycle",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_crop_cycles_parcel_status", "parcel_id", "status"),
        Index("ix_crop_cycles_user_created", "user_id", "created_at"),
    )


class FieldLogEntry(Base):
    """Una labor registrada: la fila de la hoja de cálculo.

    Las columnas transversales (costo, fecha, ubicación) son columnas reales
    porque se filtran y se agregan en SQL; lo específico de cada categoría
    (litros de diesel, kWh del riego, unidades de N…) vive en `data`, descrito
    por la plantilla del ciclo.
    """

    __tablename__ = "field_log_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crop_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parcel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    category: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False)

    unit: Mapped[str | None] = mapped_column(String(60), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Derivado de cantidad × costo unitario, pero persistido para poder sumar
    # el ciclo entero en una sola consulta.
    cost_per_ha: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    performed_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    observations: Mapped[str | None] = mapped_column(Text, nullable=True)

    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # None = no se pudo comprobar (sin GPS o parcela sin geometría).
    location_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    photos: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    # Identificador generado por el cliente antes de guardar. Es lo que hace
    # idempotente la sincronización de la cola offline: el móvil puede reenviar
    # el mismo registro N veces sin duplicarlo.
    client_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    source: Mapped[str] = mapped_column(String(20), nullable=False, default="web")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cycle = relationship("CropCycle", back_populates="entries")
    user = relationship("User")
    inputs = relationship(
        "FieldLogEntryInput",
        back_populates="entry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "client_uuid", name="uq_field_log_entries_client_uuid"),
        Index("ix_field_log_entries_cycle_category", "cycle_id", "category"),
        Index("ix_field_log_entries_cycle_performed", "cycle_id", "performed_at"),
    )


class FieldLogEntryInput(Base):
    """Producto aplicado dentro de una labor.

    Va aparte de la entrada porque una aplicación real lleva una mezcla de dos
    a cuatro productos; la hoja de cálculo los aplana en una fila y ahí es
    donde se pierde la trazabilidad del ingrediente activo.
    """

    __tablename__ = "field_log_entry_inputs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_log_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    active_ingredient: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Gramos de ingrediente activo por litro (o por kilo) de producto comercial.
    ia_concentration: Mapped[float | None] = mapped_column(Float, nullable=True)

    dose: Mapped[float | None] = mapped_column(Float, nullable=True)
    dose_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Gramos totales de i.a. por hectárea aportados por este producto.
    ia_grams: Mapped[float | None] = mapped_column(Float, nullable=True)

    n_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    k_units: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entry = relationship("FieldLogEntry", back_populates="inputs")


class PhenologyRecord(Base):
    """Etapa fenológica observada. La escala depende del cultivo."""

    __tablename__ = "phenology_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crop_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stage_code: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    observed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    observations: Mapped[str | None] = mapped_column(Text, nullable=True)

    photos: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    location_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    client_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cycle = relationship("CropCycle", back_populates="phenology")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("cycle_id", "stage_code", name="uq_phenology_cycle_stage"),
    )


class FieldLogTemplate(Base):
    """Plantilla de bitácora creada por un usuario.

    Las plantillas del sistema (CDT FIRA, caña, genérica) viven en
    `templates.py` y no ocupan filas: así no hacen falta migraciones de datos
    para corregirlas ni se desincronizan entre entornos.
    """

    __tablename__ = "field_log_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    crop_type: Mapped[str | None] = mapped_column(String(120), nullable=True)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_field_log_templates_user_key"),
    )


class FieldLogLaborStandard(Base):
    """Rendimiento de referencia de una labor (horas por hectárea).

    Sale de la tabla de horas de trabajo de la hoja original y sirve para
    estimar tiempo y costo de maquinaria antes de ejecutar la labor.
    """

    __tablename__ = "field_log_labor_standards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    labor_name: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    hours_per_ha: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_l_per_ha: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "labor_name", name="uq_labor_standards_user_labor"),
    )
