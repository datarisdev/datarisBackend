"""Reflejo bajo demanda del almacén COMPAT en las tablas SQLAlchemy.

Producción sirve la web desde el sistema COMPAT: las parcelas que el usuario ve
en Mapeo viven dentro de un JSON (`dataris_compat_state.payload`), no en la
tabla `parcels`. Los módulos que sí son SQLAlchemy puro —la Bitácora de Campo
es el primero— declaran claves foráneas reales contra `parcels` y `users`, de
modo que para ellos una parcela real sencillamente no existe: la bitácora
respondía *«Parcela no encontrada»* ante cualquier lote del usuario y, aun sin
esa comprobación, el INSERT del ciclo habría fallado con ForeignKeyViolation.

Aquí se copia esa parcela a la tabla la primera vez que un módulo la necesita.
El reflejo es de una sola dirección —COMPAT sigue siendo la fuente de verdad—
y solo lleva lo que hace falta para satisfacer la clave foránea y para que la
bitácora pueda verificar el GPS contra el polígono del lote.

Es el mismo patrón que ya usaba `app/api/deps.py` para los usuarios compat, que
ahora vive aquí para no tenerlo duplicado en dos sitios.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.parcel import Parcel
from app.models.user import User

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ lectura


def _compat_state() -> dict[str, Any]:
    """Estado COMPAT completo, o vacío si el almacén no está disponible.

    La importación es perezosa a propósito: `compat.py` arrastra medio backend
    y este módulo lo usan dependencias que se cargan mucho antes.
    """
    from app.api.routers.compat import read_db

    try:
        return read_db() or {}
    except Exception:  # pragma: no cover - el almacén no debe tumbar la API
        logger.warning("No se pudo leer el estado compat", exc_info=True)
        return {}


def _compat_rows(name: str) -> list[dict[str, Any]]:
    tables = _compat_state().get("tables") or {}
    rows = tables.get(name) or []
    return rows if isinstance(rows, list) else []


def _same_id(value: Any, expected: Any) -> bool:
    return str(value or "").strip().lower() == str(expected or "").strip().lower()


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def find_compat_parcel(parcel_id: UUID | str) -> dict[str, Any] | None:
    return next(
        (row for row in _compat_rows("parcels") if _same_id(row.get("id"), parcel_id)),
        None,
    )


def find_compat_user(user_id: UUID | str) -> dict[str, Any] | None:
    users = _compat_state().get("users") or []
    if not isinstance(users, list):
        return None
    return next((row for row in users if _same_id(row.get("id"), user_id)), None)


# ------------------------------------------------------------------ reflejo


def ensure_mirrored_user(db: Session, compat_user: dict[str, Any]) -> bool:
    """Refleja una fila mínima de `users` para un usuario compat."""
    user_uuid = _as_uuid(compat_user.get("id"))
    if user_uuid is None:
        return False
    if db.query(User).filter(User.id == user_uuid).first():
        return True

    db.add(
        User(
            id=user_uuid,
            email=compat_user.get("email") or f"{user_uuid}@compat.dataris.local",
            password_hash=compat_user.get("password_hash") or "",
            is_active=True,
        )
    )
    try:
        db.commit()
        return True
    except SQLAlchemyError:
        # Otra petición en paralelo pudo insertarlo primero; si está, sirve igual.
        db.rollback()
        return db.query(User).filter(User.id == user_uuid).first() is not None


def ensure_mirrored_user_by_id(db: Session, user_id: UUID | str) -> bool:
    user_uuid = _as_uuid(user_id)
    if user_uuid is None:
        return False
    if db.query(User).filter(User.id == user_uuid).first():
        return True

    compat_user = find_compat_user(user_uuid)
    if not compat_user:
        return False
    return ensure_mirrored_user(db, compat_user)


def _parcel_from_compat(row: dict[str, Any], owner_id: UUID) -> Parcel:
    # La geometría normalizada (`geometry_geojson`) es la que la bitácora
    # compara contra el GPS del registro; sin ella el sello de ubicación queda
    # en "sin verificar", que es peor que un 500 pero tampoco es lo pedido.
    geometry = row.get("geometry_geojson") or row.get("geometry") or {}
    return Parcel(
        id=_as_uuid(row.get("id")) or uuid.uuid4(),
        name=_text(row.get("name")) or "Parcela sin nombre",
        area=_as_float(row.get("area")),
        finca=_text(row.get("finca")),
        lote=_text(row.get("lote")),
        codigo=_text(row.get("codigo")),
        external_id=_text(row.get("external_id")),
        geometry=geometry,
        file_url=_text(row.get("file_url")),
        user_id=owner_id,
    )


def ensure_parcel(db: Session, parcel_id: UUID | str) -> Parcel | None:
    """Devuelve la parcela de la tabla `parcels`, reflejándola si solo está en COMPAT."""
    parcel_uuid = _as_uuid(parcel_id)
    if parcel_uuid is None:
        return None

    existing = db.query(Parcel).filter(Parcel.id == parcel_uuid).first()
    if existing:
        return existing

    row = find_compat_parcel(parcel_uuid)
    if not row:
        return None

    owner_id = _as_uuid(row.get("user_id"))
    if owner_id is None or not ensure_mirrored_user_by_id(db, owner_id):
        logger.warning(
            "Parcela compat %s sin usuario reflejable (user_id=%s)", parcel_uuid, row.get("user_id")
        )
        return None

    db.add(_parcel_from_compat(row, owner_id))
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return db.query(Parcel).filter(Parcel.id == parcel_uuid).first()

    logger.info("Parcela %s reflejada desde el almacén compat", parcel_uuid)
    return db.query(Parcel).filter(Parcel.id == parcel_uuid).first()


def refresh_parcel(db: Session, parcel: Parcel) -> Parcel:
    """Vuelve a traer de COMPAT los campos que el usuario puede haber cambiado.

    Un lote se renombra o se redibuja en Mapeo, y ahí el cambio va al almacén
    COMPAT: el reflejo se quedaría con el nombre y la superficie del día en que
    se creó.
    """
    row = find_compat_parcel(parcel.id)
    if not row:
        return parcel

    changes: dict[str, Any] = {}
    name = _text(row.get("name"))
    if name and name != parcel.name:
        changes["name"] = name
    area = _as_float(row.get("area"))
    if area is not None and area != parcel.area:
        changes["area"] = area
    geometry = row.get("geometry_geojson") or row.get("geometry")
    if geometry and geometry != parcel.geometry:
        changes["geometry"] = geometry

    if not changes:
        return parcel

    for key, value in changes.items():
        setattr(parcel, key, value)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
    return parcel


def compat_parcel_names(parcel_ids: list[UUID]) -> dict[UUID, str]:
    """Nombres de las parcelas que aún no están reflejadas en la tabla."""
    if not parcel_ids:
        return {}

    wanted = {str(parcel_id).lower(): parcel_id for parcel_id in parcel_ids}
    names: dict[UUID, str] = {}
    for row in _compat_rows("parcels"):
        key = str(row.get("id") or "").strip().lower()
        target = wanted.get(key)
        if target is not None and row.get("name"):
            names[target] = str(row["name"])
    return names
