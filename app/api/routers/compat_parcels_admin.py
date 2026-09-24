"""Gestión de lotes por parte del equipo de Dataris.

El cliente ya no carga sus lotes desde el perfil: los da de alta el equipo de
desarrollo/comercial desde el panel de administración.

Los lotes son de la EMPRESA: se cargan, listan y borran por `company_id` y los
ve todo su equipo. Se guardan a nombre del titular de la empresa
(`company_parcel_owner`), que es la cuenta con la que se sincronizan en Graniot.
Las rutas por `user_id` se conservan para consultar y migrar los lotes que se
cargaron por usuario antes del cambio (filas sin `company_id`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, Header, HTTPException, UploadFile

from app.api.routers.compat import (
    COMPANY_PARCEL_OWNER_FIELD,
    LOCK,
    _company_for_user,
    _parcel_bbox,
    _parcel_geometry_shape,
    bearer_user,
    company_parcel_owner,
    company_parcels,
    create_manual_parcel_for_user,
    dedupe_user_parcels,
    now,
    panel_email_allowed,
    parcel_lot_key,
    parcel_manager_covers_user,
    parcel_manager_permission,
    read_db,
    schedule_graniot_parcel_delete,
    schedule_graniot_parcel_sync,
    store_parcel_file_for_user,
    table,
    write_db,
)

router = APIRouter(prefix="/compat/admin/parcels", tags=["Admin Parcels"])


def _require_manager(authorization: Optional[str]) -> Dict[str, Any]:
    """Usuario autenticado con permiso para administrar lotes ajenos."""
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    db = read_db()
    permission = parcel_manager_permission(db, str(user.get("id") or ""))
    if not permission.get("allowed") or not panel_email_allowed(user):
        raise HTTPException(status_code=403, detail="No tienes permiso para administrar lotes de usuarios")
    return {"user": user, "permission": permission, "db": db}


def _target_user(db: Dict[str, Any], permission: Dict[str, Any], user_id: Any) -> Dict[str, Any]:
    """Usuario dueño de los lotes, validando que entre en el alcance del gestor."""
    clean_id = str(user_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Selecciona el usuario dueño de los lotes")
    owner = next((u for u in db.get("users", []) if str(u.get("id")) == clean_id), None)
    if not owner:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not parcel_manager_covers_user(db, permission, clean_id):
        raise HTTPException(status_code=403, detail="Ese usuario no pertenece a las empresas que administras")
    return owner


def _profile_for(db: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return next(
        (
            row
            for row in table(db, "profiles")
            if str(row.get("user_id") or "") == user_id or str(row.get("id") or "") == user_id
        ),
        {},
    )


def _company_name(db: Dict[str, Any], company_id: Optional[str]) -> Optional[str]:
    if not company_id:
        return None
    company = next((c for c in table(db, "companies") if str(c.get("id")) == str(company_id)), None)
    return company.get("name") if company else None


def _user_summary(db: Dict[str, Any], user: Dict[str, Any], parcels_by_user: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    user_id = str(user.get("id"))
    profile = _profile_for(db, user_id)
    admin_row = next(
        (
            row
            for row in table(db, "admin_users")
            if str(row.get("user_id") or "") == user_id and row.get("is_active", True) is not False
        ),
        None,
    )
    company_id = _company_for_user(db, user_id)
    parcels = parcels_by_user.get(user_id, [])
    first_name = profile.get("first_name") or (user.get("user_metadata") or {}).get("first_name")
    last_name = profile.get("last_name") or (user.get("user_metadata") or {}).get("last_name")
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    return {
        "id": user_id,
        "email": user.get("email"),
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name or None,
        "is_active": user.get("is_active", True),
        "company_id": company_id,
        "company_name": _company_name(db, company_id) or profile.get("company_name"),
        "admin_role": (admin_row or {}).get("admin_role"),
        "parcel_count": len(parcels),
        "total_area": round(sum(float(p.get("area") or 0) for p in parcels), 2),
        "last_parcel_at": max(
            (str(p.get("created_at") or "") for p in parcels),
            default=None,
        )
        or None,
    }


def _parcels_by_user(db: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Lotes personales (sin empresa) agrupados por usuario: los que falta migrar."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in dedupe_user_parcels(table(db, "parcels")):
        if row.get("company_id"):
            continue
        grouped.setdefault(str(row.get("user_id") or ""), []).append(row)
    return grouped


def _sorted_user_parcels(db: Dict[str, Any], user_id: str) -> List[Dict[str, Any]]:
    rows = [
        row
        for row in table(db, "parcels")
        if not row.get("company_id") and str(row.get("user_id") or "") == user_id
    ]
    rows = dedupe_user_parcels(rows)
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)


def _sorted(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(dedupe_user_parcels(rows), key=lambda r: str(r.get("created_at") or ""), reverse=True)


def _covers_company(db: Dict[str, Any], permission: Dict[str, Any], company_id: str) -> bool:
    if permission.get("scope") == "all":
        return True
    return bool(permission.get("company_id")) and str(permission.get("company_id")) == str(company_id)


def _target_company(db: Dict[str, Any], permission: Dict[str, Any], company_id: Any) -> Dict[str, Any]:
    """Empresa dueña de los lotes, validando que entre en el alcance del gestor."""
    clean_id = str(company_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Selecciona la empresa dueña de los lotes")
    company = next((c for c in table(db, "companies") if str(c.get("id")) == clean_id), None)
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    if not _covers_company(db, permission, clean_id):
        raise HTTPException(status_code=403, detail="No administras los lotes de esa empresa")
    return company


def _company_owner(db: Dict[str, Any], company: Dict[str, Any]) -> Dict[str, Any]:
    owner = company_parcel_owner(db, str(company.get("id")))
    if not owner:
        raise HTTPException(
            status_code=409,
            detail="La empresa no tiene ningún usuario activo. Da de alta a su administrador antes de cargar lotes.",
        )
    return owner


def _company_summary(db: Dict[str, Any], company: Dict[str, Any], legacy_by_user: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    company_id = str(company.get("id"))
    parcels = dedupe_user_parcels(company_parcels(db, company_id))
    member_ids = [
        str(u.get("id"))
        for u in db.get("users", [])
        if u.get("id") and str(_company_for_user(db, str(u.get("id"))) or "") == company_id
    ]
    legacy = [row for uid in member_ids for row in legacy_by_user.get(uid, [])]
    owner = company_parcel_owner(db, company_id)
    return {
        "id": company_id,
        "name": company.get("name"),
        "is_active": company.get("is_active", True),
        "max_hectares": company.get("max_hectares"),
        "member_count": len(member_ids),
        "parcel_count": len(parcels),
        "total_area": round(sum(float(p.get("area") or 0) for p in parcels), 2),
        "last_parcel_at": max((str(p.get("created_at") or "") for p in parcels), default=None) or None,
        # Lotes que sus usuarios cargaron por separado antes del cambio.
        "legacy_parcel_count": len(legacy),
        "legacy_user_count": len({str(r.get("user_id")) for r in legacy}),
        "owner": {"id": owner.get("id"), "email": owner.get("email")} if owner else None,
        "owner_pinned": bool(company.get(COMPANY_PARCEL_OWNER_FIELD)),
    }


@router.get("/context")
def parcels_admin_context(authorization: Optional[str] = Header(default=None)):
    """Permiso del solicitante, para que el frontend sepa qué ofrecer."""
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    db = read_db()
    permission = parcel_manager_permission(db, str(user.get("id") or ""))
    return {
        "data": {
            "allowed": bool(permission.get("allowed")) and panel_email_allowed(user),
            "scope": permission.get("scope"),
            "admin_role": permission.get("admin_role"),
            "company_id": permission.get("company_id"),
            "company_name": _company_name(db, permission.get("company_id")),
        },
        "error": None,
    }


@router.get("/users")
def list_manageable_users(authorization: Optional[str] = Header(default=None)):
    ctx = _require_manager(authorization)
    db = ctx["db"]
    permission = ctx["permission"]
    grouped = _parcels_by_user(db)

    users: List[Dict[str, Any]] = []
    for user in db.get("users", []):
        user_id = str(user.get("id") or "")
        if not user_id:
            continue
        if not parcel_manager_covers_user(db, permission, user_id):
            continue
        users.append(_user_summary(db, user, grouped))

    users.sort(key=lambda u: ((u.get("company_name") or "~").lower(), (u.get("email") or "").lower()))
    return {"data": {"users": users, "scope": permission.get("scope")}, "error": None}


@router.get("/companies")
def list_manageable_companies(authorization: Optional[str] = Header(default=None)):
    ctx = _require_manager(authorization)
    db = ctx["db"]
    permission = ctx["permission"]
    legacy = _parcels_by_user(db)
    companies = [
        _company_summary(db, company, legacy)
        for company in table(db, "companies")
        if company.get("id") and _covers_company(db, permission, str(company.get("id")))
    ]
    companies.sort(key=lambda c: str(c.get("name") or "~").lower())
    return {"data": {"companies": companies, "scope": permission.get("scope")}, "error": None}


@router.get("/company")
def list_company_parcels(company_id: str, authorization: Optional[str] = Header(default=None)):
    ctx = _require_manager(authorization)
    db = ctx["db"]
    company = _target_company(db, ctx["permission"], company_id)
    legacy = _parcels_by_user(db)
    members = [
        _user_summary(db, user, legacy)
        for user in db.get("users", [])
        if user.get("id") and str(_company_for_user(db, str(user.get("id"))) or "") == str(company.get("id"))
    ]
    members.sort(key=lambda u: (u.get("email") or "").lower())
    return {
        "data": {
            "company": _company_summary(db, company, legacy),
            "parcels": _sorted(company_parcels(db, str(company.get("id")))),
            "members": members,
        },
        "error": None,
    }


@router.get("/list")
def list_user_parcels(user_id: str, authorization: Optional[str] = Header(default=None)):
    """Lotes personales de un usuario que aún no se han pasado a su empresa."""
    ctx = _require_manager(authorization)
    db = ctx["db"]
    owner = _target_user(db, ctx["permission"], user_id)
    owner_id = str(owner.get("id"))
    return {
        "data": {
            "user": _user_summary(db, owner, _parcels_by_user(db)),
            "parcels": _sorted_user_parcels(db, owner_id),
        },
        "error": None,
    }


def _resolve_destination(ctx: Dict[str, Any], company_id: Any, user_id: Any) -> Dict[str, Any]:
    """Empresa y titular a cuyo nombre se guarda un lote nuevo.

    Siempre se carga para una empresa. Por compatibilidad, si solo llega
    `user_id` el lote va a la empresa de ese usuario.
    """
    db = ctx["db"]
    if not str(company_id or "").strip() and str(user_id or "").strip():
        member = _target_user(db, ctx["permission"], user_id)
        company_id = _company_for_user(db, str(member.get("id")))
        if not company_id:
            raise HTTPException(status_code=400, detail="Ese usuario no pertenece a ninguna empresa")
    company = _target_company(db, ctx["permission"], company_id)
    return {"company": company, "owner": _company_owner(db, company)}


@router.post("/upload")
async def upload_parcel_for_company(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(...),
    company_id: Optional[str] = Form(default=None),
    user_id: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
):
    ctx = _require_manager(authorization)
    dest = _resolve_destination(ctx, company_id, user_id)
    owner = dest["owner"]
    created_rows = await store_parcel_file_for_user(
        owner,
        file,
        name,
        company_id=str(dest["company"].get("id")),
        uploaded_by=str(ctx["user"].get("id") or ""),
    )
    # El titular puede no tener todavía cuenta en Graniot (cliente recién dado
    # de alta): se le crea su portal antes de subir, para que los lotes acaben
    # en la cuenta de la empresa y no se queden solo en Dataris.
    schedule_graniot_parcel_sync(background_tasks, owner, created_rows, ensure_account=True)
    return {"data": {"parcel": created_rows[0], "parcels": created_rows}, "error": None}


@router.post("/manual")
def create_manual_parcel_for_company(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    ctx = _require_manager(authorization)
    dest = _resolve_destination(ctx, payload.get("company_id"), payload.get("user_id"))
    owner = dest["owner"]
    row = create_manual_parcel_for_user(
        owner,
        payload.get("name"),
        payload.get("geometry"),
        company_id=str(dest["company"].get("id")),
        uploaded_by=str(ctx["user"].get("id") or ""),
    )
    schedule_graniot_parcel_sync(background_tasks, owner, [row], ensure_account=True)
    return {"data": {"parcel": row}, "error": None}


@router.post("/delete")
def delete_parcels_for_user(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Borra lotes de una empresa (`company_id`) o, para los aún sin migrar, de un usuario (`user_id`)."""
    ctx = _require_manager(authorization)
    company_id = str(payload.get("company_id") or "").strip()
    if company_id:
        company = _target_company(ctx["db"], ctx["permission"], company_id)
        company_id = str(company.get("id"))

        def belongs(row: Dict[str, Any]) -> bool:
            return str(row.get("company_id") or "") == company_id

        not_found = "No se encontraron lotes de esa empresa con los ids indicados"
    else:
        owner = _target_user(ctx["db"], ctx["permission"], payload.get("user_id"))
        owner_id = str(owner.get("id"))

        def belongs(row: Dict[str, Any]) -> bool:
            return not row.get("company_id") and str(row.get("user_id") or "") == owner_id

        not_found = "No se encontraron lotes de ese usuario con los ids indicados"

    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list):
        raw_ids = [payload.get("id")] if payload.get("id") else []
    ids = {str(value) for value in raw_ids if value}
    if not ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos un lote")

    with LOCK:
        db = read_db()
        rows = table(db, "parcels")
        removed = [dict(row) for row in rows if str(row.get("id")) in ids and belongs(row)]
        if removed:
            removed_ids = {str(row.get("id")) for row in removed}
            db["tables"]["parcels"] = [row for row in rows if not (str(row.get("id")) in removed_ids and belongs(row))]
            write_db(db)
        users_by_id = {str(u.get("id")): u for u in db.get("users", [])}

    if not removed:
        raise HTTPException(status_code=404, detail=not_found)

    # El lote desaparece de Dataris: quítalo también de la cuenta de Graniot en
    # la que vive (la de la persona a cuyo nombre está) para que ambos lados
    # queden igual.
    by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for row in removed:
        by_owner.setdefault(str(row.get("user_id") or ""), []).append(row)
    for owner_id, owned in by_owner.items():
        owner_user = users_by_id.get(owner_id)
        if owner_user:
            schedule_graniot_parcel_delete(background_tasks, owner_user, owned)
    return {"data": {"deleted": [row.get("id") for row in removed], "count": len(removed)}, "error": None}


