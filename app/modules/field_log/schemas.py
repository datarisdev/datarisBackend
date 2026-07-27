"""Contratos de entrada y salida de la Bitácora de Campo."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.field_log.models import CYCLE_STATUSES, ENTRY_SOURCES, LOG_CATEGORIES

CategoryLiteral = Literal[
    "acondicionamiento",
    "siembra",
    "riego",
    "fertilizante",
    "malezas",
    "plagas",
    "enfermedades",
    "foliar",
    "diversos",
    "cosecha",
]


class LocationPayload(BaseModel):
    """Ubicación capturada junto al registro."""

    lat: float
    lng: float
    accuracy_m: float | None = None
    captured_at: datetime | None = None
    source: Literal["gps", "manual"] = "gps"

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- ciclos


class CropCycleBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    crop_type: str = Field(min_length=1, max_length=120)
    variety: str | None = None
    template_key: str | None = None
    section_name: str | None = None
    section_geometry: dict[str, Any] | None = None
    area_ha: float | None = None
    planting_date: date | None = None
    harvest_date: date | None = None
    currency: str = "MXN"
    target_price_per_ton: float | None = None
    expected_yield_ton_ha: float | None = None
    actual_yield_ton_ha: float | None = None
    budget_per_ha: float | None = None
    attributes: dict[str, Any] | None = None


class CropCycleCreate(CropCycleBase):
    status: str = "active"

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        if value not in CYCLE_STATUSES:
            raise ValueError(f"status debe ser uno de {CYCLE_STATUSES}")
        return value


class CropCycleUpdate(BaseModel):
    name: str | None = None
    crop_type: str | None = None
    variety: str | None = None
    template_key: str | None = None
    section_name: str | None = None
    section_geometry: dict[str, Any] | None = None
    area_ha: float | None = None
    planting_date: date | None = None
    harvest_date: date | None = None
    status: str | None = None
    currency: str | None = None
    target_price_per_ton: float | None = None
    expected_yield_ton_ha: float | None = None
    actual_yield_ton_ha: float | None = None
    budget_per_ha: float | None = None
    attributes: dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str | None) -> str | None:
        if value is not None and value not in CYCLE_STATUSES:
            raise ValueError(f"status debe ser uno de {CYCLE_STATUSES}")
        return value


class CropCycleResponse(CropCycleBase):
    id: UUID
    parcel_id: UUID
    user_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime

    # Añadidos por el servicio para que la lista de ciclos no necesite N+1.
    parcel_name: str | None = None
    entry_count: int | None = None
    total_cost_per_ha: float | None = None
    last_entry_at: date | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- insumos


class EntryInputBase(BaseModel):
    product_name: str = Field(min_length=1, max_length=200)
    active_ingredient: str | None = None
    ia_concentration: float | None = None
    dose: float | None = None
    dose_unit: str | None = None
    ia_grams: float | None = None
    n_units: float | None = None
    p_units: float | None = None
    k_units: float | None = None


class EntryInputCreate(EntryInputBase):
    pass


class EntryInputResponse(EntryInputBase):
    id: UUID
    entry_id: UUID

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- labores


class FieldLogEntryBase(BaseModel):
    category: CategoryLiteral
    description: str = Field(min_length=1, max_length=400)
    unit: str | None = None
    quantity: float | None = None
    unit_cost: float | None = None
    performed_at: date | None = None
    observations: str | None = None
    data: dict[str, Any] | None = None
    location: LocationPayload | None = None
    photos: list[str] | None = None


class FieldLogEntryCreate(FieldLogEntryBase):
    # El costo puede llegar calculado desde el cliente; el servidor siempre
    # recalcula a partir de cantidad × costo unitario cuando ambos existen.
    cost_per_ha: float | None = None
    client_uuid: UUID | None = None
    source: str = "web"
    inputs: list[EntryInputCreate] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _valid_source(cls, value: str) -> str:
        if value not in ENTRY_SOURCES:
            raise ValueError(f"source debe ser uno de {ENTRY_SOURCES}")
        return value


class FieldLogEntryUpdate(BaseModel):
    category: CategoryLiteral | None = None
    description: str | None = None
    unit: str | None = None
    quantity: float | None = None
    unit_cost: float | None = None
    cost_per_ha: float | None = None
    performed_at: date | None = None
    observations: str | None = None
    data: dict[str, Any] | None = None
    location: LocationPayload | None = None
    photos: list[str] | None = None
    inputs: list[EntryInputCreate] | None = None


class FieldLogEntryResponse(BaseModel):
    id: UUID
    cycle_id: UUID
    parcel_id: UUID | None
    user_id: UUID
    category: str
    category_label: str | None = None
    description: str
    unit: str | None
    quantity: float | None
    unit_cost: float | None
    cost_per_ha: float
    performed_at: date | None
    observations: str | None
    data: dict[str, Any] | None
    location: dict[str, Any] | None
    location_verified: bool | None
    photos: list[str] | None
    client_uuid: UUID | None
    source: str
    created_at: datetime
    updated_at: datetime
    inputs: list[EntryInputResponse] = Field(default_factory=list)
    author_email: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- fenología


class PhenologyCreate(BaseModel):
    stage_code: str = Field(min_length=1, max_length=40)
    stage_label: str | None = None
    observed_at: date | None = None
    observations: str | None = None
    photos: list[str] | None = None
    location: LocationPayload | None = None
    client_uuid: UUID | None = None


class PhenologyUpdate(BaseModel):
    stage_label: str | None = None
    observed_at: date | None = None
    observations: str | None = None
    photos: list[str] | None = None
    location: LocationPayload | None = None


class PhenologyResponse(BaseModel):
    id: UUID
    cycle_id: UUID
    user_id: UUID
    stage_code: str
    stage_label: str | None
    observed_at: date | None
    observations: str | None
    photos: list[str] | None
    location: dict[str, Any] | None
    location_verified: bool | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- sincronización


class SyncEntry(FieldLogEntryCreate):
    cycle_id: UUID


class SyncRequest(BaseModel):
    entries: list[SyncEntry] = Field(default_factory=list)


class SyncResultItem(BaseModel):
    client_uuid: UUID | None
    status: Literal["created", "duplicate", "error"]
    entry_id: UUID | None = None
    detail: str | None = None


class SyncResponse(BaseModel):
    created: int
    duplicates: int
    errors: int
    results: list[SyncResultItem]


# ---------------------------------------------------------------- otros


class PhotoUploadUrlRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str | None = None


class PhotoUploadUrlResponse(BaseModel):
    upload_url: str
    blob_path: str
    read_url: str | None = None
    expires_at: datetime


class LaborStandardCreate(BaseModel):
    labor_name: str = Field(min_length=1, max_length=180)
    category: CategoryLiteral | None = None
    hours_per_ha: float | None = None
    fuel_l_per_ha: float | None = None


class LaborStandardResponse(LaborStandardCreate):
    id: UUID
    user_id: UUID | None
    is_system: bool = False

    model_config = ConfigDict(from_attributes=True)


class TemplateResponse(BaseModel):
    key: str
    name: str
    description: str | None = None
    crop_type: str | None = None
    is_system: bool = False
    categories: list[dict[str, Any]] = Field(default_factory=list)
    phenology_stages: list[dict[str, Any]] = Field(default_factory=list)
    labor_standards: list[dict[str, Any]] = Field(default_factory=list)
    cycle_attributes: list[dict[str, Any]] = Field(default_factory=list)


class TemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    description: str | None = None
    crop_type: str | None = None
    definition: dict[str, Any]


class CycleSummaryResponse(BaseModel):
    cycle: CropCycleResponse
    kpis: dict[str, Any]
    template: TemplateResponse
    phenology: list[PhenologyResponse] = Field(default_factory=list)
    recent_entries: list[FieldLogEntryResponse] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)


class CategoryInfo(BaseModel):
    key: str
    label: str


CATEGORY_CHOICES: list[CategoryInfo] = [
    CategoryInfo(key=key, label=key) for key in LOG_CATEGORIES
]
