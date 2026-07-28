from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
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
from app.services.digiforms_company_config import (
    HARVEST_FORM_TYPE,
    PEST_WEED_FORM_TYPE,
    connection_for_company,
    encrypt_secret,
    encryption_key_is_explicit,
    mapping_for_company,
    mappings_for_company,
    runtime_credentials,
    safe_connection,
    safe_mappings,
)
from app.services.digiforms_data_api import DigiformsDataAPI, DigiformsDataAPIError
from app.services.digiforms_report_links import (
    find_form,
    forms_for_company,
    is_report_form_type,
    report_form_type_for,
    safe_form,
    safe_forms,
    suggest_field_map,
    template_fields,
)
from app.services.commercial_demo_seed import DEMO_COMPANY_ID, is_commercial_demo_user

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
            "client_id": (safe_connection(connection_for_company(db, company_id)) or {}).get("client_id"),
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
    """Enable an extension after administrative review.

    DigiForms tenant credentials are intentionally *not* provisioned here. Once
    the extension is enabled, the company administrator completes the secure
    connection wizard in Extensiones → DigiForms. This prevents one global
    DigiForms account from being reused accidentally across companies.
    """
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
        t = now()
        enable_extension_for(db, row.get("company_id"), row.get("requested_by_user_id"), extension_id)
        client_message = (
            "Tu extensión DigiformsApp ya fue habilitada. Ingresa a Extensiones → DigiForms para conectar la cuenta de tu empresa, asociar los FormId y activar la sincronización automática."
            if extension_id == DIGIFORMS_MODULE["id"]
            else f"Tu extensión {safe_extension_name(db, extension_id)} ya fue habilitada. Ahora puedes utilizarla desde Dataris."
        )
        row.update({
            "status": "approved",
            "admin_notes": (payload.get("admin_notes") or f"Extensión {safe_extension_name(db, extension_id)} revisada y habilitada desde el panel de administración.").strip(),
            "client_message": client_message,
            "reviewed_by_user_id": user["id"],
            "reviewed_at": t,
            "enabled_at": t,
            "updated_at": t,
        })
        if extension_id == DIGIFORMS_MODULE["id"]:
            account = {
                "id": str(uuid.uuid4()),
                "company_id": row.get("company_id"),
                "user_id": row.get("requested_by_user_id"),
                "extension_request_id": row.get("id"),
                "digiforms_client_id": None,
                "digiforms_user_id": None,
                "digiforms_user_name": None,
                "digiforms_email": row.get("requester_email"),
                "profile": settings.DIGIFORMS_DEFAULT_PROFILE,
                "mode": "company_configuration_required",
                "active": True,
                "api_status": "waiting_company_configuration",
                "api_response": None,
                "created_at": t,
                "updated_at": t,
            }
            table(db, "digiforms_accounts")[:] = [a for a in table(db, "digiforms_accounts") if a.get("extension_request_id") != row.get("id")]
            table(db, "digiforms_accounts").append(account)
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
        # El superadmin ve usuarios DigiformsApp de todas las empresas reales,
        # pero nunca los usuarios sintéticos de la demo comercial (se
        # identifican por demo_seed) para que no se mezclen con clientes reales.
        return not row.get("demo_seed")
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


def require_company_admin_scope(db: Dict[str, Any], user: Dict[str, Any], requested_company_id: Optional[str] = None) -> Dict[str, Any]:
    scope = current_link_scope(db, user)
    admin = scope.get("admin")
    if not is_admin(admin):
        raise HTTPException(status_code=403, detail="Solo un administrador puede configurar la conexión DigiForms de la empresa")
    if admin and admin.get("admin_role") == "company_admin":
        company_id = str(admin.get("company_id") or "")
    else:
        company_id = str(requested_company_id or scope.get("company_id") or (admin or {}).get("company_id") or "")
    if not company_id:
        raise HTTPException(status_code=400, detail="No se pudo determinar la empresa activa para configurar DigiForms")
    return {**scope, "company_id": company_id}


def _company_runtime_connection(db: Dict[str, Any], company_id: Optional[str]) -> Dict[str, str]:
    if str(company_id or "") == DEMO_COMPANY_ID:
        return {"client_id": "DEMO-CLIENT", "api_user": "demo-api-user", "api_password": "demo-api-password"}
    row = connection_for_company(db, company_id)
    if row:
        try:
            credentials = runtime_credentials(row)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if credentials.get("client_id") and credentials.get("api_user") and credentials.get("api_password"):
            return credentials
    # Backward-compatible fallback for deployments configured before the
    # multi-company assistant was added. New tenants should use the UI.
    return {
        "client_id": str(settings.DIGIFORMS_CLIENT_ID or "").strip(),
        "api_user": str(settings.DIGIFORMS_API_USER or "").strip(),
        "api_password": str(settings.DIGIFORMS_API_PASSWORD or ""),
    }


def digiforms_client_id_for_company(db: Dict[str, Any], company_id: Optional[str]) -> str:
    return _company_runtime_connection(db, company_id).get("client_id") or ""


