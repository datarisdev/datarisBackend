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
    LOCK,
    _company_for_user,
    bearer_user,
    company_parcel_owner,
    company_parcels,
    create_manual_parcel_for_user,
    dedupe_user_parcels,
    panel_email_allowed,
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
