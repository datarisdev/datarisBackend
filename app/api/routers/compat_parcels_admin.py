"""Gestión de lotes por parte del equipo de Dataris.

El cliente ya no carga sus lotes desde el perfil: los da de alta el equipo de
desarrollo/comercial desde el panel de administración. Aquí viven los endpoints
que permiten trabajar sobre los lotes de OTRO usuario, siempre acotados por el
permiso `can_manage_parcels` de la fila `admin_users` del solicitante.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, Header, HTTPException, UploadFile

from app.api.routers.compat import (
    LOCK,
    _company_for_user,
    bearer_user,
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
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in dedupe_user_parcels(table(db, "parcels")):
        grouped.setdefault(str(row.get("user_id") or ""), []).append(row)
    return grouped


def _sorted_user_parcels(db: Dict[str, Any], user_id: str) -> List[Dict[str, Any]]:
    rows = [row for row in table(db, "parcels") if str(row.get("user_id") or "") == user_id]
    rows = dedupe_user_parcels(rows)
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)


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


@router.get("/list")
def list_user_parcels(user_id: str, authorization: Optional[str] = Header(default=None)):
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


@router.post("/upload")
async def upload_parcel_for_user(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(...),
    user_id: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    ctx = _require_manager(authorization)
    owner = _target_user(ctx["db"], ctx["permission"], user_id)
    created_rows = await store_parcel_file_for_user(owner, file, name)
    schedule_graniot_parcel_sync(background_tasks, owner, created_rows)
    return {"data": {"parcel": created_rows[0], "parcels": created_rows}, "error": None}


@router.post("/manual")
def create_manual_parcel_for_target_user(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    ctx = _require_manager(authorization)
    owner = _target_user(ctx["db"], ctx["permission"], payload.get("user_id"))
    row = create_manual_parcel_for_user(owner, payload.get("name"), payload.get("geometry"))
    schedule_graniot_parcel_sync(background_tasks, owner, [row])
    return {"data": {"parcel": row}, "error": None}


@router.post("/delete")
def delete_parcels_for_user(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    ctx = _require_manager(authorization)
    owner = _target_user(ctx["db"], ctx["permission"], payload.get("user_id"))
    owner_id = str(owner.get("id"))

    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list):
        raw_ids = [payload.get("id")] if payload.get("id") else []
    ids = {str(value) for value in raw_ids if value}
    if not ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos un lote")

    with LOCK:
        db = read_db()
        rows = table(db, "parcels")
        removed = [
            dict(row)
            for row in rows
            if str(row.get("id")) in ids and str(row.get("user_id") or "") == owner_id
        ]
        if removed:
            removed_ids = {str(row.get("id")) for row in removed}
            db["tables"]["parcels"] = [
                row
                for row in rows
                if not (str(row.get("id")) in removed_ids and str(row.get("user_id") or "") == owner_id)
            ]
            write_db(db)

    if not removed:
        raise HTTPException(status_code=404, detail="No se encontraron lotes de ese usuario con los ids indicados")

    # El lote desaparece de Dataris: quítalo también de la cuenta de Graniot del
    # usuario para que ambos lados queden igual.
    schedule_graniot_parcel_delete(background_tasks, owner, removed)
    return {"data": {"deleted": [row.get("id") for row in removed], "count": len(removed)}, "error": None}