# --- Migración de los lotes cargados por usuario ------------------------------

# Campos por los que otras tablas apuntan a un lote.
_PARCEL_REF_FIELDS = ("parcel_id", "local_parcel_id", "lote_id")


def _in_graniot(row: Dict[str, Any]) -> bool:
    return bool(row.get("graniot_parcel_id") or row.get("graniot_parcels") or row.get("graniot_synced_at"))


def _same_lot(a: Dict[str, Any], b: Dict[str, Any], shapes: Dict[str, Any]) -> bool:
    """El mismo lote cargado por dos personas: geometría casi idéntica (IoU >=
    0,95) o, si alguno no tiene geometría, el mismo nombre normalizado."""
    shape_a, shape_b = shapes.get(str(a.get("id"))), shapes.get(str(b.get("id")))
    if shape_a is not None and shape_b is not None:
        box_a, box_b = _parcel_bbox(a), _parcel_bbox(b)
        if box_a and box_b and (box_a[2] < box_b[0] or box_b[2] < box_a[0] or box_a[3] < box_b[1] or box_b[3] < box_a[1]):
            return False
        inter = shape_a.intersection(shape_b).area
        union = shape_a.area + shape_b.area - inter
        return bool(union) and inter / union >= 0.95
    key = parcel_lot_key(a)
    return bool(key) and key == parcel_lot_key(b)


