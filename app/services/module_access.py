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
    """Extensiones con solicitud aprobada, aunque no hayan dejado fila de acceso.

    Solo cuentan las que HOY son extensiones del catálogo. Graniot dejó de serlo
    al unificarse con Monitoreo Satelital, y una solicitud suya aprobada no puede
    seguir concediendo un módulo del paquete por encima de lo que la empresa
    tiene contratado: ese acceso vive ahora en `company_modules` (lo escribió la
    migración `graniot_merged_into_satelite_v1`).
    """
    approved: Set[str] = set()
    for row in rows:
        if row.get("status") not in {"approved", "enabled"}:
            continue
        if row.get("requested_by_user_id") != user_id and not (company_id and row.get("company_id") == company_id):
            continue
        module_id = module_catalog.canonical_module_id(row.get("extension_id"))
        spec = module_catalog.SPECS_BY_ID.get(module_id)
        if spec is not None and spec.category != module_catalog.CATEGORY_EXTENSION:
            continue
        approved.add(module_id)
    return approved


def module_is_granted(
    module_id: str,
    *,
    overrides: Dict[str, bool],
    company_enabled: Set[str],
    approved_extensions: Set[str],
    has_company: bool = False,
) -> bool:
    """El paquete de la empresa es el techo; el usuario solo puede quedarse corto.

    Un `override` en `true` NO concede un módulo del producto que la empresa no
    tiene contratado. Sin este tope, los usuarios dados de alta con el sistema
    anterior (que escribía una fila positiva por cada módulo marcado en el alta)
    quedaban inmunes al paquete de su empresa: quitarle un módulo a la empresa
    no les hacía nada, que era justo el síntoma reportado.

    Dos excepciones, ambas del producto y no del cálculo:

    - las extensiones (DigiformsApp, Graniot) se conceden por solicitud
      aprobada, que deja la fila del usuario sin pasar por `company_modules`; y
    - un usuario sin empresa no hereda nada, así que sus overrides son su única
      fuente de acceso.
    """
    inherited = module_id in company_enabled or module_id in approved_extensions
    override = overrides.get(module_id)
    if override is False:
        return False
    if has_company and not inherited and is_company_package_module(module_id):
        return False
    return True if override is True else inherited


def is_company_package_module(module_id: str) -> bool:
    """¿El módulo forma parte del paquete que contrata una empresa?

    Las extensiones no: tienen su propio circuito de solicitud y aprobación.
    Un módulo que no esté en el catálogo se trata como parte del paquete, que es
    la lectura prudente (no concede de más).
    """
    spec = module_catalog.SPECS_BY_ID.get(module_id)
    return spec is None or spec.category != module_catalog.CATEGORY_EXTENSION
