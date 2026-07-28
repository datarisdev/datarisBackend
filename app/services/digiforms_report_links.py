"""Enlace entre formularios de AgtechApps y plantillas de Reportes de Campo.

La integración original con AgtechApps/DigiForms sólo contemplaba dos formatos
fijos (`harvest` y `pest_weed`), cada uno con un parser escrito a mano en
`compat_sig.py`. Eso no escala: cada cliente arma sus propios formularios y el
módulo de Reportes de Campo ya permite describirlos como plantillas
declarativas.

Este módulo generaliza la integración sin romper los dos tipos existentes:

* **Catálogo** (`digiforms_forms`): los formularios que una empresa tiene en
  AgtechApps, con nombre legible. Existe porque el proveedor NO expone ninguna
  ruta para listarlos — sólo `results/GetAll` e `images` — así que el listado hay
  que mantenerlo de este lado. Es lo que alimenta el desplegable de la
  configuración, en lugar de pegar a mano el FormId interno.
* **Vínculo** (`digiforms_form_mappings` con `form_type` = `report:<FormId>`):
  ata un formulario de AgtechApps a una plantilla de Reportes. Se reutiliza la
  tabla de mapeos existente para que el cursor de sincronización, el borrado y
  la configuración por empresa sigan funcionando igual.
* **Mapeo de campos**: qué pregunta de AgtechApps llena qué campo de la
  plantilla. Se propone automáticamente cruzando los campos que la Data API
  descubre con los de la plantilla, y el admin puede corregirlo.

El formulario en sí se crea en el portal de AgtechApps: su API no permite darlo
de alta desde fuera. Lo que sí queda automático es todo lo posterior — enlazar,
mapear, sincronizar y aterrizar cada respuesta como un envío de reporte
georreferenciado.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.digiforms_data_api import normalize_key

# Los vínculos de reportes viven en la misma tabla que `harvest`/`pest_weed`.
# Se distinguen por prefijo, y se usa el FormId como discriminante para que una
# empresa pueda enlazar dos formularios distintos a la misma plantilla y para
# que cada uno conserve su propio cursor de sincronización.
REPORT_FORM_TYPE_PREFIX = "report:"


def report_form_type_for(form_id: Any) -> str:
    return f"{REPORT_FORM_TYPE_PREFIX}{str(form_id or '').strip()}"


def is_report_form_type(form_type: Any) -> bool:
    return str(form_type or "").startswith(REPORT_FORM_TYPE_PREFIX)


def form_id_from_report_type(form_type: Any) -> str:
    raw = str(form_type or "")
    return raw[len(REPORT_FORM_TYPE_PREFIX):] if is_report_form_type(raw) else ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# Catálogo de formularios de AgtechApps
# ---------------------------------------------------------------------------


def forms_for_company(db: Dict[str, Any], company_id: Optional[str]) -> List[Dict[str, Any]]:
    if not company_id:
        return []
    rows = db.setdefault("tables", {}).setdefault("digiforms_forms", [])
    return [row for row in rows if str(row.get("company_id") or "") == str(company_id)]


def find_form(db: Dict[str, Any], company_id: Optional[str], form_id: Any) -> Optional[Dict[str, Any]]:
    target = _text(form_id)
    if not target:
        return None
    return next((row for row in forms_for_company(db, company_id) if _text(row.get("form_id")) == target), None)


def safe_form(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "form_id": row.get("form_id"),
        "name": row.get("name") or row.get("form_id"),
        "description": row.get("description") or "",
        "discovered_fields": list(row.get("discovered_fields") or []),
        "last_verified_at": row.get("last_verified_at"),
        "last_records_seen": row.get("last_records_seen"),
        "verification_status": row.get("verification_status") or "not_tested",
        "verification_error": row.get("verification_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        # Metadatos que sólo trae el listado del proveedor. `valid_to` sirve para
        # avisar de un formulario cuya vigencia ya expiró: seguiría enlazado pero
        # sin recibir respuestas nuevas.
        "category": row.get("category") or "",
        "provider_status": row.get("provider_status") or "",
        "reference_id": row.get("reference_id") or "",
        "valid_from": row.get("valid_from") or "",
        "valid_to": row.get("valid_to") or "",
        "is_public": bool(row.get("is_public")),
        "source": row.get("source") or "manual",
    }


def safe_forms(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [safe_form(row) for row in rows]


# ---------------------------------------------------------------------------
# Lectura de una plantilla de Reportes
# ---------------------------------------------------------------------------


def template_fields(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Aplana una plantilla a la lista de valores que un envío puede llevar.

    Cada entrada trae la `key` con la que el renderer guarda el dato (ver
    `ReportRenderer.tsx`: los campos simples usan su `name`, las evaluaciones
    `eval.<bloque>.<fila>`, las tablas `prod.<bloque>` y las valoraciones
    `rating.<bloque>`) junto con la etiqueta visible, que es lo que de verdad se
    parece al nombre de la pregunta en AgtechApps.
    """
    fields: List[Dict[str, Any]] = []

    for header_field in schema.get("header") or []:
        if not isinstance(header_field, dict):
            continue
        name = _text(header_field.get("name"))
        if name:
            fields.append({"key": name, "label": _text(header_field.get("label")) or name, "kind": "field"})

    for tab in schema.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        for block in tab.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            kind = _text(block.get("kind"))
            block_id = _text(block.get("id"))
            if kind == "field-grid":
                for field in block.get("fields") or []:
                    if not isinstance(field, dict):
                        continue
                    name = _text(field.get("name"))
                    if name:
                        fields.append({"key": name, "label": _text(field.get("label")) or name, "kind": "field"})
            elif kind == "eval-table":
                for row in block.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    row_key = _text(row.get("key"))
                    if row_key:
                        fields.append({
                            "key": f"eval.{block_id}.{row_key}",
                            "label": _text(row.get("label")) or row_key,
                            "kind": "eval",
                            "scale": _text(block.get("scale")) or "bueno-regular-malo-na",
                        })
            elif kind == "product-table":
                fields.append({
                    "key": f"prod.{block_id}",
                    "label": _text(block.get("title")) or "Tabla de productos",
                    "kind": "table",
                    "columns": [
                        {"key": _text(column.get("key")), "label": _text(column.get("label"))}
                        for column in (block.get("columns") or [])
                        if isinstance(column, dict) and _text(column.get("key"))
                    ],
                })
            elif kind == "rating":
                fields.append({
                    "key": f"rating.{block_id}",
                    "label": _text(block.get("label")) or _text(block.get("title")) or "Valoración",
                    "kind": "rating",
                })

    return fields


