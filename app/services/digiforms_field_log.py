"""Bitácora de campo alimentada desde AgtechApps.

El módulo de Bitácora nació replicando en Dataris la hoja de cálculo del CDT
FIRA: se capturaba la labor a mano y el módulo calculaba. La captura a mano no
prendió —el técnico ya llena formularios en la app de AgtechApps— así que la
bitácora deja de ser un formulario de Dataris y pasa a ser **una lectura**: los
formularios se llenan en AgtechApps y aquí se hacen los cálculos que la hoja
hacía y que del lado de AgtechApps no se pueden expresar (sumas por bloque,
indicadores por tonelada, punto de equilibrio, matriz de sensibilidad).

Tres formularios componen la bitácora, y cada uno entra como un `form_type`
propio del conector de DigiForms:

- ``field_log``            → *Bitácora de Campo*: una fila por labor realizada.
- ``field_log_cycle``      → *Ficha de Validación*: una fila por ciclo, con la
  superficie, el cultivo y el rendimiento/precio de cierre.
- ``field_log_phenology``  → *Fenología*: una observación de etapa.

Las tres se agrupan por la misma clave de ciclo (``Ciclo`` + ``Validacion`` +
``Parcela``), que es como el CDT identifica una bitácora en papel.

Las fórmulas NO se reimplementan aquí: `app.modules.field_log.kpi` ya las
replica contra el Excel original y sus funciones son puras, así que se
reutilizan tal cual. Este módulo solo traduce una respuesta de DigiForms a la
forma que esas funciones esperan.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.modules.field_log import kpi as field_log_kpi
from app.modules.field_log import sensitivity as field_log_sensitivity
from app.modules.field_log.catalog import CATEGORY_LABELS, LOG_CATEGORIES

FIELD_LOG_ENTRY_FORM_TYPE = "field_log"
FIELD_LOG_CYCLE_FORM_TYPE = "field_log_cycle"
FIELD_LOG_PHENOLOGY_FORM_TYPE = "field_log_phenology"

FIELD_LOG_FORM_TYPES: Tuple[str, ...] = (
    FIELD_LOG_ENTRY_FORM_TYPE,
    FIELD_LOG_CYCLE_FORM_TYPE,
    FIELD_LOG_PHENOLOGY_FORM_TYPE,
)

# Nombre visible de cada formulario cuando se da de alta el vínculo.
FIELD_LOG_FORM_TYPE_LABELS: Dict[str, str] = {
    FIELD_LOG_ENTRY_FORM_TYPE: "Bitácora de campo — labores",
    FIELD_LOG_CYCLE_FORM_TYPE: "Bitácora de campo — ficha de validación",
    FIELD_LOG_PHENOLOGY_FORM_TYPE: "Bitácora de campo — fenología",
}

# Dónde aterriza cada formulario dentro del almacén de compatibilidad.
FIELD_LOG_TABLES: Dict[str, str] = {
    FIELD_LOG_ENTRY_FORM_TYPE: "field_log_records",
    FIELD_LOG_CYCLE_FORM_TYPE: "field_log_cycle_sheets",
    FIELD_LOG_PHENOLOGY_FORM_TYPE: "field_log_phenology_records",
}


def is_field_log_form_type(value: Any) -> bool:
    return str(value or "") in FIELD_LOG_FORM_TYPES


def table_for_form_type(form_type: str) -> str:
    return FIELD_LOG_TABLES[str(form_type)]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_key(value: Any) -> str:
    """Forma comparable de un nombre: sin acentos, mayúsculas ni separadores."""
    replacements = str.maketrans("áéíóúüñ", "aeiouun")
    return "".join(ch for ch in _text(value).lower().translate(replacements) if ch.isalnum())


def _first(row: Dict[str, Any], aliases: Sequence[str], default: Any = "") -> Any:
    normalized = {normalize_key(key): value for key, value in row.items() if not str(key).startswith("_")}
    for alias in aliases:
        key = normalize_key(alias)
        value = normalized.get(key)
        if value not in (None, ""):
            return value
    return default


def to_number(value: Any) -> Optional[float]:
    """Número tolerante con lo que teclea un técnico en el móvil.

    En campo se escribe «1,250.50», «1.250,50 $», «12 ha» o « » y las tres
    primeras son cantidades reales. Devuelve None solo cuando de verdad no hay
    número, para poder distinguir «no lo capturó» de «capturó cero».
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = _text(value)
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".,-")
    if not cleaned or cleaned in {"-", ".", ","}:
        return None
    if "," in cleaned and "." in cleaned:
        # El separador decimal es el último que aparece.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Una sola coma: decimal si deja 1 o 2 cifras detrás, millares si no.
        head, _, tail = cleaned.rpartition(",")
        cleaned = f"{head}.{tail}" if len(tail) in (1, 2) else cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _number_or_zero(value: Any) -> float:
    number = to_number(value)
    return 0.0 if number is None else number


