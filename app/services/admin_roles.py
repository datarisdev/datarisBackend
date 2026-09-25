"""Un solo rol de administrador, cuyo alcance depende de la empresa (hallazgo H9).

El panel ofrecía tres roles (SuperAdmin, Admin de empresa y Usuario operativo)
y el cliente no veía la diferencia entre los dos primeros. El 24 sep 2026 se
dio de alta en ASV un usuario como SuperAdmin: veía todos los módulos por su
rol y, peor, podía administrar todas las empresas de la plataforma.

Ahora quien da de alta solo elige entre «administrador» y «usuario operativo»:

- un administrador de la empresa del equipo de Dataris es `superadmin`;
- un administrador de cualquier otra empresa es `company_admin`.

Los valores guardados no cambian (`superadmin`, `company_admin`,
`company_user`), así que el resto del backend sigue leyendo lo mismo.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

ADMIN_ROLES = {"superadmin", "company_admin", "admin"}
USER_ROLE = "company_user"

# Nombre de la empresa del equipo interno. Se puede fijar por id con
# DATARIS_TEAM_COMPANY_IDS (separados por comas) si algún día se renombra.
DATARIS_TEAM_COMPANY_NAME = "dataris"


def _team_company_ids() -> set[str]:
    raw = os.getenv("DATARIS_TEAM_COMPANY_IDS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def is_dataris_team_company(company: Optional[Dict[str, Any]]) -> bool:
    if not company:
        return False
    ids = _team_company_ids()
    if ids:
        return str(company.get("id") or "") in ids
    return str(company.get("name") or "").strip().lower() == DATARIS_TEAM_COMPANY_NAME


def find_company(db: Dict[str, Any], company_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not company_id:
        return None
    return next(
        (
            c
            for c in (db.get("tables") or {}).get("companies") or []
            if str(c.get("id") or "") == str(company_id)
        ),
        None,
    )


def is_admin_role(role: Any) -> bool:
    return str(role or "") in ADMIN_ROLES


def role_for_company(db: Dict[str, Any], company_id: Optional[str], requested_role: Any) -> str:
    """Traduce el rol pedido al que corresponde según la empresa.

    Cualquier forma de «administrador» (`admin`, `company_admin` o
    `superadmin`) se guarda como `superadmin` en la empresa de Dataris y como
    `company_admin` en las demás. Un administrador sin empresa no tiene sentido
    (no hay nada que administrar ni un paquete que heredar): se rechaza fuera.
    """
    if not is_admin_role(requested_role):
        return USER_ROLE
    return "superadmin" if is_dataris_team_company(find_company(db, company_id)) else "company_admin"
