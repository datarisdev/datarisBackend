"""Administración del acceso a módulos: catálogo, empresas y usuarios.

Antes esto se hacía escribiendo a pelo en `platform_modules`, `company_modules` y
`user_modules` desde el panel con el cliente de compatibilidad, y de ahí salían
los fallos que el operador veía: módulos inventados que no aparecían en ninguna
parte, switches que se pintaban apagados aunque el usuario sí tuviera el módulo,
y desactivaciones que no surtían efecto porque las filas se buscaban por
`admin_user_id` mientras los datos reales venían con `user_id`.

Este router es la única vía soportada. Devuelve, para cada módulo, tres datos
distintos que el panel necesita separar para no mentir:

- `platform_active`: si el módulo está activo en la plataforma (catálogo);
- `company_enabled`: si la empresa lo tiene contratado;
- `override`: la decisión explícita tomada para ESE usuario (`true`, `false` o
  `null` = hereda lo de su empresa).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

from app.api.routers.compat import (
    LOCK,
    active_admin_row,
    bearer_user,
    now,
    panel_email_allowed,
    read_db,
    table,
    write_db,
)
from app.services import module_access, module_catalog
from app.services.commercial_demo_seed import is_commercial_demo_user
from app.services.module_catalog import INTERNAL_ONLY_MODULE_IDS

router = APIRouter(prefix="/compat/admin/module-access", tags=["Admin Module Access"])

MAX_USERS_PAGE = 200


def _require_admin(authorization: Optional[str], db: Dict[str, Any]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    if not panel_email_allowed(user):
        raise HTTPException(
            status_code=403,
            detail="El panel de administración está restringido a las cuentas autorizadas de Dataris",
        )
    admin = active_admin_row(db, str(user.get("id") or ""))
    role = (admin or {}).get("admin_role")
    if role not in {"superadmin", "company_admin"}:
        raise HTTPException(status_code=403, detail="No autorizado para administrar accesos")
    return {"user": user, "admin": admin, "is_superadmin": role == "superadmin"}


def _require_superadmin(authorization: Optional[str], db: Dict[str, Any]) -> Dict[str, Any]:
    ctx = _require_admin(authorization, db)
    if not ctx["is_superadmin"]:
        raise HTTPException(status_code=403, detail="Solo un superadministrador puede cambiar el catálogo de la plataforma")
    return ctx


def _catalog_rows(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filas de `platform_modules` indexadas por su id canónico."""
    rows = []
    for row in table(db, "platform_modules"):
        module_id = module_catalog.canonical_module_id(row.get("id") or row.get("name"))
        if not module_id:
            continue
        rows.append({"module_id": module_id, "row": row})
    return rows


def _company_enabled(db: Dict[str, Any], company_id: Optional[str]) -> set:
    return module_access.company_enabled_module_ids(table(db, "company_modules"), company_id)


def _overrides_for(db: Dict[str, Any], user_id: str, admin_user_id: Optional[str]) -> Dict[str, bool]:
    return module_access.user_module_overrides(table(db, "user_modules"), user_id, admin_user_id)


def _approved_extensions(db: Dict[str, Any], user_id: str, company_id: Optional[str]) -> set:
    return module_access.approved_extension_ids(table(db, "extension_requests"), user_id, company_id)


def _is_system_module(module_id: str) -> bool:
    spec = module_catalog.SPECS_BY_ID.get(module_id)
    return bool(spec and spec.surface == module_catalog.SURFACE_SYSTEM)


