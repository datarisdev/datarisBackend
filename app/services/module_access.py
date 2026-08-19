"""Cálculo del acceso efectivo a módulos.

Regla única, usada tanto por GET /me/access como por el panel de administración:

    la empresa manda y el usuario sobrescribe módulo a módulo.

- `company_modules` es el paquete contratado por la empresa: lo que heredan
  todos sus usuarios.
- `user_modules` NO es una lista blanca que sustituya a ese paquete: cada fila es
  la decisión explícita sobre UN módulo (`is_enabled` true/false). Sin fila, el
  usuario hereda lo de su empresa.

Antes `user_modules` funcionaba como sustitución total y eso rompía dos casos
reales: apagar todos los módulos de un usuario borraba sus filas y el cálculo
volvía a concederle el paquete entero, y aprobar una extensión (que crea la
primera fila del usuario) le quitaba de golpe todo lo heredado.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from app.services import module_catalog


def row_is_enabled(row: Dict[str, Any]) -> bool:
    """Una fila de acceso cuenta como concesión salvo que diga explícitamente que no."""
    return (
        row.get("is_enabled", row.get("is_active", True)) is not False
        and row.get("is_active", row.get("is_enabled", True)) is not False
    )


def company_enabled_module_ids(rows: List[Dict[str, Any]], company_id: Optional[str]) -> Set[str]:
    if not company_id:
        return set()
    return {
        module_catalog.canonical_module_id(row.get("module_id"))
        for row in rows
        if row.get("company_id") == company_id and row_is_enabled(row)
    }


def user_module_overrides(
    rows: List[Dict[str, Any]],
    user_id: str,
    admin_user_id: Optional[str] = None,
) -> Dict[str, bool]:
    """Decisiones explícitas del usuario, módulo a módulo.

    Se aceptan las filas apuntadas por `user_id` y por `admin_user_id` porque los
    datos traen ambas formas según la época y la vía de alta; buscar solo por una
    de ellas era la causa de que el panel pintara módulos apagados que el usuario
    sí tenía, y de que las desactivaciones no surtieran efecto.
    """
    relevant = [
        row
        for row in rows
        if str(row.get("user_id") or "") == str(user_id)
        or (admin_user_id and row.get("admin_user_id") == admin_user_id)
    ]
    # Ante filas duplicadas y contradictorias del mismo módulo, manda la última.
    relevant.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""))
    overrides: Dict[str, bool] = {}
    for row in relevant:
        module_id = module_catalog.canonical_module_id(row.get("module_id"))
        if module_id:
            overrides[module_id] = row_is_enabled(row)
    return overrides


def approved_extension_ids(
    rows: List[Dict[str, Any]],
    user_id: str,
    company_id: Optional[str],
) -> Set[str]:
    """Extensiones con solicitud aprobada, aunque no hayan dejado fila de acceso."""
    approved: Set[str] = set()
    for row in rows:
        if row.get("status") not in {"approved", "enabled"}:
            continue
        if row.get("requested_by_user_id") == user_id or (company_id and row.get("company_id") == company_id):
            approved.add(module_catalog.canonical_module_id(row.get("extension_id")))
    return approved


def module_is_granted(
    module_id: str,
    *,
    overrides: Dict[str, bool],
    company_enabled: Set[str],
    approved_extensions: Set[str],
) -> bool:
    inherited = module_id in company_enabled or module_id in approved_extensions
    return overrides.get(module_id, inherited)
