"""Reglas de negocio de la Bitácora de Campo."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.field_log import repository as repo
from app.modules.field_log.geo import verify_location
from app.modules.field_log.kpi import compute_kpis
from app.modules.field_log.models import (
    CATEGORY_LABELS,
    CropCycle,
    FieldLogEntry,
    FieldLogEntryInput,
    FieldLogLaborStandard,
    PhenologyRecord,
)
from app.modules.field_log.sensitivity import build_matrix
from app.modules.field_log.templates import (
    DEFAULT_TEMPLATE_KEY,
    get_system_template,
    list_system_templates,
)
from app.services import compat_mirror

logger = logging.getLogger(__name__)

PHOTO_URL_TTL_MINUTES = 60


# ------------------------------------------------------------------ acceso


def _user_uuid(current_user: dict[str, Any]) -> UUID:
    try:
        return UUID(str(current_user["id"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Usuario no válido")


def _is_admin(current_user: dict[str, Any]) -> bool:
    return current_user.get("role") in {"admin", "superadmin"}


def assert_parcel_access(db: Session, parcel_id: UUID, current_user: dict[str, Any]):
    # Las parcelas del usuario viven en el almacén compat, no en la tabla, así
    # que buscarlas solo en `parcels` devolvía 404 para lotes que el usuario
    # está viendo en pantalla. `ensure_parcel` las refleja la primera vez.
    parcel = repo.get_parcel(db, parcel_id) or compat_mirror.ensure_parcel(db, parcel_id)
    if not parcel:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")
    if str(parcel.user_id) != str(current_user["id"]) and not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a esta parcela")
    return parcel


def assert_cycle_access(db: Session, cycle_id: UUID, current_user: dict[str, Any]) -> CropCycle:
    cycle = repo.get_cycle(db, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="Ciclo no encontrado")
    if str(cycle.user_id) != str(current_user["id"]) and not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a este ciclo")
    return cycle


# ------------------------------------------------------------------ ciclos


def create_cycle(
    db: Session, *, parcel_id: UUID, payload, current_user: dict[str, Any]
) -> CropCycle:
    parcel = assert_parcel_access(db, parcel_id, current_user)

    data = payload.model_dump()
    # Si no se indica superficie, se hereda la de la parcela: los costos de la
    # bitácora son siempre por hectárea y sin área no hay forma de totalizar.
    # Se relee la parcela del almacén compat porque el lote pudo redibujarse en
    # Mapeo después de reflejarse, y heredar la superficie vieja falsearía todo
    # el costeo del ciclo.
    if data.get("area_ha") is None:
        data["area_ha"] = compat_mirror.refresh_parcel(db, parcel).area
    if not data.get("template_key"):
        data["template_key"] = DEFAULT_TEMPLATE_KEY

    cycle = CropCycle(**data, parcel_id=parcel_id, user_id=_user_uuid(current_user))
    db.add(cycle)
    db.commit()
    db.refresh(cycle)
    return cycle


def update_cycle(db: Session, *, cycle: CropCycle, payload) -> CropCycle:
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(cycle, key, value)
    db.commit()
    db.refresh(cycle)
    return cycle


def delete_cycle(db: Session, cycle: CropCycle) -> None:
    db.delete(cycle)
    db.commit()


def serialize_cycles(db: Session, cycles: list[CropCycle]) -> list[dict[str, Any]]:
    """Añade a cada ciclo el resumen que necesita la lista, sin N+1."""
    cycle_ids = [cycle.id for cycle in cycles]
    aggregates = repo.cycle_aggregates(db, cycle_ids)
    parcel_ids = [cycle.parcel_id for cycle in cycles]
    names = repo.parcel_names(db, parcel_ids)
    # Un ciclo cuya parcela aún no está reflejada en la tabla aparecería sin
    # nombre; el almacén compat sí lo tiene.
    missing = [parcel_id for parcel_id in parcel_ids if parcel_id not in names]
    if missing:
        names.update(compat_mirror.compat_parcel_names(missing))

    serialized = []
    for cycle in cycles:
        extra = aggregates.get(cycle.id, {})
        item = {
            column.name: getattr(cycle, column.name) for column in cycle.__table__.columns
        }
        item["parcel_name"] = names.get(cycle.parcel_id)
        item["entry_count"] = extra.get("entry_count", 0)
        item["total_cost_per_ha"] = extra.get("total_cost_per_ha", 0.0)
        item["last_entry_at"] = extra.get("last_entry_at")
        serialized.append(item)
    return serialized


# ------------------------------------------------------------------ labores


def _resolve_cost(quantity: float | None, unit_cost: float | None, provided: float | None) -> float:
    """Costo por hectárea de una labor.

    Cantidad × costo unitario manda siempre que ambos existan; el valor que
    envíe el cliente solo se usa para labores de costo cerrado (una renta, un
    servicio contratado) donde no hay cantidad ni precio unitario.
    """
    if quantity is not None and unit_cost is not None:
        return float(quantity) * float(unit_cost)
    if provided is not None:
        return float(provided)
    return 0.0


def _geometry_for(cycle: CropCycle, parcel) -> Any:
    """Geometría contra la que verificar: la sección si existe, si no la parcela."""
    if cycle.section_geometry:
        return cycle.section_geometry
    return parcel.geometry if parcel else None


def _apply_inputs(db: Session, entry: FieldLogEntry, inputs: list[Any]) -> None:
    repo.delete_entry_inputs(db, entry.id)
    for item in inputs:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        # Si no viene el total de i.a. pero sí dosis y concentración, se deriva:
        # es la cuenta que el técnico hace a mano y la que alimenta los KPIs.
        if data.get("ia_grams") is None and data.get("dose") and data.get("ia_concentration"):
            data["ia_grams"] = float(data["dose"]) * float(data["ia_concentration"])
        db.add(FieldLogEntryInput(entry_id=entry.id, **data))


def create_entry(
    db: Session,
    *,
    cycle: CropCycle,
    payload,
    current_user: dict[str, Any],
    parcel=None,
) -> FieldLogEntry:
    user_id = _user_uuid(current_user)

    if payload.client_uuid:
        existing = repo.find_entry_by_client_uuid(
            db, user_id=user_id, client_uuid=payload.client_uuid
        )
        if existing:
            return existing

    parcel = parcel or compat_mirror.ensure_parcel(db, cycle.parcel_id)
    location = payload.location.model_dump() if payload.location else None

    entry = FieldLogEntry(
        cycle_id=cycle.id,
        parcel_id=cycle.parcel_id,
        user_id=user_id,
        category=payload.category,
        description=payload.description,
        unit=payload.unit,
        quantity=payload.quantity,
        unit_cost=payload.unit_cost,
        cost_per_ha=_resolve_cost(payload.quantity, payload.unit_cost, payload.cost_per_ha),
        performed_at=payload.performed_at,
        observations=payload.observations,
        data=payload.data or {},
        location=location,
        location_verified=verify_location(location, _geometry_for(cycle, parcel)),
        photos=payload.photos,
        client_uuid=payload.client_uuid,
        source=payload.source,
    )
    db.add(entry)

    try:
        db.flush()
    except IntegrityError:
        # Dos envíos simultáneos de la misma captura offline: gana el primero.
        db.rollback()
        if payload.client_uuid:
            existing = repo.find_entry_by_client_uuid(
                db, user_id=user_id, client_uuid=payload.client_uuid
            )
            if existing:
                return existing
        raise

    if payload.inputs:
        _apply_inputs(db, entry, payload.inputs)

    db.commit()
    db.refresh(entry)
    return entry


def update_entry(db: Session, *, cycle: CropCycle, entry: FieldLogEntry, payload) -> FieldLogEntry:
    data = payload.model_dump(exclude_unset=True)
    inputs = data.pop("inputs", None)
    location = data.pop("location", None)

    for key, value in data.items():
        setattr(entry, key, value)

    if location is not None:
        entry.location = location
        parcel = compat_mirror.ensure_parcel(db, cycle.parcel_id)
        entry.location_verified = verify_location(location, _geometry_for(cycle, parcel))

    if "quantity" in data or "unit_cost" in data or "cost_per_ha" in data:
        entry.cost_per_ha = _resolve_cost(
            entry.quantity, entry.unit_cost, data.get("cost_per_ha", entry.cost_per_ha)
        )

    if inputs is not None:
        _apply_inputs(db, entry, payload.inputs or [])

    db.commit()
    db.refresh(entry)
    return entry


def delete_entry(db: Session, entry: FieldLogEntry) -> None:
    db.delete(entry)
    db.commit()


def serialize_entries(db: Session, entries: list[FieldLogEntry]) -> list[dict[str, Any]]:
    emails = repo.author_emails(db, list({entry.user_id for entry in entries}))
    serialized = []
    for entry in entries:
        item = {column.name: getattr(entry, column.name) for column in entry.__table__.columns}
        item["category_label"] = CATEGORY_LABELS.get(entry.category, entry.category)
        item["author_email"] = emails.get(entry.user_id)
        item["inputs"] = [
            {column.name: getattr(inp, column.name) for column in inp.__table__.columns}
            for inp in entry.inputs
        ]
        serialized.append(item)
    return serialized


# ------------------------------------------------------------------ sincronización


def sync_entries(db: Session, *, payload, current_user: dict[str, Any]) -> dict[str, Any]:
    """Vuelca la cola offline del móvil.

    Cada elemento se procesa por separado: un registro con datos corruptos no
    puede bloquear los otros veinte que el técnico capturó en el lote, porque
    entonces el usuario pierde el día de trabajo entero.
    """
    results: list[dict[str, Any]] = []
    created = duplicates = errors = 0
    cycles: dict[UUID, CropCycle] = {}

    for item in payload.entries:
        try:
            cycle = cycles.get(item.cycle_id)
            if cycle is None:
                cycle = assert_cycle_access(db, item.cycle_id, current_user)
                cycles[item.cycle_id] = cycle

            already = None
            if item.client_uuid:
                already = repo.find_entry_by_client_uuid(
                    db, user_id=_user_uuid(current_user), client_uuid=item.client_uuid
                )

            entry = create_entry(db, cycle=cycle, payload=item, current_user=current_user)

            if already is not None:
                duplicates += 1
                results.append(
                    {"client_uuid": item.client_uuid, "status": "duplicate", "entry_id": entry.id}
                )
            else:
                created += 1
                results.append(
                    {"client_uuid": item.client_uuid, "status": "created", "entry_id": entry.id}
                )
        except HTTPException as exc:
            db.rollback()
            errors += 1
            results.append(
                {"client_uuid": item.client_uuid, "status": "error", "detail": str(exc.detail)}
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors += 1
            logger.exception("field_log: fallo sincronizando registro offline")
            results.append(
                {"client_uuid": item.client_uuid, "status": "error", "detail": str(exc)}
            )

    return {"created": created, "duplicates": duplicates, "errors": errors, "results": results}


# ------------------------------------------------------------------ fenología


def upsert_phenology(
    db: Session, *, cycle: CropCycle, payload, current_user: dict[str, Any]
) -> PhenologyRecord:
    """Crea o actualiza la etapa. Una etapa se observa una vez por ciclo."""
    record = repo.get_phenology_by_stage(db, cycle.id, payload.stage_code)
    location = payload.location.model_dump() if payload.location else None
    parcel = compat_mirror.ensure_parcel(db, cycle.parcel_id)
    verified = verify_location(location, _geometry_for(cycle, parcel)) if location else None

    if record is None:
        record = PhenologyRecord(
            cycle_id=cycle.id,
            user_id=_user_uuid(current_user),
            stage_code=payload.stage_code,
            client_uuid=payload.client_uuid,
        )
        db.add(record)

    record.stage_label = payload.stage_label or record.stage_label
    record.observed_at = payload.observed_at
    record.observations = payload.observations
    record.photos = payload.photos
    if location is not None:
        record.location = location
        record.location_verified = verified

    db.commit()
    db.refresh(record)
    return record


def delete_phenology(db: Session, record: PhenologyRecord) -> None:
    db.delete(record)
    db.commit()


# ------------------------------------------------------------------ resumen


def _timeline(entries: list[FieldLogEntry], phenology: list[PhenologyRecord]) -> list[dict[str, Any]]:
    """Hitos del ciclo ordenados por fecha, para cruzarlos con la serie NDVI."""
    events: list[dict[str, Any]] = []

    for entry in entries:
        if not entry.performed_at:
            continue
        events.append(
            {
                "type": "entry",
                "date": entry.performed_at.isoformat(),
                "category": entry.category,
                "label": entry.description,
                "cost_per_ha": entry.cost_per_ha,
                "id": str(entry.id),
            }
        )

    for record in phenology:
        if not record.observed_at:
            continue
        events.append(
            {
                "type": "phenology",
                "date": record.observed_at.isoformat(),
                "category": "fenologia",
                "label": record.stage_label or record.stage_code,
                "id": str(record.id),
            }
        )

    return sorted(events, key=lambda event: event["date"])


def _effective_yield(cycle: CropCycle) -> float | None:
    """Rendimiento con el que se calculan los indicadores.

    El real manda; si el ciclo aún no se cosecha se usa el esperado, y así la
    bitácora es útil durante el ciclo y no solo al cerrarlo.
    """
    return cycle.actual_yield_ton_ha or cycle.expected_yield_ton_ha


def build_summary(db: Session, *, cycle: CropCycle) -> dict[str, Any]:
    snapshots = repo.entry_snapshots(db, cycle.id)
    kpis = compute_kpis(
        snapshots,
        yield_ton_ha=_effective_yield(cycle),
        price_per_ton=cycle.target_price_per_ton,
        budget_per_ha=cycle.budget_per_ha,
    )
    kpis["yield_is_estimated"] = cycle.actual_yield_ton_ha is None

    entries = repo.list_entries(db, cycle_id=cycle.id)
    phenology = repo.list_phenology(db, cycle.id)

    return {
        "cycle": serialize_cycles(db, [cycle])[0],
        "kpis": kpis,
        "template": resolve_template(db, cycle),
        "phenology": phenology,
        "recent_entries": serialize_entries(db, entries[:8]),
        "timeline": _timeline(entries, phenology),
        "verification": repo.verification_stats(db, cycle.id),
    }


def build_sensitivity(
    db: Session,
    *,
    cycle: CropCycle,
    yield_step: float | None = None,
    price_step: float | None = None,
) -> dict[str, Any]:
    snapshots = repo.entry_snapshots(db, cycle.id)
    kpis = compute_kpis(
        snapshots,
        yield_ton_ha=_effective_yield(cycle),
        price_per_ton=cycle.target_price_per_ton,
    )
    return build_matrix(
        investment_per_ha=kpis["economics"]["investment_per_ha"],
        yield_ton_ha=_effective_yield(cycle),
        price_per_ton=cycle.target_price_per_ton,
        yield_step=yield_step,
        price_step=price_step,
    )


# ------------------------------------------------------------------ plantillas


def resolve_template(db: Session, cycle: CropCycle) -> dict[str, Any]:
    """Plantilla del ciclo: primero las del usuario, luego las del sistema."""
    if cycle.template_key:
        custom = repo.get_user_template(db, cycle.user_id, cycle.template_key)
        if custom:
            definition = dict(custom.definition or {})
            definition.setdefault("categories", [])
            definition.setdefault("phenology_stages", [])
            definition.setdefault("labor_standards", [])
            definition.setdefault("cycle_attributes", [])
            definition.update(
                {
                    "key": custom.key,
                    "name": custom.name,
                    "description": custom.description,
                    "crop_type": custom.crop_type,
                    "is_system": False,
                }
            )
            return definition
    return get_system_template(cycle.template_key)


def available_templates(db: Session, user_id: UUID | str) -> list[dict[str, Any]]:
    templates = list_system_templates()
    for custom in repo.list_user_templates(db, user_id):
        definition = dict(custom.definition or {})
        definition.update(
            {
                "key": custom.key,
                "name": custom.name,
                "description": custom.description,
                "crop_type": custom.crop_type,
                "is_system": False,
            }
        )
        definition.setdefault("categories", [])
        definition.setdefault("phenology_stages", [])
        definition.setdefault("labor_standards", [])
        definition.setdefault("cycle_attributes", [])
        templates.append(definition)
    return templates


def labor_standards(db: Session, user_id: UUID | str) -> list[dict[str, Any]]:
    from app.modules.field_log.templates import DEFAULT_LABOR_STANDARDS

    standards = [
        {
            "id": uuid.uuid5(uuid.NAMESPACE_OID, f"labor-standard:{item['labor_name']}"),
            "user_id": None,
            "is_system": True,
            **item,
        }
        for item in DEFAULT_LABOR_STANDARDS
    ]
    for row in repo.list_labor_standards(db, user_id):
        standards.append(
            {
                "id": row.id,
                "user_id": row.user_id,
                "is_system": False,
                "labor_name": row.labor_name,
                "category": row.category,
                "hours_per_ha": row.hours_per_ha,
                "fuel_l_per_ha": row.fuel_l_per_ha,
            }
        )
    return standards


def create_labor_standard(db: Session, *, user_id: UUID, payload) -> FieldLogLaborStandard:
    row = FieldLogLaborStandard(user_id=user_id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------------ fotos


def photo_upload_url(*, user_id: UUID, cycle_id: UUID, file_name: str) -> dict[str, Any]:
    """URL de escritura temporal en Blob Storage para la evidencia fotográfica.

    Se reutiliza el contenedor y las credenciales que ya usa el módulo de
    entrenamiento; la ruta aísla por usuario igual que el resto de la
    plataforma.
    """
    from app.modules.field_log.storage import build_photo_path, generate_urls

    blob_path = build_photo_path(user_id=user_id, cycle_id=cycle_id, file_name=file_name)
    upload_url, read_url = generate_urls(blob_path)
    return {
        "upload_url": upload_url,
        "read_url": read_url,
        "blob_path": blob_path,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=PHOTO_URL_TTL_MINUTES),
    }