# ── Categorías ────────────────────────────────────────────────────────────────
# El desplegable de AgtechApps se rellenó con las etiquetas exactas de la hoja
# ("1. Acondicionamiento"), pero nadie garantiza que siga así dentro de un año:
# el cruce acepta el número solo, la etiqueta completa, la clave interna y los
# sinónimos con los que un técnico escribiría la misma categoría.
_CATEGORY_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "acondicionamiento": ("acondicionamiento", "preparacion", "preparaciondeterreno", "labranza"),
    "siembra": ("siembra", "plantacion", "resiembra"),
    "riego": ("riego", "riegos", "irrigacion"),
    "fertilizante": ("fertilizante", "fertilizacion", "nutricion", "fertilizantes"),
    "malezas": ("malezas", "manejodemalezas", "maleza", "herbicida", "controldemalezas"),
    "plagas": ("plagas", "manejodeplagas", "plaga", "insecticida", "controldeplagas"),
    "enfermedades": ("enfermedades", "manejodeenfermedades", "enfermedad", "fungicida"),
    "foliar": ("foliar", "aplicacionesfoliares", "aplicacionfoliar", "foliares"),
    "diversos": ("diversos", "otros", "varios", "diverso"),
    "cosecha": ("cosecha", "recoleccion", "corte"),
}

_CATEGORY_BY_NUMBER: Dict[str, str] = {
    str(index + 1): key for index, key in enumerate(LOG_CATEGORIES)
}

_CATEGORY_LOOKUP: Dict[str, str] = {}
for _key in LOG_CATEGORIES:
    _CATEGORY_LOOKUP[normalize_key(_key)] = _key
    _CATEGORY_LOOKUP[normalize_key(CATEGORY_LABELS[_key])] = _key
    for _synonym in _CATEGORY_SYNONYMS[_key]:
        _CATEGORY_LOOKUP[normalize_key(_synonym)] = _key


def normalize_category(value: Any) -> Optional[str]:
    """Clave interna de la categoría, o None si no se reconoce.

    Devolver None y no una categoría cualquiera es deliberado: una labor mal
    clasificada ensucia el resumen de costos sin que nadie lo note, mientras que
    una labor sin categoría se puede listar y corregir.
    """
    raw = _text(value)
    if not raw:
        return None
    normalized = normalize_key(raw)
    if normalized in _CATEGORY_LOOKUP:
        return _CATEGORY_LOOKUP[normalized]
    # "1. Acondicionamiento", "1", "1-acondicionamiento": el número manda.
    digits = ""
    for char in raw.strip():
        if char.isdigit():
            digits += char
        else:
            break
    if digits and digits in _CATEGORY_BY_NUMBER:
        return _CATEGORY_BY_NUMBER[digits]
    # Última oportunidad: la etiqueta contiene el nombre de la categoría.
    for candidate, key in _CATEGORY_LOOKUP.items():
        if len(candidate) >= 6 and candidate in normalized:
            return key
    return None


# ── Clave de ciclo ────────────────────────────────────────────────────────────
CYCLE_ALIASES = ("Ciclo", "CicloAgricola", "Ciclo Agricola")
VALIDATION_ALIASES = ("Validacion", "Validación", "NoValidacion", "Folio")
PARCEL_ALIASES = ("Parcela", "Parcela / Lote", "ParcelaLote", "Lote", "Predio")
SECTOR_ALIASES = ("Sector", "Zona", "Bloque")
COMPANY_ALIASES = ("EmpresaSolicitante", "Empresa solicitante", "Empresa", "Cliente")


def cycle_identity(row: Dict[str, Any]) -> Dict[str, str]:
    return {
        "ciclo": _text(_first(row, CYCLE_ALIASES)),
        "validacion": _text(_first(row, VALIDATION_ALIASES)),
        "parcela": _text(_first(row, PARCEL_ALIASES)),
        "sector": _text(_first(row, SECTOR_ALIASES)),
        "empresa_solicitante": _text(_first(row, COMPANY_ALIASES)),
    }