# ---------------------------------------------------------------------------
# Propuesta automática de mapeo
# ---------------------------------------------------------------------------


def suggest_field_map(schema: Dict[str, Any], discovered_fields: Sequence[str]) -> Dict[str, str]:
    """Propone qué pregunta de AgtechApps llena cada campo de la plantilla.

    Primero busca coincidencia exacta (normalizada) contra la clave y contra la
    etiqueta. Después admite coincidencia por contención, pero **sólo cuando hay
    un único candidato**: un mapeo ambiguo silencioso sería peor que dejar el
    campo sin mapear, porque nadie lo revisaría.
    """
    available = [(normalize_key(name), str(name)) for name in discovered_fields if _text(name)]
    if not available:
        return {}

    by_normalized: Dict[str, str] = {}
    for normalized, original in available:
        by_normalized.setdefault(normalized, original)

    mapping: Dict[str, str] = {}
    taken: set[str] = set()

    def claim(template_key: str, api_field: str) -> None:
        mapping[template_key] = api_field
        taken.add(api_field)

    pending: List[Dict[str, Any]] = []
    for field in template_fields(schema):
        # Las tablas de productos corresponden a grupos de detalle repetibles y
        # no a una sola pregunta; se dejan fuera del automapeo.
        if field.get("kind") == "table":
            continue
        candidates = [normalize_key(field["key"]), normalize_key(field.get("label"))]
        match = next((by_normalized[c] for c in candidates if c and c in by_normalized and by_normalized[c] not in taken), None)
        if match:
            claim(field["key"], match)
        else:
            pending.append(field)

    for field in pending:
        needles = [n for n in {normalize_key(field["key"]), normalize_key(field.get("label"))} if len(n) >= 4]
        if not needles:
            continue
        matches = {
            original
            for normalized, original in available
            if original not in taken and any(needle in normalized or normalized in needle for needle in needles)
        }
        if len(matches) == 1:
            claim(field["key"], matches.pop())

    return mapping


