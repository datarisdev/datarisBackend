"""Acceso a datos de la Bitácora de Campo.

Todo lo que sea SQL vive aquí; `service.py` no arma consultas. La regla que
más importa en este módulo: los agregados por categoría se resuelven en la base
de datos, no trayendo las filas a Python, porque un ciclo real acumula cientos
de labores y la pantalla principal las pide todas a la vez.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.parcel import Parcel
from app.models.user import User
from app.modules.field_log.kpi import EntryInputSnapshot, EntrySnapshot
from app.modules.field_log.models import (
    CropCycle,
    FieldLogEntry,
    FieldLogEntryInput,
    FieldLogLaborStandard,
    FieldLogTemplate,
    PhenologyRecord,
)


def get_cycle(db: Session, cycle_id: UUID) -> CropCycle | None:
    return db.query(CropCycle).filter(CropCycle.id == cycle_id).first()


def get_parcel(db: Session, parcel_id: UUID) -> Parcel | None:
    return db.query(Parcel).filter(Parcel.id == parcel_id).first()


def list_cycles(
    db: Session,
    *,
    user_id: UUID | str,
    parcel_id: UUID | None = None,
    status: str | None = None,
) -> list[CropCycle]:
    query = db.query(CropCycle).filter(CropCycle.user_id == user_id)
    if parcel_id:
        query = query.filter(CropCycle.parcel_id == parcel_id)
    if status:
        query = query.filter(CropCycle.status == status)
    return query.order_by(CropCycle.created_at.desc()).all()


def cycle_aggregates(db: Session, cycle_ids: list[UUID]) -> dict[UUID, dict]:
    """Conteo, costo total y fecha del último registro, por ciclo."""
    if not cycle_ids:
        return {}

    rows = db.execute(
        select(
            FieldLogEntry.cycle_id,
            func.count(FieldLogEntry.id),
            func.coalesce(func.sum(FieldLogEntry.cost_per_ha), 0.0),
            func.max(FieldLogEntry.performed_at),
        )
        .where(FieldLogEntry.cycle_id.in_(cycle_ids))
        .group_by(FieldLogEntry.cycle_id)
    ).all()

    return {
        row[0]: {
            "entry_count": row[1],
            "total_cost_per_ha": float(row[2] or 0.0),
            "last_entry_at": row[3],
        }
        for row in rows
    }


def parcel_names(db: Session, parcel_ids: list[UUID]) -> dict[UUID, str]:
    if not parcel_ids:
        return {}
    rows = db.execute(select(Parcel.id, Parcel.name).where(Parcel.id.in_(parcel_ids))).all()
    return {row[0]: row[1] for row in rows}


def list_entries(
    db: Session,
    *,
    cycle_id: UUID,
    category: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
) -> list[FieldLogEntry]:
    query = (
        db.query(FieldLogEntry)
        .options(selectinload(FieldLogEntry.inputs))
        .filter(FieldLogEntry.cycle_id == cycle_id)
    )
    if category:
        query = query.filter(FieldLogEntry.category == category)
    if date_from:
        query = query.filter(FieldLogEntry.performed_at >= date_from)
    if date_to:
        query = query.filter(FieldLogEntry.performed_at <= date_to)

    query = query.order_by(
        FieldLogEntry.performed_at.desc().nullslast(), FieldLogEntry.created_at.desc()
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def get_entry(db: Session, entry_id: UUID) -> FieldLogEntry | None:
    return (
        db.query(FieldLogEntry)
        .options(selectinload(FieldLogEntry.inputs))
        .filter(FieldLogEntry.id == entry_id)
        .first()
    )


def find_entry_by_client_uuid(
    db: Session, *, user_id: UUID | str, client_uuid: UUID
) -> FieldLogEntry | None:
    return (
        db.query(FieldLogEntry)
        .filter(FieldLogEntry.user_id == user_id, FieldLogEntry.client_uuid == client_uuid)
        .first()
    )


def entry_snapshots(db: Session, cycle_id: UUID) -> list[EntrySnapshot]:
    """Proyección mínima de las labores para el cálculo de indicadores."""
    entries = (
        db.query(FieldLogEntry)
        .options(selectinload(FieldLogEntry.inputs))
        .filter(FieldLogEntry.cycle_id == cycle_id)
        .all()
    )
    return [
        EntrySnapshot(
            category=entry.category,
            cost_per_ha=entry.cost_per_ha or 0.0,
            data=entry.data or {},
            inputs=tuple(
                EntryInputSnapshot(
                    ia_grams=item.ia_grams,
                    n_units=item.n_units,
                    p_units=item.p_units,
                    k_units=item.k_units,
                )
                for item in entry.inputs
            ),
        )
        for entry in entries
    ]


def verification_stats(db: Session, cycle_id: UUID) -> dict[str, int]:
    """Cuántos registros del ciclo llevan sello de ubicación verificada."""
    rows = db.execute(
        select(FieldLogEntry.location_verified, func.count(FieldLogEntry.id))
        .where(FieldLogEntry.cycle_id == cycle_id)
        .group_by(FieldLogEntry.location_verified)
    ).all()

    stats = {"verified": 0, "outside": 0, "unknown": 0}
    for verified, count in rows:
        if verified is True:
            stats["verified"] = count
        elif verified is False:
            stats["outside"] = count
        else:
            stats["unknown"] = count
    stats["total"] = sum(stats.values())
    return stats


def list_phenology(db: Session, cycle_id: UUID) -> list[PhenologyRecord]:
    return (
        db.query(PhenologyRecord)
        .filter(PhenologyRecord.cycle_id == cycle_id)
        .order_by(PhenologyRecord.observed_at.asc().nullslast())
        .all()
    )


def get_phenology_by_stage(db: Session, cycle_id: UUID, stage_code: str) -> PhenologyRecord | None:
    return (
        db.query(PhenologyRecord)
        .filter(PhenologyRecord.cycle_id == cycle_id, PhenologyRecord.stage_code == stage_code)
        .first()
    )


def delete_entry_inputs(db: Session, entry_id: UUID) -> None:
    db.query(FieldLogEntryInput).filter(FieldLogEntryInput.entry_id == entry_id).delete()


def list_user_templates(db: Session, user_id: UUID | str) -> list[FieldLogTemplate]:
    return (
        db.query(FieldLogTemplate)
        .filter(FieldLogTemplate.user_id == user_id)
        .order_by(FieldLogTemplate.name.asc())
        .all()
    )


def get_user_template(db: Session, user_id: UUID | str, key: str) -> FieldLogTemplate | None:
    return (
        db.query(FieldLogTemplate)
        .filter(FieldLogTemplate.user_id == user_id, FieldLogTemplate.key == key)
        .first()
    )


def list_labor_standards(db: Session, user_id: UUID | str) -> list[FieldLogLaborStandard]:
    return (
        db.query(FieldLogLaborStandard)
        .filter(FieldLogLaborStandard.user_id == user_id)
        .order_by(FieldLogLaborStandard.labor_name.asc())
        .all()
    )


def author_emails(db: Session, user_ids: list[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = db.execute(select(User.id, User.email).where(User.id.in_(user_ids))).all()
    return {row[0]: row[1] for row in rows}


def recent_descriptions(db: Session, *, user_id: UUID | str, category: str, limit: int = 12) -> list[dict]:
    """Últimas descripciones usadas por el usuario en una categoría.

    Alimenta el autocompletado del móvil: en campo se repiten las mismas
    labores con los mismos insumos, y volver a teclearlas es justo el trabajo
    que este módulo existe para eliminar.
    """
    rows = db.execute(
        select(
            FieldLogEntry.description,
            func.max(FieldLogEntry.unit),
            func.max(FieldLogEntry.unit_cost),
            func.max(FieldLogEntry.created_at),
        )
        .where(FieldLogEntry.user_id == user_id, FieldLogEntry.category == category)
        .group_by(FieldLogEntry.description)
        .order_by(func.max(FieldLogEntry.created_at).desc())
        .limit(limit)
    ).all()

    return [
        {"description": row[0], "unit": row[1], "unit_cost": row[2]}
        for row in rows
    ]