def cycle_key(row: Dict[str, Any]) -> str:
    """Identificador estable de la bitácora a la que pertenece una respuesta.

    Las tres hojas se atan por el mismo trío que el CDT escribe en la cabecera
    del papel. Se normaliza para que «Ciclo P-V 2026» y «ciclo pv 2026» no
    generen dos bitácoras distintas.
    """
    identity = cycle_identity(row)
    return "|".join(
        normalize_key(identity[field]) for field in ("ciclo", "validacion", "parcela")
    )


def cycle_label(identity: Dict[str, Any]) -> str:
    parts = [
        _text(identity.get("ciclo")),
        _text(identity.get("validacion")),
        _text(identity.get("parcela")),
    ]
    visible = [part for part in parts if part]
    return " · ".join(visible) if visible else "Bitácora sin identificar"


# ── Traducción de respuestas ──────────────────────────────────────────────────
def entry_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Una labor de la bitácora, con su costo ya calculado.

    `costo_ha = cantidad × costo unitario` es la columna G de la hoja, la que
    alimenta todo lo demás. Se calcula al aterrizar y no al consultar para que
    el número que ve el usuario sea el mismo que el que suma el resumen, aunque
    la respuesta cambie de forma más adelante.
    """
    identity = cycle_identity(row)
    quantity = to_number(_first(row, ("Cantidad", "Qty", "Cant")))
    unit_cost = to_number(_first(row, ("CostoUnitario", "Costo unitario", "CU", "PrecioUnitario")))
    category = normalize_category(_first(row, ("Categoria", "Categoria de la labor", "CategoriaLabor")))

    data: Dict[str, Any] = {}
    for target, aliases in (
        ("diesel_l", ("LitrosDiesel", "Litros de diesel", "Diesel", "LDiesel")),
        ("kwh", ("KwhRiego", "Kwh por riego", "Kwh", "KwhHa")),
        ("m3", ("M3Riego", "M3 por riego", "M3", "MetrosCubicos")),
        ("n_units", ("UnidadesN", "Unidades N", "FormulaN", "Nitrogeno")),
        ("p_units", ("UnidadesP", "Unidades P", "FormulaP", "Fosforo")),
        ("k_units", ("UnidadesK", "Unidades K", "FormulaK", "Potasio")),
        ("ia_grams", ("GramosIA", "Gramos de i.a.", "GramosIngredienteActivo", "IA")),
        ("aplicacion", ("Aplicacion", "NumeroAplicacion", "Aplicación")),
        ("rendimiento_ton_ha", ("Rendimiento", "RendimientoTonHa", "Rendimiento obtenido")),
    ):
        value = to_number(_first(row, aliases, None))
        if value is not None:
            data[target] = value

    return {
        **identity,
        "cycle_key": cycle_key(row),
        "categoria": category,
        "categoria_label": CATEGORY_LABELS[category] if category else _text(
            _first(row, ("Categoria", "Categoria de la labor"))
        ),
        "categoria_reconocida": category is not None,
        "concepto": _text(_first(row, ("Concepto", "Concepto realizado", "Descripcion", "Labor"))),
        "unidad": _text(_first(row, ("Unidades", "Unidad", "UM"))),
        "cantidad": quantity,
        "costo_unitario": unit_cost,
        "costo_ha": (quantity or 0.0) * (unit_cost or 0.0),
        "operador": _text(_first(row, ("Operador", "Responsable", "Terminal"))),
        "fecha": _text(_first(row, ("Fecha", "Fecha de realizacion", "FechaRealizacion"))),
        "hora": _text(_first(row, ("Hora",))),
        "observaciones": _text(_first(row, ("Observaciones", "Notas", "Comentarios"))),
        "data": data,
    }


def cycle_sheet_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Ficha del ciclo: lo que en la hoja está en la cabecera y en los pies."""
    identity = cycle_identity(row)
    attributes: Dict[str, Any] = {}
    for target, aliases in (
        ("camas", ("NumeroCamas", "Numero de camas", "Camas")),
        ("densidad_siembra", ("DensidadSiembra", "Densidad de siembra")),
        ("densidad_poblacion", ("DensidadPoblacion", "Densidad de poblacion")),
        ("cobertura_pct", ("PorcentajeCobertura", "Porcentaje de cobertura", "Cobertura")),
        ("formula_n", ("FormulaN", "Formula - Nitrogeno")),
        ("formula_p", ("FormulaP", "Formula - Fosforo")),
        ("formula_k", ("FormulaK", "Formula - Potasio")),
    ):
        value = to_number(_first(row, aliases, None))
        if value is not None:
            attributes[target] = value
    for target, aliases in (
        ("hibrido", ("Hibrido", "Hibrido / variedad", "Variedad")),
        ("tipo_labranza", ("TipoLabranza", "Tipo de labranza", "Labranza")),
    ):
        text_value = _text(_first(row, aliases))
        if text_value:
            attributes[target] = text_value

    return {
        **identity,
        "cycle_key": cycle_key(row),
        "superficie_ha": to_number(_first(row, ("Superficie", "Superficie (ha)", "SuperficieHa", "Area"), None)),
        "cultivo": _text(_first(row, ("Cultivo", "Crop"))),
        "rendimiento_ton_ha": to_number(_first(row, ("Rendimiento", "RendimientoTonHa"), None)),
        "precio_por_ton": to_number(_first(row, ("PrecioVenta", "Precio de venta", "Precio"), None)),
        "observaciones": _text(_first(row, ("Observaciones", "Notas"))),
        "attributes": attributes,
    }