def _group_equivalent(rows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Agrupa los lotes equivalentes cargados por usuarios distintos."""
    shapes: Dict[str, Any] = {}
    for row in rows:
        shape = _parcel_geometry_shape(row)
        shapes[str(row.get("id"))] = shape if shape is not None and not shape.is_empty and shape.area > 0 else None
    groups: List[List[Dict[str, Any]]] = []
    taken: set = set()
    for i, row in enumerate(rows):
        if i in taken:
            continue
        group = [row]
        taken.add(i)
        users_in = {str(row.get("user_id"))}
        for j in range(i + 1, len(rows)):
            other = rows[j]
            if j in taken or str(other.get("user_id")) in users_in:
                continue
            if _same_lot(row, other, shapes):
                group.append(other)
                taken.add(j)
                users_in.add(str(other.get("user_id")))
        groups.append(group)
    return groups


def _pick_keeper(group: List[Dict[str, Any]], keep_from: Optional[str]) -> Dict[str, Any]:
    """Copia que se queda: la del usuario indicado; si no, la que ya está en
    Graniot; y entre iguales, la más reciente."""
    if keep_from:
        preferred = [row for row in group if str(row.get("user_id")) == keep_from]
        if preferred:
            group = preferred
    in_graniot = [row for row in group if _in_graniot(row)]
    candidates = in_graniot or group
    return max(candidates, key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""))


def _sql_mirrored(parcel_ids: List[str]) -> Optional[List[str]]:
    """Lotes ya reflejados en las tablas SQL (Bitácora): borrarlos dejaría sus
    ciclos apuntando a nada. None si la base SQL no está disponible."""
    if not parcel_ids:
        return []
    try:
        from uuid import UUID

        from app.db.session import SessionLocal
        from app.models.parcel import Parcel

        wanted = []
        for value in parcel_ids:
            try:
                wanted.append(UUID(str(value)))
            except (TypeError, ValueError):
                continue
        if not wanted:
            return []
        with SessionLocal() as session:
            found = session.query(Parcel.id).filter(Parcel.id.in_(wanted)).all()
        return [str(row[0]) for row in found]
    except Exception:
        return None


@router.post("/owner")
def set_company_parcel_owner(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Fija el titular de los lotes de una empresa (la cuenta de Graniot que usa)."""
    ctx = _require_manager(authorization)
    company = _target_company(ctx["db"], ctx["permission"], payload.get("company_id"))
    member = _target_user(ctx["db"], ctx["permission"], payload.get("user_id"))
    if str(_company_for_user(ctx["db"], str(member.get("id"))) or "") != str(company.get("id")):
        raise HTTPException(status_code=400, detail="Ese usuario no pertenece a la empresa")
    with LOCK:
        db = read_db()
        row = next(c for c in table(db, "companies") if str(c.get("id")) == str(company.get("id")))
        row[COMPANY_PARCEL_OWNER_FIELD] = str(member.get("id"))
        row["updated_at"] = now()
        write_db(db)
        summary = _company_summary(db, row, _parcels_by_user(db))
    return {"data": {"company": summary}, "error": None}


@router.post("/migrate")
def migrate_company_parcels(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Pasa a la empresa los lotes que su equipo cargó por usuario.

    Los lotes que varias personas tienen repetidos se funden en una sola copia:
    las demás se borran, y todo lo que apuntaba a ellas (notas, imágenes,
    análisis, registros) pasa a apuntar a la que se queda.

    Por defecto SIMULA (`dry_run`, true salvo que llegue false) y devuelve el
    plan. Parámetros: `company_id`; `owner_user_id` fija el titular;
    `keep_from_user_id` indica de quién es la copia que se queda en los
    repetidos; `delete_dropped_in_graniot` borra también de Graniot las copias
    descartadas (por defecto no).
    """
    ctx = _require_manager(authorization)
    dry_run = payload.get("dry_run", True) is not False
    delete_in_graniot = bool(payload.get("delete_dropped_in_graniot"))
    keep_from = str(payload.get("keep_from_user_id") or "").strip() or None

    with LOCK:
        db = read_db()
        company = _target_company(db, ctx["permission"], payload.get("company_id"))
        company_id = str(company.get("id"))
        member_ids = {
            str(u.get("id"))
            for u in db.get("users", [])
            if u.get("id") and str(_company_for_user(db, str(u.get("id"))) or "") == company_id
        }
        owner_id = str(payload.get("owner_user_id") or "").strip()
        if owner_id and owner_id not in member_ids:
            raise HTTPException(status_code=400, detail="El titular indicado no pertenece a la empresa")
        if keep_from and keep_from not in member_ids:
            raise HTTPException(status_code=400, detail="El usuario de las copias a conservar no pertenece a la empresa")

        legacy_all = [
            row
            for row in table(db, "parcels")
            if not row.get("company_id") and str(row.get("user_id") or "") in member_ids
        ]
        # Cada usuario puede tener versiones viejas del mismo lote que la
        # pantalla ya ocultaba: se funden en su versión vigente.
        legacy = dedupe_user_parcels(legacy_all)
        current_ids = {id(row) for row in legacy}
        current_by_key = {
            f"{row.get('user_id')}:{parcel_lot_key(row) or row.get('id')}": row for row in legacy
        }
        dropped: Dict[str, str] = {}
        # Filas que se van, por identidad: en el almacén hay ids repetidos y
        # borrar por id podría llevarse también la copia que se queda.
        dropped_rows: set = set()
        for row in legacy_all:
            if id(row) not in current_ids:
                newest = current_by_key.get(f"{row.get('user_id')}:{parcel_lot_key(row) or row.get('id')}")
                if newest is not None:
                    dropped_rows.add(id(row))
                    if str(row.get("id")) != str(newest.get("id")):
                        dropped[str(row.get("id"))] = str(newest.get("id"))
        groups = _group_equivalent(legacy)
        keepers: List[Dict[str, Any]] = []
        for group in groups:
            keeper = _pick_keeper(group, keep_from)
            keepers.append(keeper)
            for row in group:
                if row is not keeper:
                    dropped_rows.add(id(row))
                    if str(row.get("id")) != str(keeper.get("id")):
                        dropped[str(row.get("id"))] = str(keeper.get("id"))
        # Una versión vieja apunta a la vigente, que a su vez puede haberse
        # fundido en la copia de otra persona: se resuelve hasta la final.
        for old_id, target in list(dropped.items()):
            seen = {old_id}
            while target in dropped and target not in seen:
                seen.add(target)
                target = dropped[target]
            dropped[old_id] = target

        refs: Dict[str, int] = {}
        for name, rows in db.get("tables", {}).items():
            if name == "parcels":
                continue
            count = 0
            for row in rows:
                if any(str(row.get(field) or "") in dropped for field in _PARCEL_REF_FIELDS):
                    count += 1
                elif isinstance(row.get("parcel_ids"), list) and any(str(v) in dropped for v in row["parcel_ids"]):
                    count += 1
            if count:
                refs[name] = count

        users_by_id = {str(u.get("id")): u for u in db.get("users", [])}
        owner_after = users_by_id.get(owner_id) if owner_id else company_parcel_owner(db, company_id)
        plan = {
            "company": {"id": company_id, "name": company.get("name")},
            "owner": {"id": owner_after.get("id"), "email": owner_after.get("email")} if owner_after else None,
            "lots_before": len(legacy),
            "stale_versions": len(legacy_all) - len(legacy),
            "lots_after": len(keepers),
            "groups_merged": sum(1 for g in groups if len(g) > 1),
            "dropped": len(dropped_rows),
            "dropped_by_user": {
                (users_by_id.get(uid) or {}).get("email") or uid: n
                for uid, n in _count_by_user([r for r in legacy_all if id(r) in dropped_rows]).items()
            },
            "dropped_in_graniot": sum(1 for r in legacy_all if id(r) in dropped_rows and _in_graniot(r)),
            "kept_in_graniot": sum(1 for r in keepers if _in_graniot(r)),
            "references_moved": refs,
            "delete_dropped_in_graniot": delete_in_graniot,
            "dry_run": dry_run,
        }
        if not owner_after:
            raise HTTPException(status_code=409, detail="La empresa no tiene ningún usuario activo que pueda ser titular")

        mirrored = _sql_mirrored(list(dropped))
        plan["sql_mirrored_dropped"] = mirrored
        if dry_run:
            return {"data": plan, "error": None}
        if mirrored is None:
            raise HTTPException(status_code=503, detail="No se pudo comprobar la Bitácora (base SQL): no se migra nada")
        if mirrored:
            raise HTTPException(
                status_code=409,
                detail=f"{len(mirrored)} copias a descartar ya se usan en la Bitácora; hay que revisarlas antes de migrar",
            )

        # 1) Referencias de las copias descartadas a la copia que se queda.
        for name, rows in db.get("tables", {}).items():
            if name == "parcels":
                continue
            for row in rows:
                for field in _PARCEL_REF_FIELDS:
                    value = str(row.get(field) or "")
                    if value in dropped:
                        row[field] = dropped[value]
                if isinstance(row.get("parcel_ids"), list):
                    row["parcel_ids"] = list(dict.fromkeys(dropped.get(str(v), v) for v in row["parcel_ids"]))

        # 2) Fuera las copias descartadas.
        removed = [dict(row) for row in table(db, "parcels") if id(row) in dropped_rows]
        db["tables"]["parcels"] = [row for row in table(db, "parcels") if id(row) not in dropped_rows]

        # 3) Lo que queda pasa a la empresa. Lo que ya vive en Graniot sigue a
        #    nombre de quien lo tiene en su cuenta; lo que no, al titular.
        stamp = now()
        keeper_rows = {id(r) for r in keepers}
        for row in table(db, "parcels"):
            if id(row) in keeper_rows:
                row["company_id"] = company_id
                if not _in_graniot(row):
                    row["user_id"] = owner_after.get("id")
                row["migrated_to_company_at"] = stamp
                row["updated_at"] = stamp

        if owner_id:
            company_row = next(c for c in table(db, "companies") if str(c.get("id")) == company_id)
            company_row[COMPANY_PARCEL_OWNER_FIELD] = owner_id
            company_row["updated_at"] = stamp
        write_db(db)

    if delete_in_graniot:
        by_owner: Dict[str, List[Dict[str, Any]]] = {}
        for row in removed:
            by_owner.setdefault(str(row.get("user_id") or ""), []).append(row)
        for uid, owned in by_owner.items():
            if users_by_id.get(uid):
                schedule_graniot_parcel_delete(background_tasks, users_by_id[uid], owned)
    return {"data": plan, "error": None}


def _count_by_user(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        uid = str(row.get("user_id") or "")
        counts[uid] = counts.get(uid, 0) + 1
    return counts


@router.post("/rehome")
def rehome_company_parcels(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Pasa a la cuenta de Graniot del titular los lotes de empresa que viven en otra.

    Con el portal de Graniot por empresa, el equipo ve la cuenta del titular:
    un lote que quedó en la cuenta de otra persona (p. ej. la copia que se
    conservó al fundir repetidos) no aparece ahí. Se pone a nombre del titular y
    se vuelve a subir con su cuenta. La copia vieja NO se borra de la otra
    cuenta: puede ser la cuenta personal de alguien.

    Por defecto SIMULA (`dry_run`). `ids` acota los lotes; sin él, todos los de
    la empresa que no están a nombre del titular.
    """
    ctx = _require_manager(authorization)
    dry_run = payload.get("dry_run", True) is not False
    wanted = {str(v) for v in (payload.get("ids") or []) if v}

    with LOCK:
        db = read_db()
        company = _target_company(db, ctx["permission"], payload.get("company_id"))
        company_id = str(company.get("id"))
        owner = _company_owner(db, company)
        owner_id = str(owner.get("id"))
        users_by_id = {str(u.get("id")): u for u in db.get("users", [])}
        rows = [
            row
            for row in company_parcels(db, company_id)
            if str(row.get("user_id") or "") != owner_id and (not wanted or str(row.get("id")) in wanted)
        ]
        plan = {
            "company": {"id": company_id, "name": company.get("name")},
            "owner": {"id": owner_id, "email": owner.get("email")},
            "count": len(rows),
            "parcels": [
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "area": row.get("area"),
                    "from_user": (users_by_id.get(str(row.get("user_id"))) or {}).get("email") or row.get("user_id"),
                    "from_graniot_account": row.get("graniot_account_email"),
                }
                for row in rows
            ],
            "dry_run": dry_run,
        }
        if dry_run or not rows:
            return {"data": plan, "error": None}

        from app.api.routers.graniot import GRANIOT_LOCAL_SYNC_FIELDS

        stamp = now()
        for row in rows:
            if row.get("graniot_account_email"):
                row["graniot_previous_account_email"] = row.get("graniot_account_email")
            for field in GRANIOT_LOCAL_SYNC_FIELDS:
                row.pop(field, None)
            row["user_id"] = owner_id
            row["updated_at"] = stamp
        moved = [dict(row) for row in rows]
        write_db(db)

    schedule_graniot_parcel_sync(background_tasks, owner, moved, ensure_account=True)
    return {"data": plan, "error": None}
