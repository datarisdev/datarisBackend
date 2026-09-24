"""Baja completa de una empresa desde el panel de administración.

Antes el panel borraba solo la fila de `companies` con el API genérico de
tablas: sus usuarios, módulos y lotes se quedaban colgados apuntando a una
empresa que ya no existía (hallazgo H6 de la revisión del 14 sep). Aquí la baja
arrastra todo lo que es de la empresa, y primero se puede pedir el resumen de
lo que se va a borrar.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException

from app.api.routers.compat import (
    LOCK,
    _company_for_user,
    _is_platform_superadmin,
    bearer_user,
    is_panel_operator,
    panel_email_allowed,
    read_db,
    schedule_graniot_parcel_delete,
    table,
    write_db,
)

router = APIRouter(prefix="/compat/admin/companies", tags=["Admin Companies"])


def _require_superadmin(db: Dict[str, Any], authorization: Optional[str]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    if not panel_email_allowed(user) or not _is_platform_superadmin(db, str(user.get("id") or "")):
        raise HTTPException(status_code=403, detail="Solo un superadministrador de Dataris puede dar de baja empresas")
    return user


def _members(db: Dict[str, Any], company_id: str) -> List[Dict[str, Any]]:
    return [
        u
        for u in db.get("users", [])
        if u.get("id") and str(_company_for_user(db, str(u.get("id"))) or "") == company_id
    ]


@router.post("/{company_id}/delete")
def delete_company(
    company_id: str,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    """Da de baja una empresa con sus usuarios, módulos y lotes.

    Con `dry_run` (por defecto true) solo devuelve lo que se borraría. No borra
    una empresa con cuentas del equipo de Dataris (las del panel o superadmins),
    ni la empresa de quien hace la petición. Los lotes que estaban en Graniot se
    quitan también de allí; las cuentas de Graniot no se tocan.
    """
    dry_run = payload.get("dry_run", True) is not False
    with LOCK:
        db = read_db()
        actor = _require_superadmin(db, authorization)
        company = next((c for c in table(db, "companies") if str(c.get("id")) == str(company_id)), None)
        if not company:
            raise HTTPException(status_code=404, detail="Empresa no encontrada")
        company_id = str(company.get("id"))

        members = _members(db, company_id)
        member_ids = {str(u.get("id")) for u in members}
        if str(actor.get("id")) in member_ids:
            raise HTTPException(status_code=409, detail="No puedes dar de baja tu propia empresa")
        staff = [
            u.get("email")
            for u in members
            if is_panel_operator(db, str(u.get("id"))) or _is_platform_superadmin(db, str(u.get("id")))
        ]
        if staff:
            raise HTTPException(
                status_code=409,
                detail=f"La empresa tiene cuentas del equipo de Dataris ({', '.join(sorted(staff))}): sácalas antes de darla de baja",
            )

        parcels = [
            row
            for row in table(db, "parcels")
            if str(row.get("company_id") or "") == company_id or str(row.get("user_id") or "") in member_ids
        ]
        modules = [row for row in table(db, "company_modules") if str(row.get("company_id") or "") == company_id]
        summary = {
            "company": {"id": company_id, "name": company.get("name")},
            "users": sorted(str(u.get("email") or u.get("id")) for u in members),
            "parcels": len(parcels),
            "parcels_in_graniot": sum(1 for r in parcels if r.get("graniot_parcel_id") or r.get("graniot_parcels")),
            "modules": len(modules),
            "dry_run": dry_run,
        }
        if dry_run:
            return {"data": summary, "error": None}

        users_by_id = {str(u.get("id")): u for u in db.get("users", [])}
        removed_parcels = [dict(row) for row in parcels]

        def belongs(row: Dict[str, Any], name: str) -> bool:
            if str(row.get("company_id") or "") == company_id:
                return True
            if str(row.get("user_id") or "") in member_ids or str(row.get("id") or "") in member_ids:
                return True
            return name == "companies" and str(row.get("id")) == company_id

        db["users"] = [u for u in db.get("users", []) if str(u.get("id")) not in member_ids]
        removed_rows: Dict[str, int] = {}
        for name, rows in db["tables"].items():
            kept = [row for row in rows if not belongs(row, name)]
            if len(kept) != len(rows):
                removed_rows[name] = len(rows) - len(kept)
            db["tables"][name] = kept
        write_db(db)

    by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for row in removed_parcels:
        if row.get("graniot_parcel_id") or row.get("graniot_parcels"):
            by_owner.setdefault(str(row.get("user_id") or ""), []).append(row)
    for owner_id, owned in by_owner.items():
        if users_by_id.get(owner_id):
            schedule_graniot_parcel_delete(background_tasks, users_by_id[owner_id], owned)

    summary["removed_rows"] = removed_rows
    return {"data": summary, "error": None}