def phenology_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    identity = cycle_identity(row)
    return {
        **identity,
        "cycle_key": cycle_key(row),
        "etapa": _text(_first(row, ("Etapa", "Etapa fenologica", "EtapaFenologica", "Estadio"))),
        "fecha": _text(_first(row, ("Fecha", "Fecha de observacion", "FechaObservacion"))),
        "observaciones": _text(_first(row, ("Observaciones", "Notas"))),
    }


ROW_PARSERS = {
    FIELD_LOG_ENTRY_FORM_TYPE: entry_from_row,
    FIELD_LOG_CYCLE_FORM_TYPE: cycle_sheet_from_row,
    FIELD_LOG_PHENOLOGY_FORM_TYPE: phenology_from_row,
}


def parse_row(form_type: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return ROW_PARSERS[str(form_type)](row)


# ── Cálculo ───────────────────────────────────────────────────────────────────
def _snapshots(entries: Iterable[Dict[str, Any]]) -> List[field_log_kpi.EntrySnapshot]:
    """Traduce las labores guardadas a lo que esperan las funciones del Excel.

    Las labores cuya categoría no se reconoció quedan fuera: sumar su costo en
    un bloque que no les corresponde falsearía el resumen. Se cuentan aparte
    para que la vista pueda avisar de que hay algo que revisar.
    """
    snapshots: List[field_log_kpi.EntrySnapshot] = []
    for entry in entries:
        category = entry.get("categoria")
        if category not in LOG_CATEGORIES:
            continue
        snapshots.append(
            field_log_kpi.EntrySnapshot(
                category=str(category),
                cost_per_ha=_number_or_zero(entry.get("costo_ha")),
                data=dict(entry.get("data") or {}),
            )
        )
    return snapshots


def _cycle_yield_and_price(
    sheet: Optional[Dict[str, Any]],
    entries: Sequence[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    """Rendimiento y precio del ciclo, con la ficha por delante de las labores.

    La ficha de validación es donde el técnico cierra el ciclo (B5 y D5 de la
    hoja). Si todavía no la ha llenado, el rendimiento se puede reconstruir
    sumando lo que declararon las labores de cosecha, que es mejor que dejar
    todos los indicadores por tonelada en blanco.
    """
    yield_ton_ha = (sheet or {}).get("rendimiento_ton_ha")
    price = (sheet or {}).get("precio_por_ton")
    if yield_ton_ha in (None, ""):
        harvested = sum(
            _number_or_zero((entry.get("data") or {}).get("rendimiento_ton_ha"))
            for entry in entries
            if entry.get("categoria") == "cosecha"
        )
        yield_ton_ha = harvested or None
    return (
        to_number(yield_ton_ha) if yield_ton_ha not in (None, "") else None,
        to_number(price) if price not in (None, "") else None,
    )


def cycle_report(
    *,
    identity: Dict[str, Any],
    sheet: Optional[Dict[str, Any]],
    entries: Sequence[Dict[str, Any]],
    phenology: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Todo lo que la hoja calculaba, para una bitácora.

    Es el corazón del módulo: las respuestas llegan planas desde AgtechApps y
    salen de aquí con la cabecera económica, los resultados técnicos, el resumen
    de costos por bloque y la matriz de sensibilidad, sin que nadie haya tocado
    una fórmula.
    """
    snapshots = _snapshots(entries)
    yield_ton_ha, price_per_ton = _cycle_yield_and_price(sheet, entries)
    kpis = field_log_kpi.compute_kpis(
        snapshots,
        yield_ton_ha=yield_ton_ha,
        price_per_ton=price_per_ton,
    )
    matrix = field_log_sensitivity.build_matrix(
        investment_per_ha=kpis["economics"]["investment_per_ha"],
        yield_ton_ha=yield_ton_ha,
        price_per_ton=price_per_ton,
    )
    unclassified = [entry for entry in entries if entry.get("categoria") not in LOG_CATEGORIES]
    located = [entry for entry in entries if entry.get("lat") is not None and entry.get("lng") is not None]

    return {
        "cycle_key": identity.get("cycle_key"),
        "label": cycle_label(identity),
        "ciclo": identity.get("ciclo"),
        "validacion": identity.get("validacion"),
        "parcela": identity.get("parcela"),
        "sector": identity.get("sector"),
        "empresa_solicitante": identity.get("empresa_solicitante"),
        "parcel_id": identity.get("parcel_id"),
        "cultivo": (sheet or {}).get("cultivo") or "",
        "superficie_ha": (sheet or {}).get("superficie_ha"),
        "attributes": dict((sheet or {}).get("attributes") or {}),
        "has_cycle_sheet": bool(sheet),
        "kpis": kpis,
        "sensitivity": matrix,
        "entry_count": len(entries),
        "located_entry_count": len(located),
        "unclassified_entries": [
            {
                "id": entry.get("id"),
                "concepto": entry.get("concepto"),
                "categoria_label": entry.get("categoria_label"),
                "costo_ha": entry.get("costo_ha"),
            }
            for entry in unclassified
        ],
        "phenology": sorted(
            (
                {
                    "id": item.get("id"),
                    "etapa": item.get("etapa"),
                    "fecha": item.get("fecha"),
                    "observaciones": item.get("observaciones"),
                    "lat": item.get("lat"),
                    "lng": item.get("lng"),
                }
                for item in phenology
            ),
            key=lambda item: str(item.get("fecha") or ""),
        ),
        "last_entry_at": max((str(entry.get("fecha") or "") for entry in entries), default=""),
    }


def group_by_cycle(
    *,
    entries: Sequence[Dict[str, Any]],
    sheets: Sequence[Dict[str, Any]],
    phenology: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Arma un informe por bitácora a partir de las tres tablas."""
    keys: List[str] = []
    identities: Dict[str, Dict[str, Any]] = {}

    def register(row: Dict[str, Any]) -> str:
        key = str(row.get("cycle_key") or "")
        if key not in identities:
            identities[key] = {
                "cycle_key": key,
                "ciclo": row.get("ciclo"),
                "validacion": row.get("validacion"),
                "parcela": row.get("parcela"),
                "sector": row.get("sector"),
                "empresa_solicitante": row.get("empresa_solicitante"),
                "parcel_id": row.get("parcel_id"),
            }
            keys.append(key)
        elif not identities[key].get("parcel_id") and row.get("parcel_id"):
            identities[key]["parcel_id"] = row.get("parcel_id")
        return key

    by_entries: Dict[str, List[Dict[str, Any]]] = {}
    for row in entries:
        by_entries.setdefault(register(row), []).append(row)

    by_sheet: Dict[str, Dict[str, Any]] = {}
    for row in sheets:
        key = register(row)
        current = by_sheet.get(key)
        # Si el técnico llenó la ficha dos veces, manda la más reciente: es una
        # corrección, no un segundo ciclo.
        if current is None or str(row.get("updated_at") or "") >= str(current.get("updated_at") or ""):
            by_sheet[key] = row

    by_phenology: Dict[str, List[Dict[str, Any]]] = {}
    for row in phenology:
        by_phenology.setdefault(register(row), []).append(row)

    reports = [
        cycle_report(
            identity=identities[key],
            sheet=by_sheet.get(key),
            entries=by_entries.get(key, []),
            phenology=by_phenology.get(key, []),
        )
        for key in keys
    ]
    reports.sort(key=lambda item: (item.get("last_entry_at") or "", item.get("label") or ""), reverse=True)
    return reports