# ---------------------------------------------------------------------------
# Conversión de una respuesta de AgtechApps a valores de un envío
# ---------------------------------------------------------------------------

# Etiquetas que el renderer usa en cada escala (ver SCALE_COLUMNS en
# ReportRenderer.tsx). Se aceptan tanto la clave como el texto que un
# encuestador habría elegido en la app.
_SCALE_VALUES: Dict[str, Dict[str, str]] = {
    "bueno-regular-malo-na": {
        "b": "b", "bueno": "b", "buena": "b", "good": "b",
        "r": "r", "regular": "r",
        "m": "m", "malo": "m", "mala": "m", "bad": "m",
        "n": "n", "na": "n", "noaplica": "n",
    },
    "si-no": {
        "si": "si", "s": "si", "yes": "si", "true": "si", "1": "si",
        "no": "no", "n": "no", "false": "no", "0": "no",
    },
}


def _coerce_eval_value(raw: Any, scale: str) -> Optional[str]:
    table = _SCALE_VALUES.get(scale) or _SCALE_VALUES["bueno-regular-malo-na"]
    return table.get(normalize_key(raw))


def _coerce_rating(raw: Any) -> Optional[float]:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def values_from_response(
    schema: Dict[str, Any],
    field_map: Dict[str, str],
    row: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Traduce una respuesta de AgtechApps a los `values` de un envío.

    Devuelve también los campos mapeados que no traían dato, para que el resumen
    de la sincronización deje ver si un formulario está enlazado a medias en vez
    de fallar en silencio.
    """
    by_key = {field["key"]: field for field in template_fields(schema)}
    normalized_row = {normalize_key(key): value for key, value in row.items()}
    values: Dict[str, Any] = {}
    missing: List[str] = []

    for template_key, api_field in (field_map or {}).items():
        if not api_field:
            continue
        raw = row.get(api_field)
        if raw in (None, ""):
            raw = normalized_row.get(normalize_key(api_field))
        if raw in (None, ""):
            missing.append(template_key)
            continue

        field = by_key.get(template_key) or {"kind": "field"}
        kind = field.get("kind")
        if kind == "eval":
            coerced = _coerce_eval_value(raw, str(field.get("scale") or "bueno-regular-malo-na"))
            if coerced is None:
                # No encaja en la escala: se conserva como observación para no
                # perder lo que el encuestador escribió.
                values[f"{template_key}.obs"] = str(raw)
            else:
                values[template_key] = coerced
        elif kind == "rating":
            rating = _coerce_rating(raw)
            if rating is not None:
                values[template_key] = rating
        elif isinstance(raw, (dict, list)):
            values[template_key] = raw
        else:
            values[template_key] = raw

    return values, missing


def photos_from_image_urls(image_urls: Sequence[str]) -> List[Dict[str, Any]]:
    """Adapta las imágenes de AgtechApps al formato de fotos de un envío."""
    return [
        {"slot": index, "url": str(url), "caption": ""}
        for index, url in enumerate(image_urls)
        if _text(url)
    ]
