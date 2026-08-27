"""Indicadores derivados de la bitácora.

Funciones puras: no tocan la base de datos ni el ORM, reciben datos planos y
devuelven números. Así se pueden verificar contra la hoja de cálculo original,
que es la única fuente de verdad sobre qué significa cada indicador.

Fórmulas tomadas del archivo `Bitácora de campo.xlsx` (hoja `Registro`):

    ingreso           = rendimiento × precio                      (F5 = B5*D5)
    inversión         = Σ costos de las diez categorías           (H5 = G108)
    utilidad          = ingreso − inversión                       (J5 = F5-H5)
    relación B/C      = ingreso ÷ inversión                       (K5 = F5/H5)
    punto equilibrio  = inversión ÷ precio                        (E7 = H5/D5)
    costo unitario    = inversión ÷ rendimiento                   (K7 = H5/B5)
    huella hídrica    = Σ m³ de riego ÷ rendimiento               (F10 = F50/B5)
    energía           = Σ kWh de riego ÷ rendimiento              (F11 = D50/B5)
    diesel            = litros por hectárea (dato directo)        (F12 = D29)
    insecticida       = Σ g de i.a. de plagas ÷ rendimiento       (F13 = D82/B5)
    herbicida         = Σ g de i.a. de malezas ÷ rendimiento      (F14 = D70/B5)
    fungicida         = Σ g de i.a. de enfermedades ÷ rendimiento (F15 = D88/B5)
    nitrógeno         = Σ unidades de N ÷ rendimiento             (F16 = D58/B5)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.modules.field_log.catalog import CATEGORY_LABELS, LOG_CATEGORIES


@dataclass(frozen=True)
class EntryInputSnapshot:
    """Producto aplicado, reducido a lo que necesitan los indicadores."""

    ia_grams: float | None = None
    n_units: float | None = None
    p_units: float | None = None
    k_units: float | None = None


@dataclass(frozen=True)
class EntrySnapshot:
    """Una labor, reducida a lo que necesitan los indicadores."""

    category: str
    cost_per_ha: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    inputs: Sequence[EntryInputSnapshot] = ()


def _number(value: Any) -> float:
    """Convierte a float tolerando None, cadenas vacías y texto no numérico."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _divide(numerator: float, denominator: float | None) -> float | None:
    """División que devuelve None en lugar del #DIV/0! de la hoja.

    Un indicador sin rendimiento capturado todavía no existe; devolver 0 haría
    creer que la huella hídrica es cero cuando lo que pasa es que aún no se ha
    cosechado.
    """
    if not denominator:
        return None
    return numerator / denominator


def cost_by_category(entries: Iterable[EntrySnapshot]) -> dict[str, float]:
    """Costo por hectárea acumulado en cada categoría, todas presentes."""
    totals = {category: 0.0 for category in LOG_CATEGORIES}
    for entry in entries:
        if entry.category in totals:
            totals[entry.category] += _number(entry.cost_per_ha)
    return totals


def cost_summary(entries: Sequence[EntrySnapshot]) -> dict[str, Any]:
    """Resumen de costos con el porcentaje de cada categoría sobre el total."""
    by_category = cost_by_category(entries)
    total = sum(by_category.values())
    return {
        "total_cost_per_ha": total,
        "categories": [
            {
                "category": category,
                "label": CATEGORY_LABELS[category],
                "cost_per_ha": by_category[category],
                "percentage": (100 * by_category[category] / total) if total else None,
                "entry_count": sum(1 for e in entries if e.category == category),
            }
            for category in LOG_CATEGORIES
        ],
    }


