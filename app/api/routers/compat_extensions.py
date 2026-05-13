from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException

from app.core.config import settings
from app.api.routers.compat import LOCK, bearer_user, ensure_storage, read_db, table, write_db, now
from app.services.digiforms_user_api import (
    DigiformsAPIError,
    DigiformsUserAPI,
    DigiformsUserPayload,
    build_digiforms_user_id,
    generate_temporary_password,
)

router = APIRouter(prefix="/compat/extensions", tags=["Compatibility Extensions"])

DIGIFORMS_MODULE = {
    "id": "digiforms",
    "name": "DigiformsApp",
    "description": "Formularios digitales de campo, captura offline, GPS, fotos y reportes desde DigiformsApp.",
    "icon": "FileText",
    "is_active": True,
}


def require_user(authorization: Optional[str]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def admin_record_for(db: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (
            row
            for row in table(db, "admin_users")
            if row.get("user_id") == user_id and row.get("is_active", True)
        ),
        None,
    )


def is_admin(row: Optional[Dict[str, Any]]) -> bool:
    return bool(row and row.get("admin_role") in {"superadmin", "company_admin"})


def profile_for(db: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (
            row
            for row in table(db, "profiles")
            if row.get("user_id") == user_id or row.get("id") == user_id
        ),
        None,
    )


def company_for_user(db: Dict[str, Any], user_id: str) -> Optional[str]:
    admin = admin_record_for(db, user_id)
    if admin and admin.get("company_id"):
        return admin.get("company_id")
    profile = profile_for(db, user_id)
    if profile and profile.get("company_id"):
        return profile.get("company_id")
    if profile and profile.get("company_name"):
        company = next((c for c in table(db, "companies") if c.get("name") == profile.get("company_name")), None)
        if company:
            return company.get("id")
    return None


def company_name(db: Dict[str, Any], company_id: Optional[str]) -> Optional[str]:
    if not company_id:
        return None
    company = next((c for c in table(db, "companies") if c.get("id") == company_id), None)
    return company.get("name") if company else None


def request_person_name(db: Dict[str, Any], user: Dict[str, Any]) -> str:
    profile = profile_for(db, user.get("id")) or {}
    parts = [profile.get("first_name"), profile.get("last_name")]
    name = " ".join(str(part).strip() for part in parts if part).strip()
    return name or (user.get("user_metadata") or {}).get("first_name") or user.get("email") or "Usuario"


def ensure_digiforms_module(db: Dict[str, Any]) -> Dict[str, Any]:
    modules = table(db, "platform_modules")
    module = next((m for m in modules if m.get("id") == DIGIFORMS_MODULE["id"]), None)
    if module:
        module.update({k: v for k, v in DIGIFORMS_MODULE.items() if k != "id"})
        module.setdefault("created_at", now())
        module["updated_at"] = now()
        return module
    created = {**DIGIFORMS_MODULE, "created_at": now(), "updated_at": now()}
    modules.append(created)
    return created


def enrich_request(db: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["company_name"] = company_name(db, row.get("company_id"))
    requester = next((u for u in db.get("users", []) if u.get("id") == row.get("requested_by_user_id")), None)
    profile = profile_for(db, row.get("requested_by_user_id")) if row.get("requested_by_user_id") else None
    item["requester_email"] = row.get("requester_email") or (requester or {}).get("email")
    item["requester_name"] = row.get("requester_name") or request_person_name(db, requester or {}) if requester else row.get("requester_name")
    account = next((a for a in table(db, "digiforms_accounts") if a.get("extension_request_id") == row.get("id")), None)
    if account:
        safe_account = dict(account)
        # Keep the temporary credential available to admins only through the admin list;
        # normal user status endpoint removes it.
        item["digiforms_account"] = safe_account
    return item


def extension_enabled_for(db: Dict[str, Any], company_id: Optional[str], user_id: Optional[str]) -> bool:
    if not company_id and not user_id:
        return False
    company_enabled = bool(
        company_id
        and any(
            cm.get("company_id") == company_id
            and cm.get("module_id") == DIGIFORMS_MODULE["id"]
            and cm.get("is_enabled", cm.get("is_active", False))
            for cm in table(db, "company_modules")
        )
    )
    user_enabled = bool(
        user_id
        and any(
            um.get("user_id") == user_id
            and um.get("module_id") == DIGIFORMS_MODULE["id"]
            and um.get("is_enabled", um.get("is_active", False))
            for um in table(db, "user_modules")
        )
    )
    return company_enabled or user_enabled


def enable_extension_for(db: Dict[str, Any], company_id: Optional[str], user_id: Optional[str]) -> None:
    ensure_digiforms_module(db)
    t = now()
    if company_id and not any(cm.get("company_id") == company_id and cm.get("module_id") == DIGIFORMS_MODULE["id"] for cm in table(db, "company_modules")):
        table(db, "company_modules").append({
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "module_id": DIGIFORMS_MODULE["id"],
            "is_enabled": True,
            "is_active": True,
            "created_at": t,
            "updated_at": t,
        })
    elif company_id:
        for cm in table(db, "company_modules"):
            if cm.get("company_id") == company_id and cm.get("module_id") == DIGIFORMS_MODULE["id"]:
                cm["is_enabled"] = True
                cm["is_active"] = True
                cm["updated_at"] = t

    if user_id and not any(um.get("user_id") == user_id and um.get("module_id") == DIGIFORMS_MODULE["id"] for um in table(db, "user_modules")):
        admin = admin_record_for(db, user_id)
        table(db, "user_modules").append({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "admin_user_id": admin.get("id") if admin else None,
            "module_id": DIGIFORMS_MODULE["id"],
            "is_enabled": True,
            "is_active": True,
            "created_at": t,
            "updated_at": t,
        })
    elif user_id:
        for um in table(db, "user_modules"):
            if um.get("user_id") == user_id and um.get("module_id") == DIGIFORMS_MODULE["id"]:
                um["is_enabled"] = True
                um["is_active"] = True
                um["updated_at"] = t


def request_visibility_filter(db: Dict[str, Any], user: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    admin = admin_record_for(db, user.get("id"))
    if admin and admin.get("admin_role") == "superadmin":
        return rows
    if admin and admin.get("admin_role") == "company_admin":
        return [r for r in rows if r.get("company_id") == admin.get("company_id")]
    return [r for r in rows if r.get("requested_by_user_id") == user.get("id")]


@router.get("/digiforms/status")
def digiforms_status(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        ensure_digiforms_module(db)
        company_id = company_for_user(db, user["id"])
        rows = [r for r in table(db, "extension_requests") if r.get("extension_id") == DIGIFORMS_MODULE["id"] and r.get("requested_by_user_id") == user["id"]]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        latest = enrich_request(db, rows[0]) if rows else None
        if latest and latest.get("digiforms_account"):
            latest["digiforms_account"].pop("initial_password", None)
        enabled = extension_enabled_for(db, company_id, user["id"])
        write_db(db)
    return {
        "data": {
            "extension_id": DIGIFORMS_MODULE["id"],
            "enabled": enabled,
            "status": "enabled" if enabled else ((latest or {}).get("status") or "not_requested"),
            "request": latest,
            "portal_url": settings.DIGIFORMS_PORTAL_URL,
            "client_id": settings.DIGIFORMS_CLIENT_ID,
        },
        "error": None,
    }


@router.post("/requests")
def create_extension_request(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    extension_id = str(payload.get("extension_id") or DIGIFORMS_MODULE["id"]).strip().lower()
    if extension_id != DIGIFORMS_MODULE["id"]:
        raise HTTPException(status_code=400, detail="Por ahora este flujo solo está habilitado para DigiformsApp")

    with LOCK:
        db = read_db()
        ensure_digiforms_module(db)
        company_id = company_for_user(db, user["id"])
        if extension_enabled_for(db, company_id, user["id"]):
            return {"data": {"already_enabled": True, "message": "DigiformsApp ya está habilitada para tu cuenta."}, "error": None}

        existing_open = next(
            (
                r
                for r in table(db, "extension_requests")
                if r.get("extension_id") == extension_id
                and r.get("requested_by_user_id") == user["id"]
                and r.get("status") in {"pending", "in_review"}
            ),
            None,
        )
        if existing_open:
            return {"data": {"request": enrich_request(db, existing_open), "message": "Ya tienes una solicitud en evaluación."}, "error": None}

        request_type = "existing_account" if payload.get("has_existing_account") else "needs_account"
        t = now()
        row = {
            "id": str(uuid.uuid4()),
            "extension_id": extension_id,
            "extension_name": "DigiformsApp",
            "status": "pending",
            "request_type": request_type,
            "has_existing_account": bool(payload.get("has_existing_account")),
            "existing_digiforms_user_id": (payload.get("existing_digiforms_user_id") or "").strip() or None,
            "contact_notes": (payload.get("contact_notes") or "").strip() or None,
            "company_id": company_id,
            "company_name_snapshot": company_name(db, company_id),
            "requested_by_user_id": user["id"],
            "requester_email": user.get("email"),
            "requester_name": request_person_name(db, user),
            "client_message": "Recibimos tu solicitud para activar DigiformsApp. El equipo de Dataris verificará la cuenta, permisos y compatibilidad antes de habilitarla.",
            "admin_notes": None,
            "reviewed_by_user_id": None,
            "reviewed_at": None,
            "enabled_at": None,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "extension_requests").append(row)
        write_db(db)
    return {"data": {"request": enrich_request(db, row), "message": row["client_message"]}, "error": None}


@router.get("/requests")
def list_extension_requests(status: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        rows = [r for r in table(db, "extension_requests") if r.get("extension_id") == DIGIFORMS_MODULE["id"]]
        rows = request_visibility_filter(db, user, rows)
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        admin = admin_record_for(db, user.get("id"))
        enriched = [enrich_request(db, row) for row in rows]
        if not is_admin(admin):
            for row in enriched:
                if row.get("digiforms_account"):
                    row["digiforms_account"].pop("initial_password", None)
    return {"data": enriched, "error": None, "count": len(enriched)}


@router.post("/requests/{request_id}/reject")
def reject_extension_request(request_id: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        admin = admin_record_for(db, user.get("id"))
        if not is_admin(admin):
            raise HTTPException(status_code=403, detail="No tienes permisos para revisar solicitudes")
        row = next((r for r in table(db, "extension_requests") if r.get("id") == request_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        if admin.get("admin_role") == "company_admin" and row.get("company_id") != admin.get("company_id"):
            raise HTTPException(status_code=403, detail="No puedes revisar solicitudes de otra empresa")
        t = now()
        row.update({
            "status": "rejected",
            "admin_notes": (payload.get("admin_notes") or payload.get("reason") or "").strip() or "Solicitud rechazada por administración.",
            "client_message": (payload.get("client_message") or "Tu solicitud de DigiformsApp fue revisada, pero aún no puede habilitarse. El equipo de Dataris te contactará con más información.").strip(),
            "reviewed_by_user_id": user["id"],
            "reviewed_at": t,
            "updated_at": t,
        })
        write_db(db)
    return {"data": enrich_request(db, row), "error": None}


@router.post("/requests/{request_id}/approve")
async def approve_extension_request(request_id: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        admin = admin_record_for(db, user.get("id"))
        if not is_admin(admin):
            raise HTTPException(status_code=403, detail="No tienes permisos para habilitar extensiones")
        row = next((r for r in table(db, "extension_requests") if r.get("id") == request_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        if admin.get("admin_role") == "company_admin" and row.get("company_id") != admin.get("company_id"):
            raise HTTPException(status_code=403, detail="No puedes habilitar solicitudes de otra empresa")
        row["status"] = "in_review"
        row["updated_at"] = now()
        write_db(db)

    # External Digiforms call happens outside the lock.
    api = DigiformsUserAPI()
    api_status = "not_configured"
    api_response: Any = None
    generated_password: Optional[str] = None
    digiforms_user_id = row.get("existing_digiforms_user_id")
    digiforms_user_name = row.get("requester_name") or "Usuario Dataris"
    digiforms_email = row.get("requester_email") or ""
    mode = "existing" if row.get("has_existing_account") else "generated"

    try:
        if settings.DIGIFORMS_PROVISIONING_ENABLED and api.is_configured:
            if row.get("has_existing_account"):
                if digiforms_user_id:
                    api_response = await api.get_user(str(digiforms_user_id))
                    api_status = "verified_existing"
                else:
                    api_status = "existing_not_verified_missing_user_id"
            else:
                generated_password = generate_temporary_password()
                digiforms_user_id = payload.get("digiforms_user_id") or build_digiforms_user_id(digiforms_email, digiforms_user_name)
                created = DigiformsUserPayload(
                    user_id=str(digiforms_user_id),
                    client_id=int(settings.DIGIFORMS_CLIENT_ID),
                    user_name=digiforms_user_name,
                    password=generated_password,
                    email=digiforms_email,
                    active=True,
                    profile=str(settings.DIGIFORMS_DEFAULT_PROFILE or "user"),
                )
                api_response = await api.create_user(created)
                api_status = "created_in_digiforms"
        elif not api.is_configured:
            api_status = "pending_external_provision_missing_env"
        else:
            api_status = "provisioning_disabled"
    except DigiformsAPIError as exc:
        with LOCK:
            db = read_db()
            row = next((r for r in table(db, "extension_requests") if r.get("id") == request_id), row)
            row.update({
                "status": "provision_failed",
                "admin_notes": f"Error DigiformsApp: {exc}. {exc.response_text or ''}".strip(),
                "client_message": "Estamos revisando tu solicitud de DigiformsApp. La habilitación quedó pendiente por una validación técnica externa.",
                "reviewed_by_user_id": user["id"],
                "reviewed_at": now(),
                "updated_at": now(),
            })
            write_db(db)
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo completar la provisión: {exc}")

    with LOCK:
        db = read_db()
        row = next((r for r in table(db, "extension_requests") if r.get("id") == request_id), row)
        enable_extension_for(db, row.get("company_id"), row.get("requested_by_user_id"))
        t = now()
        row.update({
            "status": "approved",
            "admin_notes": (payload.get("admin_notes") or "Extensión revisada y habilitada desde el panel de administración.").strip(),
            "client_message": "Tu extensión DigiformsApp ya fue habilitada. Ahora puedes crear y utilizar formularios de campo desde Dataris.",
            "reviewed_by_user_id": user["id"],
            "reviewed_at": t,
            "enabled_at": t,
            "updated_at": t,
        })
        account = {
            "id": str(uuid.uuid4()),
            "company_id": row.get("company_id"),
            "user_id": row.get("requested_by_user_id"),
            "extension_request_id": row.get("id"),
            "digiforms_client_id": settings.DIGIFORMS_CLIENT_ID,
            "digiforms_user_id": str(digiforms_user_id or ""),
            "digiforms_user_name": digiforms_user_name,
            "digiforms_email": digiforms_email,
            "profile": settings.DIGIFORMS_DEFAULT_PROFILE,
            "mode": mode,
            "active": True,
            "initial_password": generated_password,
            "api_status": api_status,
            "api_response": api_response,
            "created_at": t,
            "updated_at": t,
        }
        # Replace account for this request if it already exists.
        table(db, "digiforms_accounts")[:] = [a for a in table(db, "digiforms_accounts") if a.get("extension_request_id") != row.get("id")]
        table(db, "digiforms_accounts").append(account)
        write_db(db)
        data = enrich_request(db, row)
    return {"data": data, "error": None}
