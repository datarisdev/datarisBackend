"""Catálogo canónico de módulos de la plataforma.

Un módulo NO es una fila que alguien pueda inventar desde el panel: es código
(una ruta, una pantalla, un guardián `requiredModuleId`). Antes el panel de
administración permitía crear filas nuevas en `platform_modules`, que nacían con
un `id` UUID sin ninguna ruta detrás — se activaban y no aparecía nada — y
borrar las de siempre, que `normalize_db()` volvía a sembrar en el siguiente
arranque.

Este módulo es la única fuente de verdad de qué módulos existen y, sobre todo,
de **dónde se ven**: el panel necesita poder decirle al operador que "Mapeo" no
tiene entrada propia en el menú lateral (se abre desde el Centro de control) o
que "Dashboard" no se puede quitar. Sin ese dato, activar un módulo y no verlo
en el menú parece un fallo cuando es el comportamiento esperado.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Categorías: qué clase de acceso es cada fila del catálogo.
CATEGORY_CORE = "core"            # módulo del producto, se asigna por empresa/usuario
CATEGORY_EXTENSION = "extension"  # extensión externa, se gestiona en /admin/extensions
CATEGORY_INTERNAL = "internal"    # herramienta interna de Dataris, nunca para clientes

# Superficies: dónde aparece el módulo cuando está activo.
SURFACE_SYSTEM = "system"      # siempre concedido; no es desasignable
SURFACE_MENU = "menu"          # tiene su propia entrada en el menú lateral
SURFACE_EMBEDDED = "embedded"  # sin entrada propia: se abre desde otras pantallas
SURFACE_INTERNAL = "internal"  # solo para el equipo de Dataris


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name: str
    description: str
    icon: str
    category: str
    surface: str
    # Frase que el panel muestra al operador para que sepa qué esperar tras
    # activar el módulo.
    surface_hint: str
    routes: Tuple[str, ...] = ()

    @property
    def assignable(self) -> bool:
        """¿Tiene sentido activarlo/desactivarlo por empresa o por usuario?"""
        return self.category == CATEGORY_CORE and self.surface != SURFACE_SYSTEM


MODULE_SPECS: Tuple[ModuleSpec, ...] = (
    ModuleSpec(
        id="dashboard",
        name="Dashboard",
        description="Centro de control: portada de la plataforma con el resumen operativo.",
        icon="LayoutDashboard",
        category=CATEGORY_CORE,
        surface=SURFACE_SYSTEM,
        surface_hint="Siempre disponible. Es la portada de la plataforma y no se puede retirar a nadie.",
        routes=("/dashboard",),
    ),
    ModuleSpec(
        id="satelite",
        name="Monitoreo Satelital",
        description="Análisis satelital de los lotes: índices, fechas y comparativas.",
        icon="Satellite",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Cartografía › Monitoreo satelital.",
        routes=("/satelite",),
    ),
    ModuleSpec(
        id="mapeo",
        name="Mapeo",
        description="Mapeo y análisis geoespacial de las geometrías cargadas.",
        icon="Map",
        category=CATEGORY_CORE,
        surface=SURFACE_EMBEDDED,
        surface_hint="Sin entrada propia en el menú: se abre desde el Centro de control y habilita la Zona de Análisis.",
        routes=("/mapeo",),
    ),
    ModuleSpec(
        id="telemetria",
        name="Telemetría",
        description="Indicadores y métricas de maquinaria en campo.",
        icon="Activity",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Maquinaria › Telemetría.",
        routes=("/telemetria", "/cosecha-mecanica"),
    ),
    ModuleSpec(
        id="ortofoto-analysis",
        name="Análisis de ortofotos",
        description="Procesamiento visual de ortomosaicos de dron.",
        icon="Image",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Maquinaria › Análisis de ortofotos.",
        routes=("/ortofoto-analysis",),
    ),
    ModuleSpec(
        id="sig-agricola",
        name="SIG Agrícola",
        description="Análisis agrícola: cosecha, plagas y malezas por lote.",
        icon="Sprout",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Cartografía › SIG agrícola.",
        routes=("/sig-agricola",),
    ),
    ModuleSpec(
        id="aplicaciones-aereas",
        name="Aplicaciones Aéreas",
        description="Control de aplicaciones con dron, helicóptero y avioneta.",
        icon="Plane",
        category=CATEGORY_CORE,
        surface=SURFACE_EMBEDDED,
        surface_hint="Sin entrada propia en el menú: se abre desde Telemetría y habilita la Zona de Análisis.",
        routes=("/aplicaciones-aereas", "/drones"),
    ),
    ModuleSpec(
        id="personal",
        name="Personal de Campo",
        description="Control biométrico y georreferenciado del personal.",
        icon="Users",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Producción › Personal de campo.",
        routes=("/personal",),
    ),
    ModuleSpec(
        id="alertas",
        name="Alertas inteligentes",
        description="Detección proactiva de riesgos operativos.",
        icon="Bell",
        category=CATEGORY_CORE,
        surface=SURFACE_MENU,
        surface_hint="Menú lateral › Alertas › Alertas operativas.",
        routes=("/alertas",),
    ),
    ModuleSpec(
        id="digiforms",
        name="DigiformsApp",
        description="Formularios digitales de campo, captura offline, GPS, fotos y reportes desde DigiformsApp.",
        icon="FileText",
        category=CATEGORY_EXTENSION,
        surface=SURFACE_MENU,
        surface_hint="Extensión: se concede desde Extensiones o aprobando la solicitud del cliente.",
        routes=("/digiforms",),
    ),
    ModuleSpec(
        id="graniot",
        name="Graniot",
        description="Integración para capas satelitales, NDVI, fechas, estadísticas y sincronización de lotes desde Graniot.",
        icon="Leaf",
        category=CATEGORY_EXTENSION,
        surface=SURFACE_EMBEDDED,
        surface_hint="Extensión: alimenta las capas satelitales de la Zona de Análisis, sin entrada propia.",
        routes=(),
    ),
    ModuleSpec(
        id="ml-training",
        name="Laboratorio de IA",
        description="Entrenamiento de modelos con datos de la plataforma. Herramienta interna de Dataris.",
        icon="Brain",
        category=CATEGORY_INTERNAL,
        surface=SURFACE_INTERNAL,
        surface_hint="Interno de Dataris: nunca se concede a una empresa cliente ni a la cuenta demo.",
        routes=("/laboratorio-ia",),
    ),
)

SPECS_BY_ID: Dict[str, ModuleSpec] = {spec.id: spec for spec in MODULE_SPECS}

# Herramientas internas de Dataris: nunca se conceden a una empresa cliente ni a
# la cuenta demo, esté como esté configurado el catálogo.
INTERNAL_ONLY_MODULE_IDS = frozenset(
    spec.id for spec in MODULE_SPECS if spec.category == CATEGORY_INTERNAL
)

# Módulos derivados: no son filas del catálogo, se habilitan solos cuando el
# usuario tiene alguno de los módulos de los que dependen. Se listan aquí para
# que el panel pueda explicarlo en vez de dejar al operador buscando el switch.
DERIVED_MODULES = (
    {
        "id": "work-area",
        "name": "Zona de Análisis",
        "depends_on": ("satelite", "aplicaciones-aereas", "mapeo", "telemetria", "ortofoto-analysis"),
        "surface_hint": "Menú lateral › Cartografía › Zona de Análisis. Aparece sola con cualquiera de sus módulos base.",
    },
)

# Alias históricos de ids de módulo (los datos vienen de varias épocas del
# producto). Se resuelven al id canónico del catálogo.
MODULE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sig-agricola": ("sig-agricola", "sig_agricola", "sig"),
    "aplicaciones-aereas": ("aplicaciones-aereas", "aplicaciones_aereas", "drones", "drone", "helicoptero", "helicopter", "avioneta"),
    "ortofoto-analysis": ("ortofoto-analysis", "ortofoto_analysis", "ortofotos", "analisis-ortofotos", "analisis_de_ortofotos"),
    "satelite": ("satelite", "satellite", "satélite"),
    "telemetria": ("telemetria", "telemetría", "telemetry"),
    "digiforms": ("digiforms", "digiformsapp", "digiforms-app"),
    "graniot": ("graniot",),
    "ml-training": ("ml-training", "ml_training", "laboratorio-ia", "laboratorio_ia"),
}


def normalize_module_id(value: object) -> str:
    raw = str(value or "").strip().lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        raw = raw.replace(src, dst)
    return raw.replace("_", "-").replace(" ", "-")


def canonical_module_id(value: object) -> str:
    """Id de catálogo para cualquier alias histórico; el normalizado si no lo hay."""
    normalized = normalize_module_id(value)
    if not normalized:
        return ""
    if normalized in SPECS_BY_ID:
        return normalized
    for canonical, aliases in MODULE_ALIASES.items():
        if normalized in {normalize_module_id(alias) for alias in aliases}:
            return canonical
    return normalized


def spec_for(value: object) -> Optional[ModuleSpec]:
    return SPECS_BY_ID.get(canonical_module_id(value))


def core_specs() -> List[ModuleSpec]:
    return [spec for spec in MODULE_SPECS if spec.category == CATEGORY_CORE]


def extension_specs() -> List[ModuleSpec]:
    return [spec for spec in MODULE_SPECS if spec.category == CATEGORY_EXTENSION]


def assignable_module_ids() -> List[str]:
    return [spec.id for spec in MODULE_SPECS if spec.assignable]


def default_catalog_rows() -> List[Tuple[str, str, str, str]]:
    """Semilla de `platform_modules` para los módulos core (id, nombre, descripción, icono)."""
    return [(spec.id, spec.name, spec.description, spec.icon) for spec in core_specs()]
