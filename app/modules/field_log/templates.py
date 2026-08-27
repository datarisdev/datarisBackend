"""Plantillas de bitácora.

Una plantilla decide qué categorías se muestran, qué campos extra tiene cada
una, qué escala fenológica aplica y con qué rendimientos de labor se compara.
Es lo que permite que el mismo módulo sirva a un centro de investigación de
maíz y a un productor de caña sin cablear el formato de ninguno de los dos.

Las plantillas del sistema viven aquí y no en la base de datos: así se corrigen
con un despliegue, no con una migración de datos, y no se desincronizan entre
entornos. Las que crea un usuario sí se guardan (`field_log_templates`).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.modules.field_log.catalog import CATEGORY_LABELS, LOG_CATEGORIES

# Campos comunes a todas las categorías. Los declara la plantilla igual que los
# específicos para que el formulario del móvil se construya de una sola fuente.
COMMON_FIELDS: list[dict[str, Any]] = [
    {"name": "quantity", "label": "Cantidad", "type": "number", "core": True},
    {"name": "unit", "label": "Unidad", "type": "text", "core": True},
    {"name": "unit_cost", "label": "Costo unitario", "type": "currency", "core": True},
]


def _stage(code: str, label: str) -> dict[str, str]:
    return {"code": code, "label": label}


# Escala fenológica del maíz tal cual aparece en la hoja del CDT.
MAIZE_STAGES: list[dict[str, str]] = [
    _stage("germinacion", "Germinación"),
    *[_stage(f"v{index}", f"V{index}") for index in range(1, 12)],
    _stage("r0", "R0 — Floración"),
    _stage("r1", "R1 — Grano acuoso"),
    _stage("r2", "R2 — Grano lechoso"),
    _stage("r3", "R3 — Grano masoso"),
]

SUGARCANE_STAGES: list[dict[str, str]] = [
    _stage("germinacion", "Germinación"),
    _stage("ahijamiento", "Ahijamiento"),
    _stage("gran_crecimiento", "Gran crecimiento"),
    _stage("maduracion", "Maduración"),
    _stage("cosecha", "Cosecha"),
]

GENERIC_STAGES: list[dict[str, str]] = [
    _stage("emergencia", "Emergencia"),
    _stage("desarrollo_vegetativo", "Desarrollo vegetativo"),
    _stage("floracion", "Floración"),
    _stage("llenado", "Llenado de fruto/grano"),
    _stage("madurez", "Madurez"),
]

# Horas por hectárea de la tabla de la hoja original.
DEFAULT_LABOR_STANDARDS: list[dict[str, Any]] = [
    {"labor_name": "Subsoleo", "category": "acondicionamiento", "hours_per_ha": 2.15},
    {"labor_name": "Rastreo", "category": "acondicionamiento", "hours_per_ha": 0.65},
    {"labor_name": "Remarcar", "category": "acondicionamiento", "hours_per_ha": 1.05},
    {"labor_name": "Siembra", "category": "siembra", "hours_per_ha": 3.9},
    {"labor_name": "Desmenuzado", "category": "acondicionamiento", "hours_per_ha": 1.45},
    {"labor_name": "Emparejar", "category": "acondicionamiento", "hours_per_ha": 0.58},
    {"labor_name": "Aplicación foliar", "category": "foliar", "hours_per_ha": 1.2},
]

# Columnas que la hoja añade a los renglones de cada bloque. Son datos de la
# labor concreta —el riego del 12 de mayo consumió tantos kWh— y sin ellos los
# indicadores de sostenibilidad no se pueden calcular.
_EXTRA_FIELDS: dict[str, list[dict[str, Any]]] = {
    "acondicionamiento": [
        {"name": "diesel_l", "label": "L de diesel", "type": "number", "unit": "L"},
    ],
    "siembra": [
        {"name": "diesel_l", "label": "L de diesel", "type": "number", "unit": "L"},
    ],
    "riego": [
        {"name": "kwh", "label": "Kwh/riego", "type": "number", "unit": "kWh"},
        {"name": "m3", "label": "M³/riego", "type": "number", "unit": "m³"},
        {"name": "horas_riego", "label": "Horas de riego", "type": "number", "unit": "h"},
    ],
    "fertilizante": [
        {"name": "n_units", "label": "Unidades N", "type": "number"},
        {"name": "p_units", "label": "Unidades P", "type": "number"},
        {"name": "k_units", "label": "Unidades K", "type": "number"},
    ],
    "malezas": [
        {
            "name": "ia_grams",
            "label": "g de i.a./L ó Kg",
            "type": "number",
            "unit": "g de i.a.",
        },
    ],
    "plagas": [
        {
            "name": "ia_grams",
            "label": "g de i.a./L ó Kg",
            "type": "number",
            "unit": "g de i.a.",
        },
        {"name": "plaga", "label": "Plaga objetivo", "type": "text"},
    ],
    "enfermedades": [
        {
            "name": "ia_grams",
            "label": "g de i.a./L ó Kg",
            "type": "number",
            "unit": "g de i.a.",
        },
        {"name": "enfermedad", "label": "Enfermedad objetivo", "type": "text"},
    ],
    # La hoja parte el bloque foliar en "Aplicación 1" y "Aplicación 2", cada una
    # con su subtotal. Aquí el número de aplicación es un campo y los subtotales
    # se agrupan por él, así que no hay tope de dos.
    "foliar": [
        {"name": "aplicacion", "label": "Número de aplicación", "type": "number"},
    ],
    "diversos": [],
    "cosecha": [
        {
            "name": "rendimiento_ton_ha",
            "label": "Rendimiento obtenido",
            "type": "number",
            "unit": "ton/ha",
        },
        {"name": "humedad_pct", "label": "Humedad", "type": "number", "unit": "%"},
    ],
}

# Datos que la hoja escribe al pie de un bloque y que describen el ciclo, no la
# labor: el tipo de labranza se decide una vez, no en cada paso de rastra. Antes
# estaban como columnas de cada renglón, lo que obligaba a repetir el híbrido en
# las seis líneas de siembra y dejaba seis respuestas distintas a la misma
# pregunta. Se guardan en `crop_cycles.attributes`.
_BLOCK_ATTRIBUTES: dict[str, list[dict[str, Any]]] = {
    "acondicionamiento": [
        {
            "name": "tipo_labranza",
            "label": "Tipo de labranza",
            "type": "select",
            "options": ["Convencional", "Conservación", "Cero labranza", "Mínima"],
        },
        {
            "name": "cobertura_pct",
            "label": "Porcentaje de cobertura",
            "type": "number",
            "unit": "%",
        },
    ],
    "siembra": [
        {"name": "camas", "label": "Número de camas", "type": "number"},
        {"name": "hibrido", "label": "Híbrido/variedad", "type": "text"},
        {
            "name": "densidad_siembra",
            "label": "Densidad de siembra",
            "type": "number",
            "unit": "sem ó Kg/ha",
        },
        {
            "name": "densidad_poblacion",
            "label": "Densidad de población",
            "type": "number",
            "unit": "pl/ha",
        },
    ],
}

# Subtotales que la hoja calcula al cierre de cada bloque. Se declaran aquí —y
# no en el componente— porque son parte del formato: la plantilla de caña cierra
# sus bloques con otros números. Las claves las resuelve `kpi.block_totals`.
_BLOCK_TOTALS: dict[str, list[dict[str, Any]]] = {
    "acondicionamiento": [
        {"key": "diesel_l_per_ha", "label": "Litros de diesel/ha", "decimals": 2},
    ],
    "siembra": [
        {"key": "diesel_l_per_ha", "label": "Litros de diesel/ha", "decimals": 2},
    ],
    "riego": [
        {"key": "kwh_per_ha", "label": "Kwh/ha", "decimals": 2},
        {"key": "m3_per_ha", "label": "M cub/ha", "decimals": 2},
    ],
    "fertilizante": [
        {"key": "n_units", "label": "Nitrógeno", "decimals": 2},
        {"key": "p_units", "label": "Fósforo", "decimals": 2},
        {"key": "k_units", "label": "Potasio", "decimals": 2},
    ],
    "malezas": [{"key": "ia_grams", "label": "Total gramos de i.a.", "decimals": 2}],
    "plagas": [{"key": "ia_grams", "label": "Total gramos de i.a.", "decimals": 2}],
    "enfermedades": [{"key": "ia_grams", "label": "Total gramos de i.a.", "decimals": 2}],
    "cosecha": [
        {"key": "rendimiento_ton_ha", "label": "Rendimiento", "decimals": 2},
    ],
}

# Atributos del ciclo que no cuelgan de ningún bloque. Hoy todos los que trae la
# hoja pertenecen a uno, así que la lista queda vacía y existe para las
# plantillas que un usuario arme por su cuenta.
CYCLE_ATTRIBUTE_FIELDS: list[dict[str, Any]] = []


def _categories(only: list[str] | None = None) -> list[dict[str, Any]]:
    keys = only or list(LOG_CATEGORIES)
    return [
        {
            "key": key,
            "label": CATEGORY_LABELS[key],
            "fields": deepcopy(_EXTRA_FIELDS.get(key, [])),
            # Cada bloque se abre con sus renglones, se cierra con sus
            # subtotales y, si la hoja los pide, con los datos del ciclo que
            # describen ese bloque. Es el orden del formato en papel.
            "attributes": deepcopy(_BLOCK_ATTRIBUTES.get(key, [])),
            "totals": deepcopy(_BLOCK_TOTALS.get(key, [])),
            "supports_inputs": key in {"fertilizante", "malezas", "plagas", "enfermedades", "foliar"},
            # El bloque foliar de la hoja se subdivide por aplicación, con un
            # subtotal por cada una.
            "group_by": "aplicacion" if key == "foliar" else None,
        }
        for key in keys
    ]


SYSTEM_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "cdt-fira-maiz",
        "name": "CDT FIRA — Maíz",
        "description": (
            "Réplica de la bitácora de ciclo del Centro de Desarrollo Tecnológico: "
            "diez bloques de costos, fenología en escala V/R e indicadores de "
            "huella hídrica, energía e ingrediente activo por tonelada."
        ),
        "crop_type": "Maíz",
        "is_system": True,
        "categories": _categories(),
        "phenology_stages": MAIZE_STAGES,
        "labor_standards": DEFAULT_LABOR_STANDARDS,
        "cycle_attributes": CYCLE_ATTRIBUTE_FIELDS,
    },
    {
        "key": "cana-azucar",
        "name": "Caña de azúcar",
        "description": "Ciclo de caña con etapas propias y bloque de cosecha mecanizada.",
        "crop_type": "Caña de azúcar",
        "is_system": True,
        "categories": _categories(),
        "phenology_stages": SUGARCANE_STAGES,
        "labor_standards": DEFAULT_LABOR_STANDARDS,
        "cycle_attributes": CYCLE_ATTRIBUTE_FIELDS,
    },
    {
        "key": "generica",
        "name": "Genérica",
        "description": "Bitácora estándar para cualquier cultivo.",
        "crop_type": None,
        "is_system": True,
        "categories": _categories(),
        "phenology_stages": GENERIC_STAGES,
        "labor_standards": DEFAULT_LABOR_STANDARDS,
        "cycle_attributes": CYCLE_ATTRIBUTE_FIELDS,
    },
]

DEFAULT_TEMPLATE_KEY = "generica"


def list_system_templates() -> list[dict[str, Any]]:
    return deepcopy(SYSTEM_TEMPLATES)


def get_system_template(key: str | None) -> dict[str, Any]:
    """Plantilla del sistema por clave, con la genérica como último recurso.

    Nunca lanza: una bitácora con la plantilla equivocada se sigue pudiendo
    abrir y editar, solo muestra los campos estándar.
    """
    for template in SYSTEM_TEMPLATES:
        if template["key"] == key:
            return deepcopy(template)
    for template in SYSTEM_TEMPLATES:
        if template["key"] == DEFAULT_TEMPLATE_KEY:
            return deepcopy(template)
    return {
        "key": DEFAULT_TEMPLATE_KEY,
        "name": "Genérica",
        "categories": _categories(),
        "phenology_stages": GENERIC_STAGES,
        "labor_standards": DEFAULT_LABOR_STANDARDS,
        "cycle_attributes": CYCLE_ATTRIBUTE_FIELDS,
    }
