"""API de la Bitácora de Campo."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.modules.field_log import repository as repo
from app.modules.field_log import service
from app.modules.field_log.export_xlsx import build_workbook, default_file_name
from app.modules.field_log.import_xlsx import parse_workbook
from app.modules.field_log.models import CATEGORY_LABELS, LOG_CATEGORIES, FieldLogTemplate
from app.modules.field_log.schemas import (
    CropCycleCreate,
    CropCycleResponse,
    CropCycleUpdate,
    CycleSummaryResponse,
    FieldLogEntryCreate,
    FieldLogEntryResponse,
    FieldLogEntryUpdate,
    LaborStandardCreate,
    LaborStandardResponse,
    PhenologyCreate,
    PhenologyResponse,
    PhotoUploadUrlRequest,
    PhotoUploadUrlResponse,
    SyncRequest,
    SyncResponse,
    TemplateCreate,
    TemplateResponse,
)
from app.modules.field_log.storage import resolve_read_urls

router = APIRouter(prefix="/field-log", tags=["Bitácora de Campo"])
parcel_router = APIRouter(prefix="/parcels/{parcel_id}/cycles", tags=["Bitácora de Campo"])

MAX_IMPORT_BYTES = 10 * 1024 * 1024


# ------------------------------------------------------------------ catálogos


@router.get("/categories")
def list_categories(current_user: dict = Depends(get_current_user)) -> list[dict[str, str]]:
    return [{"key": key, "label": CATEGORY_LABELS[key]} for key in LOG_CATEGORIES]


@router.get("/templates", response_model=list[TemplateResponse])
def list_templates(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return service.available_templates(db, current_user["id"])


@router.post("/templates", response_model=TemplateResponse, status_code=201)
def create_template(
    payload: TemplateCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    existing = repo.get_user_template(db, current_user["id"], payload.key)
    if existing:
        raise HTTPException(status_code=409, detail="Ya existe una plantilla con esa clave")

    template = FieldLogTemplate(
        user_id=UUID(str(current_user["id"])),
        key=payload.key,
        name=payload.name,
        description=payload.description,
        crop_type=payload.crop_type,
        definition=payload.definition,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    definition = dict(template.definition or {})
    definition.update(
        {
            "key": template.key,
            "name": template.name,
            "description": template.description,
            "crop_type": template.crop_type,
            "is_system": False,
        }
    )
    definition.setdefault("categories", [])
    definition.setdefault("phenology_stages", [])
    definition.setdefault("labor_standards", [])
    definition.setdefault("cycle_attributes", [])
    return definition


@router.get("/labor-standards", response_model=list[LaborStandardResponse])
def list_labor_standards(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return service.labor_standards(db, current_user["id"])


@router.post("/labor-standards", response_model=LaborStandardResponse, status_code=201)
def create_labor_standard(
    payload: LaborStandardCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    row = service.create_labor_standard(
        db, user_id=UUID(str(current_user["id"])), payload=payload
    )
    return {
        "id": row.id,
        "user_id": row.user_id,
        "is_system": False,
        "labor_name": row.labor_name,
        "category": row.category,
        "hours_per_ha": row.hours_per_ha,
        "fuel_l_per_ha": row.fuel_l_per_ha,
    }


@router.get("/suggestions")
def suggestions(
    category: str = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Descripciones e importes ya usados por el técnico en esa categoría."""
    if category not in LOG_CATEGORIES:
        raise HTTPException(status_code=400, detail="Categoría no válida")
    return repo.recent_descriptions(db, user_id=current_user["id"], category=category)


# ------------------------------------------------------------------ ciclos


