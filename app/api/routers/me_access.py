from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException

from app.api.routers.compat import LOCK, bearer_user, read_db, table, write_db, now
from app.services import module_access, module_catalog
from app.services.commercial_demo_seed import is_commercial_demo_user
from app.api.routers.compat_extensions import (
    ensure_extension_catalog,
    extension_enabled_for,
    normalize_extension_id,
    company_for_user,
    profile_for,
    admin_record_for,
)

router = APIRouter(prefix="/me", tags=["Current User Access"])


def _is_active(row: Dict[str, Any]) -> bool:
    return module_access.row_is_enabled(row)


def _normalize_module_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ñ": "n",
    }
    for src, dst in replacements.items():
        raw = raw.replace(src, dst)
    return raw.replace("_", "-").replace(" ", "-")


MODULE_ALIASES: Dict[str, List[str]] = {
    "sig-agricola": ["sig-agricola", "sig_agricola", "sig"],
    "aplicaciones-aereas": ["aplicaciones-aereas", "aplicaciones_aereas", "drones", "drone", "helicoptero", "helicopter", "avioneta"],
    "ortofoto-analysis": ["ortofoto-analysis", "ortofoto_analysis", "ortofotos", "analisis-ortofotos", "analisis_de_ortofotos"],
    # Graniot se unificó con Monitoreo Satelital: era la misma capacidad.
    "satelite": ["satelite", "satellite", "satélite", "graniot"],
    "telemetria": ["telemetria", "telemetría", "telemetry"],
    "digiforms": ["digiforms", "digiformsapp", "digiforms-app"],
    "ml-training": ["ml-training", "ml_training", "laboratorio-ia", "laboratorio_ia"],
}

# "Laboratorio de IA" es una herramienta interna de Dataris (entrena modelos
# con datos propios de la plataforma) — nunca debe quedar disponible para
# empresas cliente ni para la cuenta demo, sin importar cómo esté configurado
# platform_modules/company_modules/user_modules. Se excluye explícitamente
# de todo cálculo de acceso salvo para el superadmin real de la plataforma.
# La lista sale del catálogo del producto (app/services/module_catalog.py).
INTERNAL_ONLY_MODULE_IDS = set(module_catalog.INTERNAL_ONLY_MODULE_IDS)


def _expand_aliases(value: Any) -> List[str]:
    normalized = _normalize_module_id(value)
    if not normalized:
        return []
    out = {normalized}
    for canonical, values in MODULE_ALIASES.items():
        normalized_values = {_normalize_module_id(v) for v in values}
        if normalized == canonical or normalized in normalized_values:
            out.add(canonical)
            out.update(normalized_values)
    return [v for v in out if v]


