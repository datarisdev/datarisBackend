"""Matriz de sensibilidad rendimiento × precio.

Reproduce el bloque final de la hoja original, verificado contra sus valores:
con una inversión de 14 156 $/ha, 4.5 ton/ha a 4 450 $/ton da 5 869 $/ha de
utilidad, y 5.0 ton/ha al mismo precio da 8 094. Es decir:

    utilidad(rendimiento, precio) = rendimiento × precio − inversión

La matriz no se almacena: se recalcula en cada consulta, que es una resta.
"""

from __future__ import annotations

from typing import Any, Sequence

DEFAULT_YIELD_STEPS = 10
DEFAULT_PRICE_STEPS = 9

# Pasos de la hoja: el eje de rendimiento va de 4.5 a 9 t/ha de media en media
# tonelada, y el de precio de 4 450 a 5 650 $/t de 150 en 150. Son incrementos
# con los que un productor razona ("media tonelada más", "cien pesos menos");
# un paso porcentual daría 0.45 t/ha y 133.50 $/t, que se leen peor.
DEFAULT_YIELD_STEP = 0.5
DEFAULT_PRICE_STEP = 150.0


def _build_axis(center: float, step: float, count: int) -> list[float]:
    """Eje centrado en el valor actual, con `count` valores en total."""
    if count < 1:
        count = 1
    before = (count - 1) // 2
    start = center - before * step
    if start <= 0:
        start = step
    return [round(start + index * step, 4) for index in range(count)]


def build_matrix(
    *,
    investment_per_ha: float,
    yield_ton_ha: float | None,
    price_per_ton: float | None,
    yield_step: float | None = None,
    price_step: float | None = None,
    yield_values: Sequence[float] | None = None,
    price_values: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Devuelve la rejilla de utilidad por hectárea.

    Cuando el ciclo ya tiene rendimiento y precio, los ejes se centran en ellos
    y la celda correspondiente viene marcada como escenario actual; así el
    usuario ve de un vistazo cuánto margen le queda antes de entrar en pérdida.
    """
    base_yield = yield_ton_ha or 6.0
    base_price = price_per_ton or 5000.0

    if yield_values:
        yields = [float(value) for value in yield_values]
    else:
        yields = _build_axis(base_yield, yield_step or DEFAULT_YIELD_STEP, DEFAULT_YIELD_STEPS)

    if price_values:
        prices = [float(value) for value in price_values]
    else:
        prices = _build_axis(base_price, price_step or DEFAULT_PRICE_STEP, DEFAULT_PRICE_STEPS)

    rows = []
    for yield_value in yields:
        cells = []
        for price in prices:
            profit = yield_value * price - investment_per_ha
            cells.append(
                {
                    "price_per_ton": price,
                    "profit_per_ha": profit,
                    "is_current": (
                        yield_ton_ha is not None
                        and price_per_ton is not None
                        and abs(yield_value - yield_ton_ha) < 1e-9
                        and abs(price - price_per_ton) < 1e-9
                    ),
                }
            )
        rows.append({"yield_ton_ha": yield_value, "cells": cells})

    # Precio al que se cubren los costos con el rendimiento actual, y viceversa:
    # los dos números que de verdad se usan para negociar la venta.
    break_even_price = investment_per_ha / base_yield if base_yield else None
    break_even_yield = investment_per_ha / base_price if base_price else None

    return {
        "investment_per_ha": investment_per_ha,
        "yield_axis": yields,
        "price_axis": prices,
        "rows": rows,
        "break_even_price_per_ton": break_even_price,
        "break_even_yield_ton_ha": break_even_yield,
    }