@parcel_router.get("", response_model=list[CropCycleResponse])
def list_parcel_cycles(
    parcel_id: UUID = Path(...),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    service.assert_parcel_access(db, parcel_id, current_user)
    cycles = repo.list_cycles(
        db, user_id=current_user["id"], parcel_id=parcel_id, status=status
    )
    return service.serialize_cycles(db, cycles)


@parcel_router.post("", response_model=CropCycleResponse, status_code=201)
def create_cycle(
    parcel_id: UUID = Path(...),
    payload: CropCycleCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.create_cycle(
        db, parcel_id=parcel_id, payload=payload, current_user=current_user
    )
    return service.serialize_cycles(db, [cycle])[0]


@router.get("/cycles", response_model=list[CropCycleResponse])
def list_cycles(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycles = repo.list_cycles(db, user_id=current_user["id"], status=status)
    return service.serialize_cycles(db, cycles)


@router.get("/cycles/{cycle_id}", response_model=CropCycleResponse)
def get_cycle(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    return service.serialize_cycles(db, [cycle])[0]


@router.patch("/cycles/{cycle_id}", response_model=CropCycleResponse)
def update_cycle(
    cycle_id: UUID = Path(...),
    payload: CropCycleUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    cycle = service.update_cycle(db, cycle=cycle, payload=payload)
    return service.serialize_cycles(db, [cycle])[0]


@router.delete("/cycles/{cycle_id}", status_code=204)
def delete_cycle(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    service.delete_cycle(db, cycle)


@router.get("/cycles/{cycle_id}/summary", response_model=CycleSummaryResponse)
def cycle_summary(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    return service.build_summary(db, cycle=cycle)


@router.get("/cycles/{cycle_id}/kpis")
def cycle_kpis(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    return service.build_summary(db, cycle=cycle)["kpis"]


@router.get("/cycles/{cycle_id}/sensitivity")
def cycle_sensitivity(
    cycle_id: UUID = Path(...),
    yield_step: float | None = Query(default=None),
    price_step: float | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    return service.build_sensitivity(
        db, cycle=cycle, yield_step=yield_step, price_step=price_step
    )


# ------------------------------------------------------------------ labores


@router.get("/cycles/{cycle_id}/entries", response_model=list[FieldLogEntryResponse])
def list_entries(
    cycle_id: UUID = Path(...),
    category: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    service.assert_cycle_access(db, cycle_id, current_user)
    entries = repo.list_entries(
        db, cycle_id=cycle_id, category=category, date_from=date_from, date_to=date_to
    )
    return service.serialize_entries(db, entries)


@router.post("/cycles/{cycle_id}/entries", response_model=FieldLogEntryResponse, status_code=201)
def create_entry(
    cycle_id: UUID = Path(...),
    payload: FieldLogEntryCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    entry = service.create_entry(
        db, cycle=cycle, payload=payload, current_user=current_user
    )
    return service.serialize_entries(db, [entry])[0]


@router.patch(
    "/cycles/{cycle_id}/entries/{entry_id}", response_model=FieldLogEntryResponse
)
def update_entry(
    cycle_id: UUID = Path(...),
    entry_id: UUID = Path(...),
    payload: FieldLogEntryUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    entry = repo.get_entry(db, entry_id)
    if not entry or entry.cycle_id != cycle.id:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    entry = service.update_entry(db, cycle=cycle, entry=entry, payload=payload)
    return service.serialize_entries(db, [entry])[0]


@router.delete("/cycles/{cycle_id}/entries/{entry_id}", status_code=204)
def delete_entry(
    cycle_id: UUID = Path(...),
    entry_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    entry = repo.get_entry(db, entry_id)
    if not entry or entry.cycle_id != cycle.id:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    service.delete_entry(db, entry)


@router.post("/sync", response_model=SyncResponse)
def sync(
    payload: SyncRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Vuelca la cola de capturas hechas sin señal."""
    return service.sync_entries(db, payload=payload, current_user=current_user)


# ------------------------------------------------------------------ fotos


@router.post(
    "/cycles/{cycle_id}/photo-upload-url", response_model=PhotoUploadUrlResponse
)
def photo_upload_url(
    cycle_id: UUID = Path(...),
    payload: PhotoUploadUrlRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    try:
        return service.photo_upload_url(
            user_id=UUID(str(current_user["id"])),
            cycle_id=cycle.id,
            file_name=payload.file_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"Almacenamiento de fotos no disponible: {exc}",
        )


@router.post("/photo-urls")
def photo_read_urls(
    paths: list[str] = Body(..., embed=True),
    current_user: dict = Depends(get_current_user),
):
    """URLs de lectura firmadas para las rutas guardadas en los registros."""
    return {"urls": resolve_read_urls(paths)}


# ------------------------------------------------------------------ fenología


@router.get("/cycles/{cycle_id}/phenology", response_model=list[PhenologyResponse])
def list_phenology(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    service.assert_cycle_access(db, cycle_id, current_user)
    return repo.list_phenology(db, cycle_id)


@router.post("/cycles/{cycle_id}/phenology", response_model=PhenologyResponse)
def upsert_phenology(
    cycle_id: UUID = Path(...),
    payload: PhenologyCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    return service.upsert_phenology(
        db, cycle=cycle, payload=payload, current_user=current_user
    )


@router.delete("/cycles/{cycle_id}/phenology/{record_id}", status_code=204)
def delete_phenology(
    cycle_id: UUID = Path(...),
    record_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    records = repo.list_phenology(db, cycle.id)
    record = next((item for item in records if item.id == record_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Etapa no encontrada")
    service.delete_phenology(db, record)


# ------------------------------------------------------------------ importar / exportar


@router.get("/cycles/{cycle_id}/export.xlsx")
def export_cycle(
    cycle_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Descarga la bitácora en el formato de hoja de cálculo de siempre."""
    cycle = service.assert_cycle_access(db, cycle_id, current_user)
    summary = service.build_summary(db, cycle=cycle)
    entries = service.serialize_entries(db, repo.list_entries(db, cycle_id=cycle.id))
    sensitivity = service.build_sensitivity(db, cycle=cycle)

    content = build_workbook(summary, entries, sensitivity)
    file_name = default_file_name(cycle.name)

    from io import BytesIO

    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.post("/cycles/{cycle_id}/import-xlsx")
async def import_cycle(
    cycle_id: UUID = Path(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Carga el histórico de un ciclo desde su hoja de cálculo."""
    cycle = service.assert_cycle_access(db, cycle_id, current_user)

    content = await file.read()
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera los 10 MB")

    try:
        parsed = parse_workbook(content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {exc}")

    imported = 0
    for item in parsed["entries"]:
        payload = FieldLogEntryCreate(**item)
        service.create_entry(db, cycle=cycle, payload=payload, current_user=current_user)
        imported += 1

    stages = 0
    for item in parsed["phenology"]:
        service.upsert_phenology(
            db, cycle=cycle, payload=PhenologyCreate(**item), current_user=current_user
        )
        stages += 1

    return {
        "imported_entries": imported,
        "imported_stages": stages,
        "detected_cycle": parsed["cycle"],
        "warnings": parsed["warnings"],
    }