def _unique(values: List[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        for item in _expand_aliases(value):
            if item and item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _latest_request_for(db: Dict[str, Any], user_id: str, company_id: Optional[str], extension_id: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_extension_id(extension_id)
    rows = [
        row
        for row in table(db, "extension_requests")
        if normalize_extension_id(row.get("extension_id")) == normalized
        and (
            row.get("requested_by_user_id") == user_id
            or (company_id and row.get("company_id") == company_id)
        )
    ]
    rows.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
    if not rows:
        return None
    row = dict(rows[0])
    # Do not expose generated temporary credentials through the fast access endpoint.
    row.pop("digiforms_account", None)
    return row


def _latest_timestamp(rows: List[Dict[str, Any]]) -> str:
    values = [str(row.get("updated_at") or row.get("created_at") or "") for row in rows if row]
    return max(values) if values else ""


def _access_version(db: Dict[str, Any], user_id: str, company_id: Optional[str], admin_user_id: Optional[str]) -> str:
    relevant_rows: List[Dict[str, Any]] = []
    for name in ["platform_modules", "company_modules", "user_modules", "admin_users", "extension_requests", "digiforms_accounts", "digiforms_user_links"]:
        for row in table(db, name):
            if name == "platform_modules":
                relevant_rows.append(row)
            elif row.get("user_id") == user_id or row.get("requested_by_user_id") == user_id or row.get("dataris_user_id") == user_id:
                relevant_rows.append(row)
            elif admin_user_id and row.get("admin_user_id") == admin_user_id:
                relevant_rows.append(row)
            elif company_id and row.get("company_id") == company_id:
                relevant_rows.append(row)
    payload = {
        "user_id": user_id,
        "company_id": company_id,
        "admin_user_id": admin_user_id,
        "latest": _latest_timestamp(relevant_rows),
        "count": len(relevant_rows),
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _company_enabled_module_ids(db: Dict[str, Any], company_id: Optional[str]) -> set:
    return module_access.company_enabled_module_ids(table(db, "company_modules"), company_id)


def _user_module_overrides(db: Dict[str, Any], user_id: str, admin_user_id: Optional[str]) -> Dict[str, bool]:
    return module_access.user_module_overrides(table(db, "user_modules"), user_id, admin_user_id)


def _approved_extension_ids(db: Dict[str, Any], user_id: str, company_id: Optional[str]) -> set:
    return module_access.approved_extension_ids(table(db, "extension_requests"), user_id, company_id)


def _effective_module_ids(
    db: Dict[str, Any],
    *,
    active_modules: List[Dict[str, Any]],
    user_id: str,
    admin_user_id: Optional[str],
    company_id: Optional[str],
) -> List[str]:
    """Acceso efectivo: la empresa manda y el usuario sobrescribe módulo a módulo."""
    company_enabled = _company_enabled_module_ids(db, company_id)
    overrides = _user_module_overrides(db, user_id, admin_user_id)
    approved_extensions = _approved_extension_ids(db, user_id, company_id)

    granted = ["dashboard"]
    for row in active_modules:
        module_id = module_catalog.canonical_module_id(row.get("id") or row.get("name"))
        if not module_id or module_id == "dashboard":
            continue
        if module_access.module_is_granted(
            module_id,
            overrides=overrides,
            company_enabled=company_enabled,
            approved_extensions=approved_extensions,
            has_company=bool(company_id),
        ):
            granted.append(module_id)
    return _unique(granted)


@router.get("/access")
def get_current_access(authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")

    with LOCK:
        db = read_db()
        catalog_before = json.dumps(table(db, "platform_modules"), sort_keys=True, default=str)
        catalog = ensure_extension_catalog(db)
        catalog_changed = catalog_before != json.dumps(table(db, "platform_modules"), sort_keys=True, default=str)

        user_id = user.get("id")
        profile = profile_for(db, user_id) or {}
        admin = admin_record_for(db, user_id) or {}
        admin_role = admin.get("admin_role") or None
        admin_user_id = admin.get("id") or None
        company_id = admin.get("company_id") or company_for_user(db, user_id)
        is_superadmin = admin_role == "superadmin"
        is_demo = is_commercial_demo_user(user)
        # El superadmin de la cuenta demo comercial es "company_admin", nunca
        # "superadmin" (ver commercial_demo_seed.py) — is_demo se revisa aparte
        # de todas formas para no depender únicamente de ese dato.
        is_dataris_admin = is_superadmin and not is_demo

        # The commercial tenant is an isolated showcase.  It intentionally sees
        # the complete product catalog even if an operator disabled a module for
        # normal tenants while testing a rollout.
        active_modules = [
            row for row in table(db, "platform_modules")
            if is_demo or row.get("is_active", True) is not False
        ]
        active_platform_ids = _unique([item for row in active_modules for item in [row.get("id"), row.get("name")]])

        if is_demo or is_superadmin:
            effective_ids = _unique(["dashboard", *active_platform_ids])
        else:
            effective_ids = _effective_module_ids(
                db,
                active_modules=active_modules,
                user_id=user_id,
                admin_user_id=admin_user_id,
                company_id=company_id,
            )

        if not is_dataris_admin:
            effective_ids = [module_id for module_id in effective_ids if module_id not in INTERNAL_ONLY_MODULE_IDS]

        modules = []
        for row in active_modules:
            expanded = _unique([row.get("id"), row.get("name")])
            if not is_dataris_admin and any(module_id in INTERNAL_ONLY_MODULE_IDS for module_id in expanded):
                continue
            if is_demo or is_superadmin or any(module_id in effective_ids for module_id in expanded):
                modules.append({
                    "id": _normalize_module_id(row.get("id") or row.get("name")),
                    "name": row.get("name") or row.get("id"),
                    "description": row.get("description"),
                    "icon": row.get("icon"),
                    "is_active": row.get("is_active", True) is not False,
                })

        extensions = []
        extension_statuses: Dict[str, Any] = {}
        for row in catalog:
            extension_id = normalize_extension_id(row.get("id") or row.get("name"))
            enabled = extension_enabled_for(db, company_id, user_id, extension_id)
            latest_request = _latest_request_for(db, user_id, company_id, extension_id)
            status = "enabled" if enabled else (latest_request or {}).get("status") or "not_requested"
            item = {
                "id": extension_id,
                "name": row.get("name") or extension_id,
                "description": row.get("description"),
                "icon": row.get("icon"),
                "is_active": row.get("is_active", True) is not False,
                "is_requestable": row.get("is_requestable", True) is not False,
                "enabled": enabled,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            extensions.append(item)
            extension_statuses[extension_id] = {
                "extension_id": extension_id,
                "enabled": enabled,
                "status": status,
                "request": latest_request,
            }

        access = {
            "user": {
                "id": user_id,
                "email": user.get("email"),
                "is_active": user.get("is_active", True) is not False,
                "first_name": profile.get("first_name"),
                "last_name": profile.get("last_name"),
                "company_name": profile.get("company_name"),
                "is_demo": is_demo,
            },
            "userId": user_id,
            "adminUserId": admin_user_id,
            "companyId": company_id,
            "adminRole": admin_role,
            "isSuperAdmin": is_superadmin,
            "moduleIds": effective_ids,
            "moduleNames": _unique([module.get("name") or module.get("id") for module in modules]),
            "modules": modules,
            "extensions": extensions,
            "extensionStatuses": extension_statuses,
            "accessVersion": _access_version(db, user_id, company_id, admin_user_id),
            "generatedAt": now(),
        }

        # Persist only when the catalog really needed a repair. Previously every
        # access check rewrote the complete compatibility JSON state.
        if catalog_changed:
            write_db(db)
        return {"data": access, "error": None}