def digiforms_api_or_error(db: Dict[str, Any], company_id: Optional[str]) -> DigiformsUserAPI:
    credentials = _company_runtime_connection(db, company_id)
    api = DigiformsUserAPI(
        client_id=credentials.get("client_id"),
        api_user=credentials.get("api_user"),
        api_password=credentials.get("api_password"),
    )
    if not api.is_configured:
        raise HTTPException(
            status_code=503,
            detail="DigiformsApp User API no está configurada para esta empresa. Completa ClientId, usuario técnico y contraseña desde Extensiones → DigiForms.",
        )
    if not settings.DIGIFORMS_PROVISIONING_ENABLED:
        raise HTTPException(status_code=503, detail="La provisión vía DigiformsApp User API está deshabilitada en el backend.")
    return api


def _default_initial_sync_dates() -> tuple[str, str]:
    current_year = date.today().year
    return f"{current_year}-01-01", f"{current_year}-12-31"


def _clean_iso_date(value: Any, fallback: str) -> str:
    raw = str(value or fallback).strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Las fechas iniciales DigiForms deben usar formato AAAA-MM-DD.") from exc


def _upsert_form_mapping(
    db: Dict[str, Any],
    *,
    company_id: str,
    form_type: str,
    form_id: Any,
    display_name: str,
    initial_response_id: int,
    initial_sync_start_date: str,
    initial_sync_end_date: str,
    timestamp: str,
) -> Dict[str, Any]:
    rows = table(db, "digiforms_form_mappings")
    row = mapping_for_company(db, company_id, form_type)
    values = {
        "company_id": company_id,
        "form_type": form_type,
        "display_name": display_name,
        "form_id": str(form_id or "").strip(),
        "is_enabled": bool(str(form_id or "").strip()),
        "initial_response_id": int(initial_response_id or 0),
        "initial_sync_start_date": initial_sync_start_date,
        "initial_sync_end_date": initial_sync_end_date,
        "updated_at": timestamp,
    }
    if row:
        row.update(values)
        return row
    row = {"id": str(uuid.uuid4()), "created_at": timestamp, **values}
    rows.append(row)
    return row


def _reset_company_cursor(db: Dict[str, Any], *, company_id: str, form_type: str, form_id: str, initial_response_id: int, timestamp: str) -> None:
    rows = table(db, "sig_sync_cursors")
    row = next((item for item in rows if str(item.get("company_id") or "") == company_id and item.get("form_type") == form_type), None)
    values = {
        "company_id": company_id,
        "form_type": form_type,
        "form_id": form_id,
        "last_response_id": int(initial_response_id or 0),
        "last_sync_at": None,
        "last_success_at": None,
        "last_error": None,
        "updated_at": timestamp,
    }
    if row:
        row.update(values)
    else:
        rows.append({"id": str(uuid.uuid4()), "created_at": timestamp, **values})