def block_totals(entries: Sequence[EntrySnapshot]) -> dict[str, dict[str, float]]:
    """Pie de cada bloque, tal como lo cierra la hoja.

    Son las mismas sumas que el formato lleva bajo cada sección —`SUM(H20:H26)`
    para el diesel del acondicionamiento, `SUM(H42:H49)` y `SUM(I42:I49)` para
    los kWh y los m³ de los riegos, `SUM(H62:H69)` para los gramos de i.a. de
    malezas, `SUM(H52:H57)` y hermanas para la fórmula N-P-K— pero acotadas al
    bloque, que es lo que hace legible el cierre de cada categoría. Los
    indicadores por tonelada de `sustainability` siguen calculándose aparte
    sobre todo el ciclo.
    """
    totals: dict[str, dict[str, float]] = {category: {} for category in LOG_CATEGORIES}

    for category in LOG_CATEGORIES:
        block = [entry for entry in entries if entry.category == category]
        if not block:
            continue

        values: dict[str, float] = {"cost_per_ha": sum(_number(e.cost_per_ha) for e in block)}
        one = {category}

        diesel = _sum_field(block, one, "diesel_l", "litros_diesel")
        if diesel:
            values["diesel_l_per_ha"] = diesel

        if category == "riego":
            values["kwh_per_ha"] = _sum_field(block, one, "kwh", "kwh_por_riego", "energy_kwh")
            values["m3_per_ha"] = _sum_field(block, one, "m3", "m3_por_riego", "water_m3")

        if category == "fertilizante":
            for key, aliases in (
                ("n_units", ("n_units", "unidades_n")),
                ("p_units", ("p_units", "unidades_p")),
                ("k_units", ("k_units", "unidades_k")),
            ):
                values[key] = _sum_field(block, one, *aliases) + _sum_inputs(block, one, key)

        if category in {"malezas", "plagas", "enfermedades"}:
            values["ia_grams"] = _ia_grams(block, category)

        if category == "cosecha":
            values["rendimiento_ton_ha"] = _sum_field(block, one, "rendimiento_ton_ha", "rendimiento")

        totals[category] = values

    return totals


def group_totals(entries: Sequence[EntrySnapshot], *, category: str, field: str) -> list[dict[str, Any]]:
    """Subtotales dentro de un bloque, como las dos aplicaciones foliares.

    La hoja fija dos (`G89 = G90+G95`); aquí el número de aplicación es un dato
    y salen tantos grupos como haya, que es lo que pasa en un ciclo con presión
    de plaga alta.
    """
    buckets: dict[str, float] = {}
    for entry in entries:
        if entry.category != category:
            continue
        raw = (entry.data or {}).get(field)
        key = str(raw).strip() if raw not in (None, "") else ""
        buckets[key] = buckets.get(key, 0.0) + _number(entry.cost_per_ha)

    def sort_key(value: str) -> tuple[int, float | str]:
        if not value:
            return (2, "")
        try:
            return (0, float(value))
        except ValueError:
            return (1, value)

    return [
        {"value": key or None, "cost_per_ha": total}
        for key, total in sorted(buckets.items(), key=lambda item: sort_key(item[0]))
    ]


def economics(
    entries: Sequence[EntrySnapshot],
    *,
    yield_ton_ha: float | None,
    price_per_ton: float | None,
) -> dict[str, Any]:
    """Bloque económico de la cabecera de la hoja."""
    investment = sum(cost_by_category(entries).values())

    revenue = None
    if yield_ton_ha and price_per_ton:
        revenue = yield_ton_ha * price_per_ton

    profit = revenue - investment if revenue is not None else None

    return {
        "yield_ton_ha": yield_ton_ha,
        "price_per_ton": price_per_ton,
        "revenue_per_ha": revenue,
        "investment_per_ha": investment,
        "profit_per_ha": profit,
        "benefit_cost_ratio": _divide(revenue, investment) if revenue is not None else None,
        "break_even_ton_ha": _divide(investment, price_per_ton),
        "unit_cost_per_ton": _divide(investment, yield_ton_ha),
    }


def _sum_field(entries: Iterable[EntrySnapshot], categories: set[str], *keys: str) -> float:
    """Suma un campo de `data` en las categorías indicadas, probando alias."""
    total = 0.0
    for entry in entries:
        if entry.category not in categories:
            continue
        for key in keys:
            if entry.data and key in entry.data:
                total += _number(entry.data.get(key))
                break
    return total


def _sum_inputs(entries: Iterable[EntrySnapshot], categories: set[str], attribute: str) -> float:
    total = 0.0
    for entry in entries:
        if entry.category not in categories:
            continue
        for item in entry.inputs:
            total += _number(getattr(item, attribute, None))
    return total


