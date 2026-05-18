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
    "is_extension": True,
    "is_requestable": True,
}

GRANIOT_MODULE = {
    "id": "graniot",
    "name": "Graniot",
    "description": "Integración para capas satelitales, NDVI, fechas, estadísticas y sincronización de lotes desde Graniot.",
    "icon": "Leaf",
    "is_active": True,
    "is_extension": True,
    "is_requestable": True,
}

DEFAULT_EXTENSION_MODULES = [DIGIFORMS_MODULE, GRANIOT_MODULE]


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


def normalize_extension_id(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def ensure_extension_catalog(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    modules = table(db, "platform_modules")
    t = now()
    for base in DEFAULT_EXTENSION_MODULES:
        module = next((m for m in modules if normalize_extension_id(m.get("id") or m.get("name")) == base["id"]), None)
        if module:
            # No forzamos is_active si el admin ya la desactivó; solo completamos metadata.
            module.setdefault("id", base["id"])
            module.setdefault("name", base["name"])
            module.setdefault("description", base["description"])
            module.setdefault("icon", base["icon"])
            module.setdefault("is_active", base["is_active"])
            module["is_extension"] = True
            module.setdefault("is_requestable", True)
            module.setdefault("created_at", t)
            module["updated_at"] = module.get("updated_at") or t
        else:
            modules.append({**base, "created_at": t, "updated_at": t})
    return [m for m in modules if m.get("is_extension") or normalize_extension_id(m.get("id") or m.get("name")) in {"digiforms", "graniot"}]


def ensure_digiforms_module(db: Dict[str, Any]) -> Dict[str, Any]:
    return next((m for m in ensure_extension_catalog(db) if normalize_extension_id(m.get("id") or m.get("name")) == DIGIFORMS_MODULE["id"]), DIGIFORMS_MODULE)


def extension_module_for(db: Dict[str, Any], extension_id: str) -> Optional[Dict[str, Any]]:
    ensure_extension_catalog(db)
    normalized = normalize_extension_id(extension_id)
    return next((m for m in table(db, "platform_modules") if normalize_extension_id(m.get("id") or m.get("name")) == normalized), None)


def safe_extension_name(db: Dict[str, Any], extension_id: str) -> str:
    module = extension_module_for(db, extension_id)
    return str((module or {}).get("name") or extension_id)


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


def extension_enabled_for(db: Dict[str, Any], company_id: Optional[str], user_id: Optional[str], extension_id: str = DIGIFORMS_MODULE["id"]) -> bool:
    extension_id = normalize_extension_id(extension_id)
    if not company_id and not user_id:
        return False
    company_enabled = bool(
        company_id
        and any(
            cm.get("company_id") == company_id
            and normalize_extension_id(cm.get("module_id")) == extension_id
            and cm.get("is_enabled", cm.get("is_active", False)) is not False
            and cm.get("is_active", cm.get("is_enabled", True)) is not False
            for cm in table(db, "company_modules")
        )
    )
    user_enabled = bool(
        user_id
        and any(
            um.get("user_id") == user_id
            and normalize_extension_id(um.get("module_id")) == extension_id
            and um.get("is_enabled", um.get("is_active", False)) is not False
            and um.get("is_active", um.get("is_enabled", True)) is not False
            for um in table(db, "user_modules")
        )
    )
    approved_request_enabled = bool(
        any(
            normalize_extension_id(r.get("extension_id")) == extension_id
            and r.get("status") in {"approved", "enabled"}
            and (
                (user_id and r.get("requested_by_user_id") == user_id)
                or (company_id and r.get("company_id") == company_id)
            )
            for r in table(db, "extension_requests")
        )
    )
    return company_enabled or user_enabled or approved_request_enabled


def enable_extension_for(db: Dict[str, Any], company_id: Optional[str], user_id: Optional[str], extension_id: str = DIGIFORMS_MODULE["id"]) -> None:
    ensure_extension_catalog(db)
    extension_id = normalize_extension_id(extension_id)
    t = now()
    if company_id and not any(cm.get("company_id") == company_id and normalize_extension_id(cm.get("module_id")) == extension_id for cm in table(db, "company_modules")):
        table(db, "company_modules").append({
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "module_id": extension_id,
            "is_enabled": True,
            "is_active": True,
            "created_at": t,
            "updated_at": t,
        })
    elif company_id:
        for cm in table(db, "company_modules"):
            if cm.get("company_id") == company_id and normalize_extension_id(cm.get("module_id")) == extension_id:
                cm["is_enabled"] = True
                cm["is_active"] = True
                cm["updated_at"] = t

    if user_id and not any(um.get("user_id") == user_id and normalize_extension_id(um.get("module_id")) == extension_id for um in table(db, "user_modules")):
        admin = admin_record_for(db, user_id)
        table(db, "user_modules").append({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "admin_user_id": admin.get("id") if admin else None,
            "module_id": extension_id,
            "is_enabled": True,
            "is_active": True,
            "created_at": t,
            "updated_at": t,
        })
    elif user_id:
        for um in table(db, "user_modules"):
            if um.get("user_id") == user_id and normalize_extension_id(um.get("module_id")) == extension_id:
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


@router.get("/catalog")
def list_extensions_catalog(authorization: Optional[str] = Header(default=None)):
    # Requiere sesión para que el catálogo pueda incluir estado del usuario.
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        rows = ensure_extension_catalog(db)
        company_id = company_for_user(db, user["id"])
        result = []
        for row in rows:
            extension_id = normalize_extension_id(row.get("id") or row.get("name"))
            result.append({
                "id": extension_id,
                "name": row.get("name") or extension_id,
                "description": row.get("description"),
                "icon": row.get("icon"),
                "is_active": row.get("is_active", True) is not False,
                "is_requestable": row.get("is_requestable", True) is not False,
                "enabled": extension_enabled_for(db, company_id, user["id"], extension_id),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            })
        write_db(db)
    return {"data": result, "error": None, "count": len(result)}


@router.post("/catalog")
def create_extension_catalog_item(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        admin = admin_record_for(db, user.get("id"))
        if not is_admin(admin):
            raise HTTPException(status_code=403, detail="No tienes permisos para administrar extensiones")
        extension_id = normalize_extension_id(payload.get("id") or payload.get("name"))
        if not extension_id:
            raise HTTPException(status_code=400, detail="El id o nombre de la extensión es obligatorio")
        ensure_extension_catalog(db)
        modules = table(db, "platform_modules")
        existing = next((m for m in modules if normalize_extension_id(m.get("id") or m.get("name")) == extension_id), None)
        t = now()
        item = {
            "id": extension_id,
            "name": str(payload.get("name") or extension_id).strip(),
            "description": (payload.get("description") or "").strip() or None,
            "icon": (payload.get("icon") or "Puzzle").strip(),
            "is_active": payload.get("is_active", True) is not False,
            "is_requestable": payload.get("is_requestable", True) is not False,
            "is_extension": True,
            "updated_at": t,
        }
        if existing:
            existing.update(item)
            existing.setdefault("created_at", t)
            row = existing
        else:
            row = {**item, "created_at": t}
            modules.append(row)
        write_db(db)
    return {"data": row, "error": None}


@router.put("/catalog/{extension_id}")
def update_extension_catalog_item(extension_id: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        admin = admin_record_for(db, user.get("id"))
        if not is_admin(admin):
            raise HTTPException(status_code=403, detail="No tienes permisos para administrar extensiones")
        ensure_extension_catalog(db)
        normalized = normalize_extension_id(extension_id)
        row = next((m for m in table(db, "platform_modules") if normalize_extension_id(m.get("id") or m.get("name")) == normalized), None)
        if not row:
            raise HTTPException(status_code=404, detail="Extensión no encontrada")
        for field in ["name", "description", "icon", "is_active", "is_requestable"]:
            if field in payload:
                row[field] = payload[field]
        row["is_extension"] = True
        row["updated_at"] = now()
        write_db(db)
    return {"data": row, "error": None}


@router.get("/{extension_id}/status")
def generic_extension_status(extension_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    normalized = normalize_extension_id(extension_id)
    with LOCK:
        db = read_db()
        module = extension_module_for(db, normalized)
        if not module or module.get("is_active", True) is False:
            return {"data": {"extension_id": normalized, "enabled": False, "status": "unavailable", "request": None}, "error": None}
        company_id = company_for_user(db, user["id"])
        rows = [
            r for r in table(db, "extension_requests")
            if normalize_extension_id(r.get("extension_id")) == normalized
            and (
                r.get("requested_by_user_id") == user["id"]
                or (company_id and r.get("company_id") == company_id and r.get("status") in {"approved", "enabled"})
            )
        ]
        rows.sort(key=lambda r: r.get("enabled_at") or r.get("updated_at") or r.get("created_at", ""), reverse=True)
        latest = enrich_request(db, rows[0]) if rows else None
        enabled = extension_enabled_for(db, company_id, user["id"], normalized)
    return {"data": {"extension_id": normalized, "enabled": enabled, "status": "enabled" if enabled else ((latest or {}).get("status") or "not_requested"), "request": latest}, "error": None}


@router.get("/digiforms/status")
def digiforms_status(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        ensure_digiforms_module(db)
        company_id = company_for_user(db, user["id"])

        # El estado debe ser estable aunque la extensión haya sido habilitada a
        # nivel de empresa y no desde una solicitud creada por este usuario.
        # Por eso revisamos solicitudes del usuario y también solicitudes
        # aprobadas de la misma empresa.
        rows = [
            r
            for r in table(db, "extension_requests")
            if r.get("extension_id") == DIGIFORMS_MODULE["id"]
            and (
                r.get("requested_by_user_id") == user["id"]
                or (company_id and r.get("company_id") == company_id and r.get("status") in {"approved", "enabled"})
            )
        ]
        rows.sort(key=lambda r: r.get("enabled_at") or r.get("updated_at") or r.get("created_at", ""), reverse=True)
        latest = enrich_request(db, rows[0]) if rows else None
        if latest and latest.get("digiforms_account"):
            latest["digiforms_account"].pop("initial_password", None)

        enabled = extension_enabled_for(db, company_id, user["id"])
        if enabled:
            # Autorrepara registros antiguos: si existe una solicitud aprobada
            # pero faltó crear el company_module/user_module, lo regeneramos para
            # que el sidebar y la pantalla de extensiones no cambien de estado al
            # recargar.
            enable_extension_for(db, company_id, user["id"])

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
    extension_id = normalize_extension_id(payload.get("extension_id") or DIGIFORMS_MODULE["id"])

    with LOCK:
        db = read_db()
        module = extension_module_for(db, extension_id)
        if not module or module.get("is_active", True) is False or module.get("is_requestable", True) is False:
            raise HTTPException(status_code=400, detail="Esta extensión no está disponible para solicitud")
        company_id = company_for_user(db, user["id"])
        if extension_enabled_for(db, company_id, user["id"], extension_id):
            enable_extension_for(db, company_id, user["id"], extension_id)
            write_db(db)
            return {"data": {"already_enabled": True, "message": f"{safe_extension_name(db, extension_id)} ya está habilitada para tu cuenta."}, "error": None}

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
            "extension_name": safe_extension_name(db, extension_id),
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
            "client_message": f"Recibimos tu solicitud para activar {safe_extension_name(db, extension_id)}. El equipo de Dataris verificará la cuenta, permisos y compatibilidad antes de habilitarla.",
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
        ensure_extension_catalog(db)
        rows = [r for r in table(db, "extension_requests")]
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
            "client_message": (payload.get("client_message") or "Tu solicitud de extensión fue revisada, pero aún no puede habilitarse. El equipo de Dataris te contactará con más información.").strip(),
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
        extension_id = normalize_extension_id(row.get("extension_id") or DIGIFORMS_MODULE["id"])
        if extension_id != DIGIFORMS_MODULE["id"]:
            t = now()
            enable_extension_for(db, row.get("company_id"), row.get("requested_by_user_id"), extension_id)
            row.update({
                "status": "approved",
                "admin_notes": (payload.get("admin_notes") or f"Extensión {safe_extension_name(db, extension_id)} revisada y habilitada desde el panel de administración.").strip(),
                "client_message": f"Tu extensión {safe_extension_name(db, extension_id)} ya fue habilitada. Ahora puedes utilizarla desde Dataris.",
                "reviewed_by_user_id": user["id"],
                "reviewed_at": t,
                "enabled_at": t,
                "updated_at": t,
            })
            write_db(db)
            return {"data": enrich_request(db, row), "error": None}
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
        enable_extension_for(db, row.get("company_id"), row.get("requested_by_user_id"), DIGIFORMS_MODULE["id"])
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

        # Also expose the approved/provisioned base account in the DigiformsApp
        # user-management section. This keeps approval, provisioning and daily
        # user administration in the same operational view.
        if digiforms_user_id:
            existing_link = next(
                (
                    link
                    for link in table(db, "digiforms_user_links")
                    if link.get("digiforms_client_id") == settings.DIGIFORMS_CLIENT_ID
                    and link.get("digiforms_user_id") == str(digiforms_user_id)
                    and link.get("company_id") == row.get("company_id")
                ),
                None,
            )
            link_payload = {
                "company_id": row.get("company_id"),
                "dataris_user_id": row.get("requested_by_user_id"),
                "created_by_user_id": user.get("id"),
                "digiforms_client_id": settings.DIGIFORMS_CLIENT_ID,
                "digiforms_user_id": str(digiforms_user_id),
                "digiforms_user_name": digiforms_user_name,
                "digiforms_email": digiforms_email,
                "profile": settings.DIGIFORMS_DEFAULT_PROFILE,
                "mode": mode,
                "active": True,
                "external_status": api_status,
                "last_api_action": "approve_extension",
                "last_api_status": api_status,
                "last_api_response": api_response,
                "updated_at": t,
            }
            if existing_link:
                existing_link.update(link_payload)
                if generated_password:
                    existing_link["initial_password"] = generated_password
                    existing_link["temporary_password_was_generated"] = True
            else:
                table(db, "digiforms_user_links").append({
                    "id": str(uuid.uuid4()),
                    **link_payload,
                    "initial_password": generated_password,
                    "temporary_password_was_generated": bool(generated_password),
                    "created_at": t,
                })

        write_db(db)
        data = enrich_request(db, row)
    return {"data": data, "error": None}


# ---------------------------------------------------------------------------
# DigiformsApp User API management
# ---------------------------------------------------------------------------

def normalize_digiforms_profile(value: Any) -> str:
    profile = str(value or "user").strip().lower()
    return profile if profile in {"admin", "user"} else "user"


def require_digiforms_module_access(db: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    admin = admin_record_for(db, user.get("id"))
    company_id = company_for_user(db, user.get("id"))
    if is_admin(admin) or extension_enabled_for(db, company_id, user.get("id")):
        return {"admin": admin, "company_id": company_id}
    raise HTTPException(status_code=403, detail="DigiformsApp no está habilitado para tu empresa o usuario")


def current_link_scope(db: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    access = require_digiforms_module_access(db, user)
    admin = access.get("admin")
    company_id = access.get("company_id")
    return {"admin": admin, "company_id": company_id, "user_id": user.get("id")}


def link_visible_for_scope(row: Dict[str, Any], scope: Dict[str, Any]) -> bool:
    admin = scope.get("admin")
    if admin and admin.get("admin_role") == "superadmin":
        return True
    if admin and admin.get("admin_role") == "company_admin":
        return row.get("company_id") == admin.get("company_id")
    company_id = scope.get("company_id")
    if company_id:
        return row.get("company_id") == company_id
    return row.get("created_by_user_id") == scope.get("user_id") or row.get("dataris_user_id") == scope.get("user_id")


def find_digiforms_link(db: Dict[str, Any], link_id: str, scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    row = next((r for r in table(db, "digiforms_user_links") if r.get("id") == link_id), None)
    if not row:
        return None
    if not link_visible_for_scope(row, scope):
        raise HTTPException(status_code=403, detail="No puedes administrar usuarios DigiformsApp de otra empresa")
    return row


def safe_digiforms_link(row: Dict[str, Any], include_secret: bool = False) -> Dict[str, Any]:
    item = dict(row)
    if not include_secret:
        item.pop("initial_password", None)
        item.pop("last_plain_password", None)
    return item


def create_operation_log(db: Dict[str, Any], *, user_id: str, action: str, status: str, target_user_id: Optional[str] = None, message: Optional[str] = None, response: Any = None) -> None:
    table(db, "digiforms_operation_logs").append({
        "id": str(uuid.uuid4()),
        "action": action,
        "status": status,
        "target_digiforms_user_id": target_user_id,
        "message": message,
        "response": response,
        "created_by_user_id": user_id,
        "created_at": now(),
    })


def digiforms_api_or_error() -> DigiformsUserAPI:
    api = DigiformsUserAPI()
    if not api.is_configured:
        raise HTTPException(
            status_code=503,
            detail="DigiformsApp User API no está configurada. Define DIGIFORMS_BASE_URL, DIGIFORMS_CLIENT_ID, DIGIFORMS_API_USER y DIGIFORMS_API_PASSWORD en el backend.",
        )
    if not settings.DIGIFORMS_PROVISIONING_ENABLED:
        raise HTTPException(status_code=503, detail="La provisión vía DigiformsApp User API está deshabilitada en el backend.")
    return api


@router.get("/digiforms/connection-test")
async def test_digiforms_connection(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        current_link_scope(db, user)

    api = DigiformsUserAPI()
    configured = api.is_configured
    payload: Dict[str, Any] = {
        "configured": configured,
        "client_id": settings.DIGIFORMS_CLIENT_ID,
        "api_user": settings.DIGIFORMS_API_USER,
        "base_url": settings.DIGIFORMS_BASE_URL,
        "portal_url": settings.DIGIFORMS_PORTAL_URL,
        "provisioning_enabled": settings.DIGIFORMS_PROVISIONING_ENABLED,
        "external_ok": False,
        "external_status": "not_configured" if not configured else "not_tested",
    }
    if not configured or not settings.DIGIFORMS_PROVISIONING_ENABLED:
        return {"data": payload, "error": None}

    try:
        response = await api.get_user(settings.DIGIFORMS_API_USER)
        payload.update({"external_ok": True, "external_status": "ok", "response": response})
    except DigiformsAPIError as exc:
        payload.update({"external_ok": False, "external_status": "error", "message": str(exc), "response_text": exc.response_text, "status_code": exc.status_code})
    return {"data": payload, "error": None}


@router.get("/digiforms/users")
def list_digiforms_users(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        rows = [safe_digiforms_link(r) for r in table(db, "digiforms_user_links") if link_visible_for_scope(r, scope)]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"data": rows, "error": None, "count": len(rows)}


@router.get("/digiforms/users/lookup/{digiforms_user_id}")
async def lookup_digiforms_user(digiforms_user_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        current_link_scope(db, user)
    api = digiforms_api_or_error()
    try:
        response = await api.get_user(digiforms_user_id)
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar el usuario en DigiformsApp: {exc}")
    return {"data": {"digiforms_user_id": digiforms_user_id, "response": response}, "error": None}


@router.post("/digiforms/users/link")
async def link_existing_digiforms_user(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    digiforms_user_id = str(payload.get("digiforms_user_id") or "").strip()
    if not digiforms_user_id:
        raise HTTPException(status_code=400, detail="Debes indicar el UserId existente de DigiformsApp")

    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = str(payload.get("company_id") or scope.get("company_id") or "") or None
        if scope.get("admin") and scope["admin"].get("admin_role") == "company_admin":
            company_id = scope["admin"].get("company_id")
        duplicate = next(
            (
                r for r in table(db, "digiforms_user_links")
                if r.get("digiforms_client_id") == settings.DIGIFORMS_CLIENT_ID
                and r.get("digiforms_user_id") == digiforms_user_id
                and (not company_id or r.get("company_id") == company_id)
            ),
            None,
        )
        if duplicate:
            return {"data": safe_digiforms_link(duplicate), "error": None, "message": "El usuario ya estaba vinculado en Dataris."}

    api = digiforms_api_or_error()
    try:
        external_response = await api.get_user(digiforms_user_id)
        external_status = "verified_existing"
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo validar el usuario existente: {exc}")

    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = str(payload.get("company_id") or scope.get("company_id") or "") or None
        if scope.get("admin") and scope["admin"].get("admin_role") == "company_admin":
            company_id = scope["admin"].get("company_id")
        t = now()
        row = {
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "dataris_user_id": payload.get("dataris_user_id") or user.get("id"),
            "created_by_user_id": user.get("id"),
            "digiforms_client_id": settings.DIGIFORMS_CLIENT_ID,
            "digiforms_user_id": digiforms_user_id,
            "digiforms_user_name": str(payload.get("user_name") or payload.get("digiforms_user_name") or "").strip() or digiforms_user_id,
            "digiforms_email": str(payload.get("email") or "").strip() or None,
            "profile": normalize_digiforms_profile(payload.get("profile") or "user"),
            "mode": "existing",
            "active": True,
            "external_status": external_status,
            "last_api_action": "lookup",
            "last_api_status": external_status,
            "last_api_response": external_response,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "digiforms_user_links").append(row)
        create_operation_log(db, user_id=user.get("id"), action="link_existing_user", status="ok", target_user_id=digiforms_user_id, response=external_response)
        write_db(db)
    return {"data": safe_digiforms_link(row), "error": None}


@router.post("/digiforms/users")
async def create_digiforms_user(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    user_name = str(payload.get("user_name") or payload.get("digiforms_user_name") or "").strip()
    email = str(payload.get("email") or payload.get("digiforms_email") or "").strip()
    profile = normalize_digiforms_profile(payload.get("profile") or settings.DIGIFORMS_DEFAULT_PROFILE)
    if not user_name:
        raise HTTPException(status_code=400, detail="Debes indicar el nombre del usuario DigiformsApp")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Debes indicar un correo válido para el usuario DigiformsApp")

    digiforms_user_id = str(payload.get("digiforms_user_id") or "").strip() or build_digiforms_user_id(email, user_name)
    password = str(payload.get("password") or "").strip()
    generated_password = False
    if not password:
        password = generate_temporary_password()
        generated_password = True

    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = str(payload.get("company_id") or scope.get("company_id") or "") or None
        if scope.get("admin") and scope["admin"].get("admin_role") == "company_admin":
            company_id = scope["admin"].get("company_id")
        duplicate = next(
            (
                r for r in table(db, "digiforms_user_links")
                if r.get("digiforms_client_id") == settings.DIGIFORMS_CLIENT_ID
                and r.get("digiforms_user_id") == digiforms_user_id
                and (not company_id or r.get("company_id") == company_id)
            ),
            None,
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="Ya existe un usuario DigiformsApp vinculado con ese UserId")

    api = digiforms_api_or_error()
    api_payload = DigiformsUserPayload(
        user_id=digiforms_user_id,
        client_id=int(settings.DIGIFORMS_CLIENT_ID),
        user_name=user_name,
        password=password,
        email=email,
        active=True,
        profile=profile,
    )
    try:
        external_response = await api.create_user(api_payload)
        external_status = "created_in_digiforms"
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo crear el usuario: {exc}. {exc.response_text or ''}".strip())

    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = str(payload.get("company_id") or scope.get("company_id") or "") or None
        if scope.get("admin") and scope["admin"].get("admin_role") == "company_admin":
            company_id = scope["admin"].get("company_id")
        t = now()
        row = {
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "dataris_user_id": payload.get("dataris_user_id") or user.get("id"),
            "created_by_user_id": user.get("id"),
            "digiforms_client_id": settings.DIGIFORMS_CLIENT_ID,
            "digiforms_user_id": digiforms_user_id,
            "digiforms_user_name": user_name,
            "digiforms_email": email,
            "profile": profile,
            "mode": "generated",
            "active": True,
            "external_status": external_status,
            "last_api_action": "create",
            "last_api_status": external_status,
            "last_api_response": external_response,
            "initial_password": password if generated_password else None,
            "temporary_password_was_generated": generated_password,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "digiforms_user_links").append(row)
        create_operation_log(db, user_id=user.get("id"), action="create_user", status="ok", target_user_id=digiforms_user_id, response=external_response)
        write_db(db)
    return {"data": safe_digiforms_link(row, include_secret=True), "error": None}


@router.put("/digiforms/users/{link_id}")
async def update_digiforms_user(link_id: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        if not row:
            raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        current = dict(row)

    user_name = str(payload.get("user_name") or payload.get("digiforms_user_name") or current.get("digiforms_user_name") or "").strip()
    email = str(payload.get("email") or payload.get("digiforms_email") or current.get("digiforms_email") or "").strip()
    profile = normalize_digiforms_profile(payload.get("profile") or current.get("profile") or "user")
    password = str(payload.get("password") or "").strip()
    active = bool(payload.get("active", current.get("active", True)))
    if not user_name:
        raise HTTPException(status_code=400, detail="Debes indicar el nombre del usuario")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Debes indicar un correo válido")
    if not password:
        raise HTTPException(status_code=400, detail="Para actualizar un usuario por la API de DigiformsApp debes indicar la contraseña actual o una nueva contraseña. La API documentada requiere enviar el campo Password en el objeto User.")

    api = digiforms_api_or_error()
    api_payload = DigiformsUserPayload(
        user_id=str(current.get("digiforms_user_id")),
        client_id=int(current.get("digiforms_client_id") or settings.DIGIFORMS_CLIENT_ID),
        user_name=user_name,
        password=password,
        email=email,
        active=active,
        profile=profile,
    )
    try:
        external_response = await api.update_user(api_payload)
        external_status = "updated_in_digiforms"
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo actualizar el usuario: {exc}. {exc.response_text or ''}".strip())

    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        t = now()
        row.update({
            "digiforms_user_name": user_name,
            "digiforms_email": email,
            "profile": profile,
            "active": active,
            "external_status": external_status,
            "last_api_action": "update",
            "last_api_status": external_status,
            "last_api_response": external_response,
            "updated_at": t,
        })
        create_operation_log(db, user_id=user.get("id"), action="update_user", status="ok", target_user_id=row.get("digiforms_user_id"), response=external_response)
        write_db(db)
        data = safe_digiforms_link(row)
    return {"data": data, "error": None}


@router.post("/digiforms/users/{link_id}/verify")
async def verify_digiforms_user(link_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        if not row:
            raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        digiforms_user_id = row.get("digiforms_user_id")
    api = digiforms_api_or_error()
    try:
        external_response = await api.get_user(str(digiforms_user_id))
        external_status = "verified"
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo verificar el usuario en DigiformsApp: {exc}")
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        row.update({
            "external_status": external_status,
            "last_api_action": "verify",
            "last_api_status": external_status,
            "last_api_response": external_response,
            "updated_at": now(),
        })
        create_operation_log(db, user_id=user.get("id"), action="verify_user", status="ok", target_user_id=row.get("digiforms_user_id"), response=external_response)
        write_db(db)
        data = safe_digiforms_link(row)
    return {"data": data, "error": None}


@router.post("/digiforms/users/{link_id}/deactivate")
async def deactivate_digiforms_user(link_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        if not row:
            raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        digiforms_user_id = str(row.get("digiforms_user_id"))
        client_id = str(row.get("digiforms_client_id") or settings.DIGIFORMS_CLIENT_ID)
    api = digiforms_api_or_error()
    try:
        external_response = await api.deactivate_user(digiforms_user_id, client_id=client_id)
        external_status = "deactivated_in_digiforms"
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo desactivar el usuario: {exc}. {exc.response_text or ''}".strip())
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        row = find_digiforms_link(db, link_id, scope)
        t = now()
        row.update({
            "active": False,
            "external_status": external_status,
            "last_api_action": "deactivate",
            "last_api_status": external_status,
            "last_api_response": external_response,
            "deactivated_at": t,
            "updated_at": t,
        })
        create_operation_log(db, user_id=user.get("id"), action="deactivate_user", status="ok", target_user_id=digiforms_user_id, response=external_response)
        write_db(db)
        data = safe_digiforms_link(row)
    return {"data": data, "error": None}