def _module_card(module_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    spec = module_catalog.SPECS_BY_ID.get(module_id)
    return {
        "id": module_id,
        "name": (spec.name if spec else (row or {}).get("name")) or module_id,
        "description": (spec.description if spec else (row or {}).get("description")),
        "icon": (spec.icon if spec else (row or {}).get("icon")),
        "category": spec.category if spec else "unknown",
        "surface": spec.surface if spec else "menu",
        "surface_hint": spec.surface_hint if spec else "Módulo fuera del catálogo del producto: revísalo con el equipo de Dataris.",
        "routes": list(spec.routes) if spec else [],
        "assignable": bool(spec and spec.assignable),
        "is_system": _is_system_module(module_id),
        "is_internal": module_id in INTERNAL_ONLY_MODULE_IDS or bool(spec and spec.category == module_catalog.CATEGORY_INTERNAL),
        "in_product_catalog": spec is not None,
        "platform_active": (row or {}).get("is_active", True) is not False if row else False,
    }


def _profile_for(db: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return next(
        (row for row in table(db, "profiles") if str(row.get("user_id") or row.get("id") or "") == str(user_id)),
        {},
    )


def _company_for(db: Dict[str, Any], user_id: str, admin_row: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if admin_row and admin_row.get("company_id"):
        return admin_row.get("company_id")
    profile = _profile_for(db, user_id)
    if profile.get("company_id"):
        return profile.get("company_id")
    company_name = str(profile.get("company_name") or "").strip().lower()
    if company_name:
        match = next(
            (c for c in table(db, "companies") if str(c.get("name") or "").strip().lower() == company_name),
            None,
        )
        if match:
            return match.get("id")
    return None


def _user_display_name(profile: Dict[str, Any], user: Dict[str, Any]) -> str:
    name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip()
    if name:
        return name
    metadata = user.get("user_metadata") or {}
    meta_name = f"{metadata.get('first_name') or ''} {metadata.get('last_name') or ''}".strip()
    return meta_name or str(user.get("email") or "Sin nombre")


def _user_summary(db: Dict[str, Any], user: Dict[str, Any], companies: Dict[str, str]) -> Dict[str, Any]:
    user_id = str(user.get("id") or "")
    profile = _profile_for(db, user_id)
    admin_row = active_admin_row(db, user_id)
    company_id = _company_for(db, user_id, admin_row)
    overrides = _overrides_for(db, user_id, (admin_row or {}).get("id"))
    company_enabled = _company_enabled(db, company_id)
    approved = _approved_extensions(db, user_id, company_id)

    effective = set()
    for module_id, row in ((item["module_id"], item["row"]) for item in _catalog_rows(db)):
        if row.get("is_active", True) is False or module_id in INTERNAL_ONLY_MODULE_IDS:
            continue
        if _is_system_module(module_id):
            effective.add(module_id)
            continue
        if module_access.module_is_granted(
            module_id,
            overrides=overrides,
            company_enabled=company_enabled,
            approved_extensions=approved,
            has_company=bool(company_id),
        ):
            effective.add(module_id)

    return {
        "id": user_id,
        "email": user.get("email"),
        "name": _user_display_name(profile, user),
        "is_active": user.get("is_active", True) is not False,
        "created_at": user.get("created_at"),
        "company_id": company_id,
        "company_name": companies.get(str(company_id or "")) or profile.get("company_name"),
        "admin_role": (admin_row or {}).get("admin_role"),
        "effective_count": len(effective),
        "override_count": len(overrides),
        "inherits_from_company": bool(company_id) and not overrides,
    }


@router.get("/catalog")
def get_catalog(authorization: Optional[str] = Header(default=None)):
    """Catálogo real del producto, con dónde se ve cada módulo y quién lo usa."""
    with LOCK:
        db = read_db()
        _require_admin(authorization, db)

        rows_by_id = {item["module_id"]: item["row"] for item in _catalog_rows(db)}
        companies_by_module: Dict[str, int] = {}
        for row in table(db, "company_modules"):
            if not module_access.row_is_enabled(row):
                continue
            module_id = module_catalog.canonical_module_id(row.get("module_id"))
            companies_by_module[module_id] = companies_by_module.get(module_id, 0) + 1
        users_by_module: Dict[str, Dict[str, int]] = {}
        for row in table(db, "user_modules"):
            module_id = module_catalog.canonical_module_id(row.get("module_id"))
            bucket = users_by_module.setdefault(module_id, {"granted": 0, "revoked": 0})
            bucket["granted" if module_access.row_is_enabled(row) else "revoked"] += 1

        modules = []
        seen = set()
        for spec in module_catalog.MODULE_SPECS:
            row = rows_by_id.get(spec.id)
            card = _module_card(spec.id, row)
            card["companies_enabled"] = companies_by_module.get(spec.id, 0)
            card["users_granted"] = users_by_module.get(spec.id, {}).get("granted", 0)
            card["users_revoked"] = users_by_module.get(spec.id, {}).get("revoked", 0)
            # ml-training no vive en platform_modules: es interno y se sirve por
            # rol, no por catálogo. Solo se lista como información.
            if row is None and spec.category != module_catalog.CATEGORY_INTERNAL:
                continue
            modules.append(card)
            seen.add(spec.id)

        # Filas antiguas que ya no corresponden a ningún módulo del producto:
        # se muestran para que el operador sepa que no hacen nada.
        for module_id, row in rows_by_id.items():
            if module_id in seen:
                continue
            card = _module_card(module_id, row)
            card["companies_enabled"] = companies_by_module.get(module_id, 0)
            card["users_granted"] = users_by_module.get(module_id, {}).get("granted", 0)
            card["users_revoked"] = users_by_module.get(module_id, {}).get("revoked", 0)
            modules.append(card)

        return {
            "data": {
                "modules": modules,
                "derived": [dict(item, depends_on=list(item["depends_on"])) for item in module_catalog.DERIVED_MODULES],
                "companies_total": len(table(db, "companies")),
            },
            "error": None,
        }


@router.patch("/catalog/{module_id}")
def update_catalog_module(
    module_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Activa o desactiva un módulo en TODA la plataforma."""
    with LOCK:
        db = read_db()
        _require_superadmin(authorization, db)

        canonical = module_catalog.canonical_module_id(module_id)
        spec = module_catalog.SPECS_BY_ID.get(canonical)
        if _is_system_module(canonical):
            raise HTTPException(status_code=400, detail=f"{spec.name} es la portada de la plataforma: no se puede desactivar.")

        row = next((item["row"] for item in _catalog_rows(db) if item["module_id"] == canonical), None)
        if not row:
            raise HTTPException(status_code=404, detail="Ese módulo no existe en el catálogo")

        if "is_active" in payload:
            row["is_active"] = bool(payload.get("is_active"))
        row["updated_at"] = now()
        write_db(db)
        return {"data": _module_card(canonical, row), "error": None}


@router.get("/companies")
def list_companies(authorization: Optional[str] = Header(default=None)):
    """Empresas con el paquete de módulos que tienen contratado."""
    with LOCK:
        db = read_db()
        ctx = _require_admin(authorization, db)

        companies = table(db, "companies")
        if not ctx["is_superadmin"]:
            company_id = (ctx["admin"] or {}).get("company_id")
            companies = [c for c in companies if c.get("id") == company_id]

        rows_by_id = {item["module_id"]: item["row"] for item in _catalog_rows(db)}
        assignable = [
            module_id
            for module_id in rows_by_id
            if module_id not in INTERNAL_ONLY_MODULE_IDS and not _is_system_module(module_id)
        ]

        payload = []
        for company in companies:
            enabled = _company_enabled(db, company.get("id"))
            payload.append({
                "id": company.get("id"),
                "name": company.get("name"),
                "is_active": company.get("is_active", True) is not False,
                "modules": {module_id: module_id in enabled for module_id in assignable},
                "users_count": len([
                    row for row in table(db, "admin_users")
                    if row.get("company_id") == company.get("id") and row.get("is_active", True) is not False
                ]),
            })
        payload.sort(key=lambda item: str(item.get("name") or "").lower())
        return {"data": {"companies": payload}, "error": None}


@router.put("/companies/{company_id}")
def update_company_modules(
    company_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Fija el paquete de módulos de una empresa (lo que heredan sus usuarios)."""
    with LOCK:
        db = read_db()
        ctx = _require_admin(authorization, db)
        if not ctx["is_superadmin"] and (ctx["admin"] or {}).get("company_id") != company_id:
            raise HTTPException(status_code=403, detail="Solo puedes cambiar el paquete de tu propia empresa")

        company = next((c for c in table(db, "companies") if c.get("id") == company_id), None)
        if not company:
            raise HTTPException(status_code=404, detail="Empresa no encontrada")

        modules = payload.get("modules")
        if not isinstance(modules, dict):
            raise HTTPException(status_code=400, detail="Falta el mapa de módulos a activar")

        rows_by_id = {item["module_id"]: item["row"] for item in _catalog_rows(db)}
        rows = table(db, "company_modules")
        t = now()
        for raw_id, enabled in modules.items():
            module_id = module_catalog.canonical_module_id(raw_id)
            spec = module_catalog.SPECS_BY_ID.get(module_id)
            if module_id not in rows_by_id:
                raise HTTPException(status_code=400, detail=f"El módulo «{raw_id}» no existe en el catálogo")
            if module_id in INTERNAL_ONLY_MODULE_IDS:
                raise HTTPException(status_code=400, detail=f"«{spec.name if spec else module_id}» es una herramienta interna de Dataris")
            if spec and spec.surface == module_catalog.SURFACE_SYSTEM:
                continue
            existing = [
                row for row in rows
                if row.get("company_id") == company_id
                and module_catalog.canonical_module_id(row.get("module_id")) == module_id
            ]
            if existing:
                for row in existing:
                    row["is_enabled"] = bool(enabled)
                    row["is_active"] = bool(enabled)
                    row["updated_at"] = t
            else:
                rows.append({
                    "id": str(uuid.uuid4()),
                    "company_id": company_id,
                    "module_id": module_id,
                    "is_enabled": bool(enabled),
                    "is_active": bool(enabled),
                    "created_at": t,
                    "updated_at": t,
                })

        write_db(db)
        enabled_now = _company_enabled(db, company_id)
        return {
            "data": {
                "id": company_id,
                "name": company.get("name"),
                "modules": {module_id: module_id in enabled_now for module_id in rows_by_id},
            },
            "error": None,
        }


@router.get("/users")
def list_users(
    search: Optional[str] = Query(default=None),
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_USERS_PAGE),
    authorization: Optional[str] = Header(default=None),
):
    """Todos los usuarios de la plataforma, no solo los que tienen fila de admin."""
    with LOCK:
        db = read_db()
        ctx = _require_admin(authorization, db)
        companies = {str(c.get("id")): c.get("name") for c in table(db, "companies")}

        scope_company = None if ctx["is_superadmin"] else (ctx["admin"] or {}).get("company_id")
        needle = str(search or "").strip().lower()

        summaries = []
        for user in db.get("users", []):
            summary = _user_summary(db, user, companies)
            if scope_company and summary["company_id"] != scope_company:
                continue
            if company_id and summary["company_id"] != company_id:
                continue
            if needle:
                haystack = " ".join([
                    str(summary.get("email") or ""),
                    str(summary.get("name") or ""),
                    str(summary.get("company_name") or ""),
                ]).lower()
                if needle not in haystack:
                    continue
            summaries.append(summary)

        summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "data": {"users": summaries[:limit], "total": len(summaries)},
            "error": None,
        }


def _user_detail(db: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    user = next((u for u in db.get("users", []) if str(u.get("id")) == str(user_id)), None)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    profile = _profile_for(db, user_id)
    admin_row = active_admin_row(db, user_id)
    company_id = _company_for(db, user_id, admin_row)
    company = next((c for c in table(db, "companies") if c.get("id") == company_id), None)

    overrides = _overrides_for(db, user_id, (admin_row or {}).get("id"))
    company_enabled = _company_enabled(db, company_id)
    approved = _approved_extensions(db, user_id, company_id)
    is_superadmin_user = (admin_row or {}).get("admin_role") == "superadmin"
    is_demo_user = is_commercial_demo_user(user)

    modules = []
    for item in _catalog_rows(db):
        module_id, row = item["module_id"], item["row"]
        card = _module_card(module_id, row)
        if card["is_internal"]:
            continue
        inherited = module_id in company_enabled or module_id in approved
        override = overrides.get(module_id)
        if card["is_system"]:
            effective, source = True, "system"
        elif not card["platform_active"]:
            effective, source = False, "platform_off"
        else:
            effective = module_access.module_is_granted(
                module_id,
                overrides=overrides,
                company_enabled=company_enabled,
                approved_extensions=approved,
                has_company=bool(company_id),
            )
            if override is False:
                source = "user"
            elif not inherited and company_id:
                # Un override en `true` sobre algo que la empresa no tiene
                # contratado no concede nada: manda el paquete.
                source = "company_off"
            elif override is True:
                source = "user"
            else:
                source = "company" if inherited else "none"
        card.update({
            "company_enabled": module_id in company_enabled,
            "approved_extension": module_id in approved,
            "override": override,
            "effective": effective,
            "source": source,
        })
        modules.append(card)

    return {
        "user": {
            "id": user_id,
            "email": user.get("email"),
            "name": _user_display_name(profile, user),
            "is_active": user.get("is_active", True) is not False,
            "admin_role": (admin_row or {}).get("admin_role"),
            "admin_user_id": (admin_row or {}).get("id"),
            "created_at": user.get("created_at"),
            # Un superadmin de Dataris y la cuenta demo comercial ven la
            # plataforma completa por su rol: los switches de abajo no les
            # aplican y el panel debe decirlo.
            "sees_everything": is_superadmin_user or is_demo_user,
            "is_demo": is_demo_user,
        },
        "company": {"id": company_id, "name": (company or {}).get("name") or profile.get("company_name")} if company_id or profile.get("company_name") else None,
        "modules": modules,
    }


@router.get("/users/{user_id}")
def get_user_access(user_id: str, authorization: Optional[str] = Header(default=None)):
    with LOCK:
        db = read_db()
        ctx = _require_admin(authorization, db)
        detail = _user_detail(db, user_id)
        if not ctx["is_superadmin"]:
            target_company = (detail.get("company") or {}).get("id")
            if target_company != (ctx["admin"] or {}).get("company_id"):
                raise HTTPException(status_code=403, detail="Ese usuario no pertenece a tu empresa")
        return {"data": detail, "error": None}


@router.put("/users/{user_id}")
def update_user_access(
    user_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Guarda los overrides de un usuario.

    `overrides` es un mapa `{module_id: true | false | null}`: `true` se lo
    concede, `false` se lo quita aunque su empresa lo tenga, y `null` borra la
    decisión para que vuelva a heredar de la empresa.
    """
    with LOCK:
        db = read_db()
        ctx = _require_admin(authorization, db)

        detail = _user_detail(db, user_id)
        company_id = (detail.get("company") or {}).get("id")
        if not ctx["is_superadmin"] and company_id != (ctx["admin"] or {}).get("company_id"):
            raise HTTPException(status_code=403, detail="Ese usuario no pertenece a tu empresa")

        overrides = payload.get("overrides")
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=400, detail="Falta el mapa de módulos (overrides)")

        rows_by_id = {item["module_id"]: item["row"] for item in _catalog_rows(db)}
        company_enabled = _company_enabled(db, company_id)
        admin_row = active_admin_row(db, user_id)
        rows = table(db, "user_modules")
        t = now()

        for raw_id, value in overrides.items():
            module_id = module_catalog.canonical_module_id(raw_id)
            spec = module_catalog.SPECS_BY_ID.get(module_id)
            label = spec.name if spec else raw_id
            if module_id not in rows_by_id:
                raise HTTPException(status_code=400, detail=f"El módulo «{raw_id}» no existe en el catálogo")
            if module_id in INTERNAL_ONLY_MODULE_IDS:
                raise HTTPException(status_code=400, detail=f"«{label}» es una herramienta interna de Dataris")
            if spec and spec.surface == module_catalog.SURFACE_SYSTEM:
                continue
            if value is True and company_id and module_id not in company_enabled:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"«{label}» no está en el paquete de la empresa. "
                        "Actívalo primero para la empresa y después ajústalo por usuario."
                    ),
                )

            matching = [
                row for row in rows
                if module_catalog.canonical_module_id(row.get("module_id")) == module_id
                and (
                    str(row.get("user_id") or "") == str(user_id)
                    or (admin_row and row.get("admin_user_id") == admin_row.get("id"))
                )
            ]

            if value is None:
                # Vuelve a heredar: se retiran TODAS las filas del usuario para
                # ese módulo, vengan por user_id o por admin_user_id (mezclarlas
                # era justo lo que dejaba desactivaciones sin efecto).
                for row in matching:
                    rows.remove(row)
                continue

            enabled = bool(value)
            if matching:
                for row in matching:
                    row["user_id"] = row.get("user_id") or user_id
                    row["admin_user_id"] = row.get("admin_user_id") or (admin_row or {}).get("id")
                    row["module_id"] = module_id
                    row["is_enabled"] = enabled
                    row["is_active"] = enabled
                    row["updated_at"] = t
            else:
                rows.append({
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "admin_user_id": (admin_row or {}).get("id"),
                    "module_id": module_id,
                    "is_enabled": enabled,
                    "is_active": enabled,
                    "created_at": t,
                    "updated_at": t,
                })

        write_db(db)
        return {"data": _user_detail(db, user_id), "error": None}
