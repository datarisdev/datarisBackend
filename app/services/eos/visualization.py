"""Canonical EOSDA render palettes shared with the API response and UI legend.

EOSDA range colorization applies absolute thresholds: N thresholds define N+1
discrete color intervals. Keeping this contract server-side prevents the map,
legend and point tooltip from drifting independently.
"""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from typing import Any, Optional


_VISUALIZATIONS: dict[str, dict[str, Any]] = {
    "NDVI": {
        "label": "NDVI",
        "full_name": "Índice de Vegetación de Diferencia Normalizada",
        "description": "Respuesta espectral de la vegetación",
        "min": -1.0,
        "max": 1.0,
        "colors": ["#1f2937", "#8b1d1d", "#e76f2e", "#e7cf3a", "#9dcc4b", "#3b9b51", "#096b3b"],
        "thresholds": [-0.2, 0.0, 0.2, 0.4, 0.6, 0.8],
        "labels": [
            "Respuesta negativa",
            "Superficie no vegetal",
            "Vegetación muy baja",
            "Vegetación baja",
            "Vegetación media",
            "Vegetación alta",
            "Vegetación muy alta",
        ],
    },
    "NDWI": {
        "label": "NDWI",
        "full_name": "Índice de Agua de Diferencia Normalizada",
        "description": "Respuesta relativa de agua superficial",
        "min": -1.0,
        "max": 1.0,
        "colors": ["#7c2d12", "#d97706", "#facc15", "#bae6fd", "#38bdf8", "#2563eb", "#172554"],
        "thresholds": [-0.4, -0.1, 0.1, 0.3, 0.6, 0.8],
        "labels": [
            "Respuesta de agua muy baja",
            "Respuesta de agua baja",
            "Respuesta ligeramente negativa",
            "Respuesta positiva baja",
            "Respuesta positiva media",
            "Respuesta positiva alta",
            "Respuesta positiva muy alta",
        ],
    },
    "NDSI": {
        "label": "NDSI",
        "full_name": "Índice de Nieve de Diferencia Normalizada",
        "description": "Contraste espectral visible–SWIR",
        "min": -1.0,
        "max": 1.0,
        "colors": ["#5b3a29", "#9a6b45", "#c7a878", "#d6d3d1", "#a5f3fc", "#38bdf8", "#f8fafc"],
        "thresholds": [-0.4, -0.1, 0.1, 0.3, 0.6, 0.8],
        "labels": [
            "Respuesta NDSI muy baja",
            "Respuesta NDSI baja",
            "Respuesta NDSI ligeramente negativa",
            "Respuesta NDSI positiva baja",
            "Respuesta NDSI positiva media",
            "Respuesta NDSI alta",
            "Respuesta NDSI muy alta",
        ],
    },
}


def legend_for_index(index: str) -> Optional[dict[str, Any]]:
    config = _VISUALIZATIONS.get(str(index or "").upper())
    if not config:
        return None
    payload = deepcopy(config)
    payload.update(
        {
            "provider": "EOSDA",
            "render_method": "absolute_thresholds",
            "value_source": "EOSDA Point Value API",
            "precision": 4,
        }
    )
    return payload


def render_params_for_index(index: str) -> dict[str, str]:
    config = _VISUALIZATIONS.get(str(index or "").upper())
    if not config:
        return {}
    return {
        "COLORS": ",".join(color.removeprefix("#") for color in config["colors"]),
        "THRESHOLDS": ",".join(f"{float(value):g}" for value in config["thresholds"]),
        "mimetype": "image/png",
    }


def classify_index_value(index: str, value: float) -> Optional[dict[str, Any]]:
    config = _VISUALIZATIONS.get(str(index or "").upper())
    if not config:
        return None
    position = bisect_right(config["thresholds"], float(value))
    lower = config["min"] if position == 0 else config["thresholds"][position - 1]
    upper = config["max"] if position >= len(config["thresholds"]) else config["thresholds"][position]
    return {
        "label": config["labels"][position],
        "color": config["colors"][position],
        "range": {"min": lower, "max": upper},
    }
