"""Bitácora de campo: la lectura calculada de los formularios de AgtechApps.

La captura vive en AgtechApps; aquí solo se calcula. El módulo expone las
bitácoras (una por ciclo × validación × parcela) con todo lo que la hoja del CDT
resolvía con fórmulas y que del lado del formulario no se puede expresar: los
pies de bloque, la cabecera económica, los indicadores por tonelada y la matriz
de sensibilidad.

Nada de esto se almacena. Se recalcula en cada consulta a partir de las
respuestas ya sincronizadas, igual que la hoja se recalcula al abrirla: así una
corrección en AgtechApps se ve en el siguiente refresco sin migrar datos.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query

from app.api.routers.compat import LOCK, read_db, table
from app.api.routers.compat_sig import (
    _company_id_for_user,
    _require_user,
    _same_company_or_legacy_user,
)
from app.services.digiforms_company_config import mappings_for_company
from app.services.digiforms_field_log import (
    FIELD_LOG_CYCLE_FORM_TYPE,
    FIELD_LOG_ENTRY_FORM_TYPE,
    FIELD_LOG_FORM_TYPE_LABELS,
    FIELD_LOG_FORM_TYPES,
    FIELD_LOG_PHENOLOGY_FORM_TYPE,
    group_by_cycle,
    table_for_form_type,
)

router = APIRouter(prefix="/compat/field-log", tags=["Bitácora de campo (compat)"])


def _scoped(db: Dict[str, Any], table_name: str, *, company_id: Optional[str], user_id: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in table(db, table_name)
        if _same_company_or_legacy_user(row, company_id=company_id, user_id=user_id)
    ]


def _load(db: Dict[str, Any], *, company_id: Optional[str], user_id: str) -> Dict[str, List[Dict[str, Any]]]:
    return {
        form_type: _scoped(db, table_for_form_type(form_type), company_id=company_id, user_id=user_id)
        for form_type in FIELD_LOG_FORM_TYPES
    }


def _connection_status(db: Dict[str, Any], company_id: Optional[str]) -> List[Dict[str, Any]]:
    """Qué formulario de AgtechApps alimenta cada parte y cuándo se trajo.

    Sin esto, una bitácora vacía es indistinguible de una bitácora sin
    configurar, que es justo la pregunta que se hace quien abre el módulo y no
    ve nada.
    """
    mappings = {str(row.get("form_type")): row for row in mappings_for_company(db, company_id)}
    cursors = {
        str(row.get("form_type")): row
        for row in table(db, "sig_sync_cursors")
        if str(row.get("company_id") or "") == str(company_id or "")
    }
    status = []
    for form_type in FIELD_LOG_FORM_TYPES:
        mapping = mappings.get(form_type) or {}
        cursor = cursors.get(form_type) or {}
        status.append({
            "form_type": form_type,
            "label": FIELD_LOG_FORM_TYPE_LABELS[form_type],
            "form_id": mapping.get("form_id") or "",
            "display_name": mapping.get("display_name") or "",
            "is_linked": bool(mapping.get("form_id")) and mapping.get("is_enabled", True) is not False,
            "last_sync_at": cursor.get("last_sync_at"),
            "last_response_id": cursor.get("last_response_id"),
            "last_error": cursor.get("last_error"),
        })
    return status


@router.get("/status")
def get_status(authorization: Optional[str] = Header(default=None)):
    """Si la empresa tiene la bitácora conectada, y nada más.

    Lo consulta la barra lateral para decidir si enseña el módulo, así que tiene
    que ser barato: mira los vínculos y los cursores, sin leer las respuestas ni
    calcular ningún indicador. `/cycles` hace ambas cosas y sería un desperdicio
    ejecutarlo en cada carga de página solo para pintar —o no— una entrada de
    menú.
    """
    user = _require_user(authorization)
    with LOCK:
        db = read_db()
        company_id = _company_id_for_user(db, str(user.get("id") or ""))
        forms = _connection_status(db, company_id)
    return {
        "data": {"is_configured": any(item["is_linked"] for item in forms), "forms": forms},
        "error": None,
    }


@router.get("/cycles")
def list_cycles(authorization: Optional[str] = Header(default=None)):
    """Todas las bitácoras de la empresa, ya calculadas."""
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        company_id = _company_id_for_user(db, user_id)
        rows = _load(db, company_id=company_id, user_id=user_id)
        status = _connection_status(db, company_id)
    cycles = group_by_cycle(
        entries=rows[FIELD_LOG_ENTRY_FORM_TYPE],
        sheets=rows[FIELD_LOG_CYCLE_FORM_TYPE],
        phenology=rows[FIELD_LOG_PHENOLOGY_FORM_TYPE],
    )
    return {
        "data": {
            "cycles": cycles,
            "forms": status,
            "is_configured": any(item["is_linked"] for item in status),
            "totals": {
                "cycles": len(cycles),
                "entries": len(rows[FIELD_LOG_ENTRY_FORM_TYPE]),
                "cycle_sheets": len(rows[FIELD_LOG_CYCLE_FORM_TYPE]),
                "phenology": len(rows[FIELD_LOG_PHENOLOGY_FORM_TYPE]),
            },
        },
        "error": None,
    }


@router.get("/cycles/{cycle_key}")
def get_cycle(cycle_key: str, authorization: Optional[str] = Header(default=None)):
    """Una bitácora con el detalle de sus labores, ordenadas como la hoja."""
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        company_id = _company_id_for_user(db, user_id)
        rows = _load(db, company_id=company_id, user_id=user_id)

    def mine(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [row for row in items if str(row.get("cycle_key") or "") == cycle_key]

    entries = mine(rows[FIELD_LOG_ENTRY_FORM_TYPE])
    sheets = mine(rows[FIELD_LOG_CYCLE_FORM_TYPE])
    phenology = mine(rows[FIELD_LOG_PHENOLOGY_FORM_TYPE])
    if not entries and not sheets and not phenology:
        raise HTTPException(status_code=404, detail="Esa bitácora no existe o no pertenece a tu empresa.")

    cycles = group_by_cycle(entries=entries, sheets=sheets, phenology=phenology)
    report = cycles[0] if cycles else {}
    entries_sorted = sorted(
        entries,
        key=lambda row: (str(row.get("categoria") or "zzz"), str(row.get("fecha") or ""), str(row.get("hora") or "")),
    )
    return {
        "data": {
            **report,
            "entries": entries_sorted,
            "cycle_sheets": sheets,
        },
        "error": None,
    }


@router.get("/entries")
def list_entries(
    cycle_key: Optional[str] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Labores en crudo, para tablas y exportaciones."""
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        company_id = _company_id_for_user(db, user_id)
        entries = _scoped(
            db,
            table_for_form_type(FIELD_LOG_ENTRY_FORM_TYPE),
            company_id=company_id,
            user_id=user_id,
        )
    rows = [
        row
        for row in entries
        if (not cycle_key or str(row.get("cycle_key") or "") == cycle_key)
        and (not categoria or str(row.get("categoria") or "") == categoria)
    ]
    rows.sort(key=lambda row: str(row.get("fecha") or ""), reverse=True)
    return {"data": rows, "error": None, "count": len(rows)}