@router.get("/digiforms/company-config")
def get_digiforms_company_config(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = scope.get("company_id")
        connection = connection_for_company(db, company_id)
        mappings = safe_mappings(mappings_for_company(db, company_id))
        can_edit = is_admin(scope.get("admin"))
    return {
        "data": {
            "company_id": company_id,
            "can_edit": can_edit,
            "connection": safe_connection(connection),
            "mappings": mappings,
            "provider": {
                "user_api_base_url": settings.DIGIFORMS_BASE_URL,
                "data_api_base_url": settings.DIGIFORMS_DATA_BASE_URL,
                "portal_url": settings.DIGIFORMS_PORTAL_URL,
            },
            "encryption_key_is_explicit": encryption_key_is_explicit(),
        },
        "error": None,
    }


@router.put("/digiforms/company-config")
def save_digiforms_company_config(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        rows = table(db, "digiforms_connections")
        row = connection_for_company(db, company_id)
        client_id = str(payload.get("client_id") or (row or {}).get("client_id") or "").strip()
        api_user = str(payload.get("api_user") or (row or {}).get("api_user") or "").strip()
        password = str(payload.get("api_password") or "")
        encrypted_password = (row or {}).get("encrypted_api_password")
        if password:
            encrypted_password = encrypt_secret(password)
        if not client_id or not api_user or not encrypted_password:
            raise HTTPException(status_code=400, detail="ClientId, usuario técnico y contraseña son obligatorios para conectar DigiForms")
        timestamp = now()
        values = {
            "company_id": company_id,
            "client_id": client_id,
            "api_user": api_user,
            "encrypted_api_password": encrypted_password,
            "auto_sync_enabled": payload.get("auto_sync_enabled", (row or {}).get("auto_sync_enabled", True)) is not False,
            "connection_status": (row or {}).get("connection_status") or "not_tested",
            "configured_by_user_id": user.get("id"),
            "updated_at": timestamp,
        }
        if row:
            row.update(values)
        else:
            row = {"id": str(uuid.uuid4()), "created_at": timestamp, **values}
            rows.append(row)

        default_initial = int(payload.get("initial_response_id") or 0)
        harvest_initial = int(payload.get("harvest_initial_response_id") or default_initial)
        pest_initial = int(payload.get("pest_weed_initial_response_id") or default_initial)
        default_start, default_end = _default_initial_sync_dates()
        previous_harvest = mapping_for_company(db, company_id, HARVEST_FORM_TYPE) or {}
        previous_pest = mapping_for_company(db, company_id, PEST_WEED_FORM_TYPE) or {}
        previous_harvest_id = str(previous_harvest.get("form_id") or "")
        previous_pest_id = str(previous_pest.get("form_id") or "")
        harvest_start = _clean_iso_date(payload.get("harvest_initial_sync_start_date"), str(previous_harvest.get("initial_sync_start_date") or default_start))
        harvest_end = _clean_iso_date(payload.get("harvest_initial_sync_end_date"), str(previous_harvest.get("initial_sync_end_date") or default_end))
        pest_start = _clean_iso_date(payload.get("pest_weed_initial_sync_start_date"), str(previous_pest.get("initial_sync_start_date") or default_start))
        pest_end = _clean_iso_date(payload.get("pest_weed_initial_sync_end_date"), str(previous_pest.get("initial_sync_end_date") or default_end))
        harvest = _upsert_form_mapping(db, company_id=company_id, form_type=HARVEST_FORM_TYPE, form_id=payload.get("harvest_form_id"), display_name="Monitoreo de cosecha", initial_response_id=harvest_initial, initial_sync_start_date=harvest_start, initial_sync_end_date=harvest_end, timestamp=timestamp)
        pest = _upsert_form_mapping(db, company_id=company_id, form_type=PEST_WEED_FORM_TYPE, form_id=payload.get("pest_weed_form_id"), display_name="Malezas y plagas", initial_response_id=pest_initial, initial_sync_start_date=pest_start, initial_sync_end_date=pest_end, timestamp=timestamp)
        reset_cursors = payload.get("reset_cursors") is True
        cursor_rows = table(db, "sig_sync_cursors")
        for form_type, mapping, previous_id, initial in [
            (HARVEST_FORM_TYPE, harvest, previous_harvest_id, harvest_initial),
            (PEST_WEED_FORM_TYPE, pest, previous_pest_id, pest_initial),
        ]:
            current_id = str(mapping.get("form_id") or "")
            existing_cursor = next((item for item in cursor_rows if str(item.get("company_id") or "") == company_id and item.get("form_type") == form_type), None)
            # Preserve progress on routine credential edits. A cursor is reset
            # only for first setup, an explicit operator request, or a changed
            # FormId that points to another DigiForms format.
            if reset_cursors or existing_cursor is None or previous_id != current_id:
                _reset_company_cursor(db, company_id=company_id, form_type=form_type, form_id=current_id, initial_response_id=initial, timestamp=timestamp)
        write_db(db)
    return {"data": {"connection": safe_connection(row), "mappings": safe_mappings([harvest, pest])}, "error": None, "message": "Configuración DigiForms guardada para la empresa."}


@router.post("/digiforms/company-config/test")
async def test_digiforms_company_config(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    if is_commercial_demo_user(user):
        return {"data": {"client_id": "DEMO-CLIENT", "api_user": "demo-api-user", "user_api": {"ok": True, "response": {"mode": "commercial_demo"}}, "data_api": [{"form_type": "harvest", "form_id": "DEMO-HARVEST-01", "ok": True, "records_received": 18}, {"form_type": "pest_weed", "form_id": "DEMO-PEST-01", "ok": True, "records_received": 4}], "data_api_ok": True, "ok": True, "demo": True}, "error": None}
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        stored = connection_for_company(db, company_id) or {}
        credentials = _company_runtime_connection(db, company_id)
        client_id = str(payload.get("client_id") or credentials.get("client_id") or "").strip()
        api_user = str(payload.get("api_user") or credentials.get("api_user") or "").strip()
        api_password = str(payload.get("api_password") or credentials.get("api_password") or "")
        harvest_mapping = mapping_for_company(db, company_id, HARVEST_FORM_TYPE) or {}
        pest_mapping = mapping_for_company(db, company_id, PEST_WEED_FORM_TYPE) or {}
        default_start, default_end = _default_initial_sync_dates()
        harvest_form_id = str(payload.get("harvest_form_id") or harvest_mapping.get("form_id") or "").strip()
        pest_form_id = str(payload.get("pest_weed_form_id") or pest_mapping.get("form_id") or "").strip()
        harvest_start = _clean_iso_date(payload.get("harvest_initial_sync_start_date"), str(harvest_mapping.get("initial_sync_start_date") or default_start))
        harvest_end = _clean_iso_date(payload.get("harvest_initial_sync_end_date"), str(harvest_mapping.get("initial_sync_end_date") or default_end))
        pest_start = _clean_iso_date(payload.get("pest_weed_initial_sync_start_date"), str(pest_mapping.get("initial_sync_start_date") or default_start))
        pest_end = _clean_iso_date(payload.get("pest_weed_initial_sync_end_date"), str(pest_mapping.get("initial_sync_end_date") or default_end))
    if not client_id or not api_user or not api_password:
        raise HTTPException(status_code=400, detail="Completa ClientId, usuario técnico y contraseña antes de probar la conexión")
    result: Dict[str, Any] = {"client_id": client_id, "api_user": api_user, "user_api": {"ok": False}, "data_api": []}
    user_api = DigiformsUserAPI(client_id=client_id, api_user=api_user, api_password=api_password)
    try:
        response = await user_api.get_user(api_user)
        result["user_api"] = {"ok": True, "response": response}
    except DigiformsAPIError as exc:
        result["user_api"] = {"ok": False, "message": str(exc), "status_code": exc.status_code}
    data_api = DigiformsDataAPI(client_id=client_id, api_user=api_user, api_password=api_password)
    for form_type, form_id, initial, start_date, end_date in [
        (HARVEST_FORM_TYPE, harvest_form_id, int(payload.get("harvest_initial_response_id") or 0), harvest_start, harvest_end),
        (PEST_WEED_FORM_TYPE, pest_form_id, int(payload.get("pest_weed_initial_response_id") or 0), pest_start, pest_end),
    ]:
        if not form_id:
            continue
        try:
            probe = await data_api.test_results_connection(form_id, initial, start_date=start_date, end_date=end_date)
            result["data_api"].append({"form_type": form_type, **probe})
        except DigiformsDataAPIError as exc:
            result["data_api"].append({"form_type": form_type, "form_id": form_id, "ok": False, "message": str(exc), "status_code": exc.status_code})
    data_api_ok = any(item.get("ok") for item in result["data_api"])
    result["data_api_ok"] = data_api_ok
    result["ok"] = bool(data_api_ok if result["data_api"] else result["user_api"].get("ok"))
    if payload.get("persist_test_result"):
        with LOCK:
            db = read_db()
            row = connection_for_company(db, company_id)
            if row:
                row.update({"connection_status": "ok" if result["ok"] else "error", "last_connection_test_at": now(), "last_connection_error": None if result["ok"] else "La Data API no validó los FormId internos configurados.", "updated_at": now()})
                write_db(db)
    return {"data": result, "error": None}


@router.get("/digiforms/connection-test")
async def test_digiforms_connection(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    if is_commercial_demo_user(user):
        return {"data": {"configured": True, "client_id": "DEMO-CLIENT", "api_user": "demo-api-user", "base_url": settings.DIGIFORMS_BASE_URL, "portal_url": settings.DIGIFORMS_PORTAL_URL, "provisioning_enabled": True, "external_ok": True, "external_status": "demo_ready", "source": "commercial_demo_dataset", "message": "Conexión simulada lista para demostración."}, "error": None}
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = scope.get("company_id")
        credentials = _company_runtime_connection(db, company_id)
        connection = connection_for_company(db, company_id)
    api = DigiformsUserAPI(
        client_id=credentials.get("client_id"),
        api_user=credentials.get("api_user"),
        api_password=credentials.get("api_password"),
    )
    configured = api.is_configured
    payload: Dict[str, Any] = {
        "configured": configured,
        "client_id": credentials.get("client_id"),
        "api_user": credentials.get("api_user"),
        "base_url": settings.DIGIFORMS_BASE_URL,
        "portal_url": settings.DIGIFORMS_PORTAL_URL,
        "provisioning_enabled": settings.DIGIFORMS_PROVISIONING_ENABLED,
        "external_ok": False,
        "external_status": "not_configured" if not configured else "not_tested",
        "source": "company_config" if connection else "legacy_backend_env",
    }
    if not configured or not settings.DIGIFORMS_PROVISIONING_ENABLED:
        return {"data": payload, "error": None}
    try:
        response = await api.get_user(str(credentials.get("api_user") or ""))
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
        scope = current_link_scope(db, user)
        api = digiforms_api_or_error(db, scope.get("company_id"))
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
        client_id = digiforms_client_id_for_company(db, company_id)
        duplicate = next((r for r in table(db, "digiforms_user_links") if str(r.get("digiforms_client_id") or "") == client_id and r.get("digiforms_user_id") == digiforms_user_id and (not company_id or r.get("company_id") == company_id)), None)
        if duplicate:
            return {"data": safe_digiforms_link(duplicate), "error": None, "message": "El usuario ya estaba vinculado en Dataris."}
        api = digiforms_api_or_error(db, company_id)
    try:
        external_response = await api.get_user(digiforms_user_id)
    except DigiformsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo validar el usuario existente: {exc}")
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = str(payload.get("company_id") or scope.get("company_id") or "") or None
        if scope.get("admin") and scope["admin"].get("admin_role") == "company_admin":
            company_id = scope["admin"].get("company_id")
        client_id = digiforms_client_id_for_company(db, company_id)
        t = now()
        row = {
            "id": str(uuid.uuid4()), "company_id": company_id, "dataris_user_id": payload.get("dataris_user_id") or user.get("id"), "created_by_user_id": user.get("id"),
            "digiforms_client_id": client_id, "digiforms_user_id": digiforms_user_id, "digiforms_user_name": str(payload.get("user_name") or payload.get("digiforms_user_name") or "").strip() or digiforms_user_id,
            "digiforms_email": str(payload.get("email") or "").strip() or None, "profile": normalize_digiforms_profile(payload.get("profile") or "user"), "mode": "existing", "active": True,
            "external_status": "verified_existing", "last_api_action": "lookup", "last_api_status": "verified_existing", "last_api_response": external_response, "created_at": t, "updated_at": t,
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
        client_id = digiforms_client_id_for_company(db, company_id)
        duplicate = next((r for r in table(db, "digiforms_user_links") if str(r.get("digiforms_client_id") or "") == client_id and r.get("digiforms_user_id") == digiforms_user_id and (not company_id or r.get("company_id") == company_id)), None)
        if duplicate:
            raise HTTPException(status_code=409, detail="Ya existe un usuario DigiformsApp vinculado con ese UserId")
        api = digiforms_api_or_error(db, company_id)
    api_payload = DigiformsUserPayload(user_id=digiforms_user_id, client_id=int(client_id), user_name=user_name, password=password, email=email, active=True, profile=profile)
    try:
        external_response = await api.create_user(api_payload)
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
            "id": str(uuid.uuid4()), "company_id": company_id, "dataris_user_id": payload.get("dataris_user_id") or user.get("id"), "created_by_user_id": user.get("id"), "digiforms_client_id": client_id,
            "digiforms_user_id": digiforms_user_id, "digiforms_user_name": user_name, "digiforms_email": email, "profile": profile, "mode": "generated", "active": True,
            "external_status": "created_in_digiforms", "last_api_action": "create", "last_api_status": "created_in_digiforms", "last_api_response": external_response,
            "initial_password": password if generated_password else None, "temporary_password_was_generated": generated_password, "created_at": t, "updated_at": t,
        }
        table(db, "digiforms_user_links").append(row)
        create_operation_log(db, user_id=user.get("id"), action="create_user", status="ok", target_user_id=digiforms_user_id, response=external_response)
        write_db(db)
    return {"data": safe_digiforms_link(row, include_secret=True), "error": None}


@router.put("/digiforms/users/{link_id}")
async def update_digiforms_user(link_id: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope)
        if not row: raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        current = dict(row); api = digiforms_api_or_error(db, current.get("company_id"))
    user_name = str(payload.get("user_name") or payload.get("digiforms_user_name") or current.get("digiforms_user_name") or "").strip()
    email = str(payload.get("email") or payload.get("digiforms_email") or current.get("digiforms_email") or "").strip()
    profile = normalize_digiforms_profile(payload.get("profile") or current.get("profile") or "user")
    password = str(payload.get("password") or "").strip(); active = bool(payload.get("active", current.get("active", True)))
    if not user_name: raise HTTPException(status_code=400, detail="Debes indicar el nombre del usuario")
    if not email or "@" not in email: raise HTTPException(status_code=400, detail="Debes indicar un correo válido")
    if not password: raise HTTPException(status_code=400, detail="Para actualizar un usuario por la API de DigiformsApp debes indicar la contraseña actual o una nueva contraseña. La API documentada requiere enviar el campo Password en el objeto User.")
    api_payload = DigiformsUserPayload(user_id=str(current.get("digiforms_user_id")), client_id=int(current.get("digiforms_client_id") or api.client_id), user_name=user_name, password=password, email=email, active=active, profile=profile)
    try: external_response = await api.update_user(api_payload)
    except DigiformsAPIError as exc: raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo actualizar el usuario: {exc}. {exc.response_text or ''}".strip())
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope); t = now()
        row.update({"digiforms_user_name": user_name, "digiforms_email": email, "profile": profile, "active": active, "external_status": "updated_in_digiforms", "last_api_action": "update", "last_api_status": "updated_in_digiforms", "last_api_response": external_response, "updated_at": t})
        create_operation_log(db, user_id=user.get("id"), action="update_user", status="ok", target_user_id=row.get("digiforms_user_id"), response=external_response); write_db(db); data = safe_digiforms_link(row)
    return {"data": data, "error": None}


@router.post("/digiforms/users/{link_id}/verify")
async def verify_digiforms_user(link_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope)
        if not row: raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        digiforms_user_id = str(row.get("digiforms_user_id")); api = digiforms_api_or_error(db, row.get("company_id"))
    try: external_response = await api.get_user(digiforms_user_id)
    except DigiformsAPIError as exc: raise HTTPException(status_code=502, detail=f"No se pudo verificar el usuario en DigiformsApp: {exc}")
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope)
        row.update({"external_status": "verified", "last_api_action": "verify", "last_api_status": "verified", "last_api_response": external_response, "updated_at": now()})
        create_operation_log(db, user_id=user.get("id"), action="verify_user", status="ok", target_user_id=row.get("digiforms_user_id"), response=external_response); write_db(db); data = safe_digiforms_link(row)
    return {"data": data, "error": None}


@router.post("/digiforms/users/{link_id}/deactivate")
async def deactivate_digiforms_user(link_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope)
        if not row: raise HTTPException(status_code=404, detail="Usuario DigiformsApp no encontrado")
        digiforms_user_id = str(row.get("digiforms_user_id")); client_id = str(row.get("digiforms_client_id") or ""); api = digiforms_api_or_error(db, row.get("company_id"))
    try: external_response = await api.deactivate_user(digiforms_user_id, client_id=client_id)
    except DigiformsAPIError as exc: raise HTTPException(status_code=502, detail=f"DigiformsApp no pudo desactivar el usuario: {exc}. {exc.response_text or ''}".strip())
    with LOCK:
        db = read_db(); scope = current_link_scope(db, user); row = find_digiforms_link(db, link_id, scope); t = now()
        row.update({"active": False, "external_status": "deactivated_in_digiforms", "last_api_action": "deactivate", "last_api_status": "deactivated_in_digiforms", "last_api_response": external_response, "deactivated_at": t, "updated_at": t})
        create_operation_log(db, user_id=user.get("id"), action="deactivate_user", status="ok", target_user_id=digiforms_user_id, response=external_response); write_db(db); data = safe_digiforms_link(row)
    return {"data": data, "error": None}


# ---------------------------------------------------------------------------
# Catálogo de formularios de AgtechApps
#
# El proveedor no expone ninguna ruta para listar los formularios de un cliente
# (su Data API se limita a `results/GetAll` e `images`), así que el listado se
# mantiene aquí. Registrar un formulario una vez, con nombre legible, es lo que
# permite elegirlo después en un desplegable en lugar de pegar el FormId interno
# en cada pantalla.
# ---------------------------------------------------------------------------


def _visible_report_templates(db: Dict[str, Any], company_id: Optional[str]) -> List[Dict[str, Any]]:
    """Plantillas que la empresa puede enlazar: las suyas y las del sistema."""
    rows = table(db, "report_templates")
    visible = [row for row in rows if not row.get("company_id") or str(row.get("company_id")) == str(company_id or "")]
    latest: Dict[str, Dict[str, Any]] = {}
    for template in visible:
        key = str(template.get("key") or "")
        current = latest.get(key)
        if not current or int(template.get("version") or 0) > int(current.get("version") or 0):
            latest[key] = template
    return sorted(latest.values(), key=lambda item: str(item.get("name") or ""))


def _template_by_id(db: Dict[str, Any], template_id: str, company_id: Optional[str]) -> Dict[str, Any]:
    template = next((row for row in table(db, "report_templates") if str(row.get("id")) == str(template_id)), None)
    if not template:
        raise HTTPException(status_code=404, detail="La plantilla de reporte no existe")
    if template.get("company_id") and str(template.get("company_id")) != str(company_id or ""):
        raise HTTPException(status_code=403, detail="Esa plantilla pertenece a otra empresa")
    return template


@router.get("/digiforms/forms")
def list_digiforms_forms(authorization: Optional[str] = Header(default=None)):
    """Catálogo de formularios de AgtechApps y plantillas enlazables."""
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = scope.get("company_id")
        forms = safe_forms(forms_for_company(db, company_id))
        templates = [
            {"id": row.get("id"), "key": row.get("key"), "name": row.get("name"), "version": row.get("version")}
            for row in _visible_report_templates(db, company_id)
        ]
        can_edit = is_admin(scope.get("admin"))
    return {"data": {"forms": forms, "report_templates": templates, "can_edit": can_edit}, "error": None}


@router.post("/digiforms/forms")
def upsert_digiforms_form(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Registra un formulario de AgtechApps en el catálogo de la empresa."""
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        form_id = str(payload.get("form_id") or "").strip()
        if not form_id:
            raise HTTPException(status_code=400, detail="Indica el FormId interno del formulario en AgtechApps")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Ponle un nombre al formulario para poder reconocerlo")
        timestamp = now()
        row = find_form(db, company_id, form_id)
        values = {
            "company_id": company_id,
            "form_id": form_id,
            "name": name,
            "description": str(payload.get("description") or "").strip(),
            "updated_at": timestamp,
        }
        if row:
            row.update(values)
        else:
            row = {"id": str(uuid.uuid4()), "created_at": timestamp, "discovered_fields": [], "verification_status": "not_tested", **values}
            table(db, "digiforms_forms").append(row)
        write_db(db)
        data = safe_form(row)
    return {"data": data, "error": None}


@router.post("/digiforms/forms/import")
async def import_digiforms_forms(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Trae el listado de formularios directamente desde AgtechApps.

    Evita tener que copiar identificadores a mano: el proveedor publica los
    formularios del cliente en `GET api/form/{clientId}`. Los que ya estaban en
    el catálogo se actualizan conservando lo que Dataris sabe de ellos (las
    preguntas descubiertas y su verificación); los registrados a mano que no
    aparecen en el listado se respetan, porque pueden ser de otro cliente o
    haberse creado antes de que existiera este endpoint.
    """
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        credentials = _company_runtime_connection(db, company_id)

    if not credentials.get("client_id") or not credentials.get("api_user") or not credentials.get("api_password"):
        raise HTTPException(status_code=400, detail="Configura primero ClientId, usuario técnico y contraseña de AgtechApps")

    api = DigiformsDataAPI(
        client_id=credentials.get("client_id"),
        api_user=credentials.get("api_user"),
        api_password=credentials.get("api_password"),
    )
    try:
        remote_forms = await api.get_forms()
    except DigiformsDataAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    created = 0
    updated = 0
    with LOCK:
        db = read_db()
        rows = table(db, "digiforms_forms")
        timestamp = now()
        for remote in remote_forms:
            existing = find_form(db, company_id, remote["form_id"])
            values = {
                "name": remote["name"],
                "description": remote["description"],
                "category": remote["category"],
                "provider_status": remote["status"],
                "reference_id": remote["reference_id"],
                "valid_from": remote["valid_from"],
                "valid_to": remote["valid_to"],
                "is_public": remote["is_public"],
                "source": "agtechapps_catalog",
                "updated_at": timestamp,
            }
            if existing:
                # El nombre puesto a mano no se pisa si el proveedor no trae uno
                # mejor: alguien pudo renombrarlo para reconocerlo.
                if not remote["name"] or remote["name"] == remote["form_id"]:
                    values.pop("name")
                existing.update(values)
                updated += 1
            else:
                rows.append({
                    "id": str(uuid.uuid4()),
                    "company_id": company_id,
                    "form_id": remote["form_id"],
                    "discovered_fields": [],
                    "verification_status": "not_tested",
                    "created_at": timestamp,
                    **values,
                })
                created += 1
        write_db(db)
        forms = safe_forms(forms_for_company(db, company_id))

    return {
        "data": {"forms": forms, "created": created, "updated": updated, "received": len(remote_forms)},
        "error": None,
        "message": f"{len(remote_forms)} formularios recibidos de AgtechApps ({created} nuevos).",
    }


@router.delete("/digiforms/forms/{row_id}")
def delete_digiforms_form(row_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user)
        company_id = str(scope.get("company_id") or "")
        rows = table(db, "digiforms_forms")
        row = next((item for item in rows if str(item.get("id")) == row_id and str(item.get("company_id") or "") == company_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="Ese formulario no está en el catálogo")
        # Un formulario enlazado no se borra a la ligera: dejaría el vínculo
        # apuntando a un formulario que ya no se puede elegir en la interfaz.
        linked = next(
            (item for item in mappings_for_company(db, company_id)
             if str(item.get("form_id") or "") == str(row.get("form_id") or "") and item.get("report_template_id")),
            None,
        )
        if linked:
            raise HTTPException(status_code=400, detail="Primero quita el vínculo de este formulario con su plantilla de reporte")
        rows.remove(row)
        write_db(db)
    return {"data": {"ok": True}, "error": None}


@router.post("/digiforms/forms/verify")
async def verify_digiforms_form(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Consulta el formulario en AgtechApps y guarda los campos que descubre.

    Es el paso que convierte un FormId opaco en algo utilizable: la Data API
    devuelve las preguntas reales del formato, y con ellas se puede proponer el
    mapeo contra una plantilla sin que nadie los teclee.
    """
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        form_id = str(payload.get("form_id") or "").strip()
        if not form_id:
            raise HTTPException(status_code=400, detail="Indica el formulario que quieres comprobar")
        credentials = _company_runtime_connection(db, company_id)
        default_start, default_end = _default_initial_sync_dates()
        start_date = _clean_iso_date(payload.get("start_date"), default_start)
        end_date = _clean_iso_date(payload.get("end_date"), default_end)

    if not credentials.get("client_id") or not credentials.get("api_user") or not credentials.get("api_password"):
        raise HTTPException(status_code=400, detail="Configura primero ClientId, usuario técnico y contraseña de AgtechApps")

    api = DigiformsDataAPI(
        client_id=credentials.get("client_id"),
        api_user=credentials.get("api_user"),
        api_password=credentials.get("api_password"),
    )
    try:
        probe = await api.test_results_connection(form_id, 0, start_date=start_date, end_date=end_date)
        error_message = None
    except DigiformsDataAPIError as exc:
        probe = {"ok": False, "records_received": 0, "discovered_fields": []}
        error_message = str(exc)

    with LOCK:
        db = read_db()
        row = find_form(db, company_id, form_id)
        timestamp = now()
        if row is not None:
            row.update({
                "discovered_fields": list(probe.get("discovered_fields") or []) or list(row.get("discovered_fields") or []),
                "last_verified_at": timestamp,
                "last_records_seen": int(probe.get("records_received") or 0),
                "verification_status": "ok" if not error_message else "error",
                "verification_error": error_message,
                "updated_at": timestamp,
            })
            write_db(db)
            data = safe_form(row)
        else:
            data = {"form_id": form_id, "discovered_fields": list(probe.get("discovered_fields") or [])}
    return {
        "data": {**data, "ok": not error_message, "records_received": int(probe.get("records_received") or 0), "message": error_message},
        "error": None,
    }


# ---------------------------------------------------------------------------
# Vínculos formulario de AgtechApps ↔ plantilla de Reportes de Campo
# ---------------------------------------------------------------------------


@router.get("/digiforms/report-links")
def list_report_links(authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = current_link_scope(db, user)
        company_id = scope.get("company_id")
        links = [
            safe_mappings([row])[0]
            for row in mappings_for_company(db, company_id)
            if is_report_form_type(row.get("form_type"))
        ]
        forms = {str(item.get("form_id")): item for item in forms_for_company(db, company_id)}
        for link in links:
            catalog_entry = forms.get(str(link.get("form_id") or ""))
            link["form_name"] = (catalog_entry or {}).get("name") or link.get("form_id")
            link["discovered_fields"] = list((catalog_entry or {}).get("discovered_fields") or [])
        can_edit = is_admin(scope.get("admin"))
    return {"data": {"links": links, "can_edit": can_edit}, "error": None}


@router.post("/digiforms/report-links/suggest")
def suggest_report_link_mapping(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Propone el mapeo de campos entre un formulario y una plantilla.

    No guarda nada: la interfaz enseña la propuesta para que un admin la revise
    antes de confirmarla.
    """
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user)
        company_id = str(scope.get("company_id") or "")
        form_id = str(payload.get("form_id") or "").strip()
        template = _template_by_id(db, str(payload.get("report_template_id") or ""), company_id)
        catalog_entry = find_form(db, company_id, form_id)
        if not catalog_entry:
            raise HTTPException(status_code=404, detail="Ese formulario no está en el catálogo de la empresa")
        discovered = list(payload.get("discovered_fields") or catalog_entry.get("discovered_fields") or [])
        schema = template.get("schema") or {}
        suggestion = suggest_field_map(schema, discovered)
        fields = template_fields(schema)
    return {
        "data": {
            "field_map": suggestion,
            "template_fields": fields,
            "discovered_fields": discovered,
            "unmapped_template_fields": [f["key"] for f in fields if f.get("kind") != "table" and f["key"] not in suggestion],
            "unused_api_fields": [name for name in discovered if name not in suggestion.values()],
        },
        "error": None,
    }


@router.post("/digiforms/report-links")
def save_report_link(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Enlaza un formulario de AgtechApps con una plantilla de Reportes."""
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user, str(payload.get("company_id") or "") or None)
        company_id = str(scope.get("company_id") or "")
        form_id = str(payload.get("form_id") or "").strip()
        if not form_id:
            raise HTTPException(status_code=400, detail="Elige el formulario de AgtechApps")
        catalog_entry = find_form(db, company_id, form_id)
        if not catalog_entry:
            raise HTTPException(status_code=404, detail="Ese formulario no está en el catálogo de la empresa")
        template = _template_by_id(db, str(payload.get("report_template_id") or ""), company_id)

        field_map = payload.get("field_map")
        if not isinstance(field_map, dict) or not field_map:
            # Sin mapeo explícito se propone uno: enlazar y que no llegue nada
            # sería la peor combinación posible.
            field_map = suggest_field_map(template.get("schema") or {}, catalog_entry.get("discovered_fields") or [])
        field_map = {str(key): str(value) for key, value in field_map.items() if str(key) and str(value)}

        form_type = report_form_type_for(form_id)
        default_start, default_end = _default_initial_sync_dates()
        previous = mapping_for_company(db, company_id, form_type) or {}
        initial_response_id = int(payload.get("initial_response_id") or previous.get("initial_response_id") or 0)
        start_date = _clean_iso_date(payload.get("initial_sync_start_date"), str(previous.get("initial_sync_start_date") or default_start))
        end_date = _clean_iso_date(payload.get("initial_sync_end_date"), str(previous.get("initial_sync_end_date") or default_end))
        timestamp = now()

        mapping = _upsert_form_mapping(
            db,
            company_id=company_id,
            form_type=form_type,
            form_id=form_id,
            display_name=str(payload.get("display_name") or catalog_entry.get("name") or template.get("name") or "Reporte"),
            initial_response_id=initial_response_id,
            initial_sync_start_date=start_date,
            initial_sync_end_date=end_date,
            timestamp=timestamp,
        )
        mapping.update({
            "report_template_id": template.get("id"),
            "report_template_key": template.get("key"),
            "field_map": field_map,
            "is_enabled": payload.get("is_enabled", True) is not False,
        })

        # Un vínculo nuevo, o uno que cambia de plantilla, arranca su cursor
        # desde el principio para no perderse respuestas ya existentes.
        if not previous or previous.get("report_template_id") != template.get("id") or payload.get("reset_cursor") is True:
            _reset_company_cursor(
                db,
                company_id=company_id,
                form_type=form_type,
                form_id=form_id,
                initial_response_id=initial_response_id,
                timestamp=timestamp,
            )
        write_db(db)
        data = safe_mappings([mapping])[0]
    return {"data": data, "error": None, "message": "Formulario enlazado con la plantilla de reporte."}


@router.delete("/digiforms/report-links/{form_id}")
def delete_report_link(form_id: str, authorization: Optional[str] = Header(default=None)):
    user = require_user(authorization)
    with LOCK:
        db = read_db()
        scope = require_company_admin_scope(db, user)
        company_id = str(scope.get("company_id") or "")
        form_type = report_form_type_for(form_id)
        rows = table(db, "digiforms_form_mappings")
        row = next(
            (item for item in rows
             if str(item.get("company_id") or "") == company_id and str(item.get("form_type") or "") == form_type),
            None,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Ese formulario no está enlazado")
        rows.remove(row)
        # El cursor se va con el vínculo: si mañana se vuelve a enlazar, conviene
        # que reimporte desde el inicio en lugar de heredar una posición vieja.
        cursors = table(db, "sig_sync_cursors")
        for cursor in [item for item in cursors if str(item.get("company_id") or "") == company_id and item.get("form_type") == form_type]:
            cursors.remove(cursor)
        write_db(db)
    return {"data": {"ok": True}, "error": None}