def _ia_grams(entries: Sequence[EntrySnapshot], category: str) -> float:
    """Gramos de i.a. de una categoría: los de la entrada más los de sus productos.

    La hoja solo tiene la columna agregada de la fila; los productos con su
    ingrediente activo son la versión trazable de lo mismo, así que se suman
    ambas fuentes y quien capture por producto no tiene que repetir el total.
    """
    categories = {category}
    return _sum_field(entries, categories, "ia_grams", "gramos_ia") + _sum_inputs(
        entries, categories, "ia_grams"
    )


def sustainability(
    entries: Sequence[EntrySnapshot],
    *,
    yield_ton_ha: float | None,
) -> dict[str, Any]:
    """Bloque de resultados técnicos: agua, energía e insumos por tonelada."""
    water_m3 = _sum_field(entries, {"riego"}, "m3", "m3_por_riego", "water_m3")
    energy_kwh = _sum_field(entries, {"riego"}, "kwh", "kwh_por_riego", "energy_kwh")
    diesel_l = _sum_field(
        entries,
        {"acondicionamiento", "siembra", "cosecha", "diversos"},
        "diesel_l",
        "litros_diesel",
    )

    nitrogen = _sum_field(entries, {"fertilizante"}, "n_units", "unidades_n") + _sum_inputs(
        entries, {"fertilizante"}, "n_units"
    )
    phosphorus = _sum_field(entries, {"fertilizante"}, "p_units", "unidades_p") + _sum_inputs(
        entries, {"fertilizante"}, "p_units"
    )
    potassium = _sum_field(entries, {"fertilizante"}, "k_units", "unidades_k") + _sum_inputs(
        entries, {"fertilizante"}, "k_units"
    )

    herbicide = _ia_grams(entries, "malezas")
    insecticide = _ia_grams(entries, "plagas")
    fungicide = _ia_grams(entries, "enfermedades")

    return {
        "water_m3_per_ha": water_m3,
        "water_footprint_m3_per_ton": _divide(water_m3, yield_ton_ha),
        "energy_kwh_per_ha": energy_kwh,
        "energy_kwh_per_ton": _divide(energy_kwh, yield_ton_ha),
        "diesel_l_per_ha": diesel_l,
        "nitrogen_units_per_ha": nitrogen,
        "phosphorus_units_per_ha": phosphorus,
        "potassium_units_per_ha": potassium,
        "nitrogen_kg_per_ton": _divide(nitrogen, yield_ton_ha),
        "herbicide_ia_g_per_ha": herbicide,
        "herbicide_ia_g_per_ton": _divide(herbicide, yield_ton_ha),
        "insecticide_ia_g_per_ha": insecticide,
        "insecticide_ia_g_per_ton": _divide(insecticide, yield_ton_ha),
        "fungicide_ia_g_per_ha": fungicide,
        "fungicide_ia_g_per_ton": _divide(fungicide, yield_ton_ha),
        "npk_formula": f"{nitrogen:g}-{phosphorus:g}-{potassium:g}",
    }


def compute_kpis(
    entries: Sequence[EntrySnapshot],
    *,
    yield_ton_ha: float | None,
    price_per_ton: float | None,
    budget_per_ha: float | None = None,
) -> dict[str, Any]:
    """Todos los indicadores del ciclo en una sola pasada."""
    summary = cost_summary(entries)
    economic = economics(entries, yield_ton_ha=yield_ton_ha, price_per_ton=price_per_ton)

    budget_usage = None
    if budget_per_ha:
        budget_usage = 100 * economic["investment_per_ha"] / budget_per_ha

    return {
        "economics": economic,
        "sustainability": sustainability(entries, yield_ton_ha=yield_ton_ha),
        "costs": summary,
        "blocks": block_totals(entries),
        "foliar_applications": group_totals(entries, category="foliar", field="aplicacion"),
        "budget_per_ha": budget_per_ha,
        "budget_usage_percentage": budget_usage,
        "entry_count": len(entries),
    }
