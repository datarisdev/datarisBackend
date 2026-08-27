"""Vocabulario de la bitácora: categorías, estados y orígenes.

Vivían en `models.py`, junto a las tablas SQLAlchemy. Se separan porque los
cálculos (`kpi.py`, `sensitivity.py`) y el conector de AgtechApps solo necesitan
estas listas, y arrastrar el ORM para leer diez cadenas obliga a importar toda
la capa de modelos —lo que además da un import circular cuando el primero en
cargarse es el módulo de bitácora y no `app.models`.

`models.py` las reexporta, así que nada de lo que ya las importaba de allí
cambia.
"""

from __future__ import annotations

# Las diez categorías de la bitácora, en el orden en el que se presentan y se
# suman. El orden importa: es el del resumen de costos y el de la exportación.
LOG_CATEGORIES: tuple[str, ...] = (
    "acondicionamiento",
    "siembra",
    "riego",
    "fertilizante",
    "malezas",
    "plagas",
    "enfermedades",
    "foliar",
    "diversos",
    "cosecha",
)

CATEGORY_LABELS: dict[str, str] = {
    "acondicionamiento": "1. Acondicionamiento",
    "siembra": "2. Siembra",
    "riego": "3. Riegos",
    "fertilizante": "4. Fertilizante",
    "malezas": "5. Manejo de malezas",
    "plagas": "6. Manejo de plagas",
    "enfermedades": "7. Manejo de enfermedades",
    "foliar": "8. Aplicaciones foliares",
    "diversos": "9. Diversos",
    "cosecha": "10. Cosecha",
}

CYCLE_STATUSES: tuple[str, ...] = ("planning", "active", "closed")

ENTRY_SOURCES: tuple[str, ...] = ("mobile", "web", "digiforms", "import", "harvest")
