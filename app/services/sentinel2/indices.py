from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable

import numpy as np

BandMap = Dict[str, np.ndarray]


def _safe_div(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    numerator = numerator.astype("float32", copy=False)
    denominator = denominator.astype("float32", copy=False)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = numerator / denominator
    out[~np.isfinite(out)] = np.nan
    return out.astype("float32", copy=False)


def _clip_reflectance(arr: np.ndarray) -> np.ndarray:
    arr = arr.astype("float32", copy=False)
    # Sentinel-2 COG providers usually expose reflectance either in 0..1 or
    # scaled integer 0..10000. The heuristic keeps both providers compatible.
    finite = arr[np.isfinite(arr)]
    if finite.size and float(np.nanmax(finite)) > 2.0:
        arr = arr / 10000.0
    return arr.astype("float32", copy=False)


def ndvi(b: BandMap) -> np.ndarray:
    return _safe_div(b["nir"] - b["red"], b["nir"] + b["red"])


def gndvi(b: BandMap) -> np.ndarray:
    return _safe_div(b["nir"] - b["green"], b["nir"] + b["green"])


def ndre(b: BandMap) -> np.ndarray:
    rededge = b["rededge1"] if "rededge1" in b else b["rededge2"]
    return _safe_div(b["nir"] - rededge, b["nir"] + rededge)


def savi(b: BandMap, L: float = 0.5) -> np.ndarray:
    return _safe_div((b["nir"] - b["red"]) * (1.0 + L), b["nir"] + b["red"] + L)


def osavi(b: BandMap) -> np.ndarray:
    return _safe_div((b["nir"] - b["red"]) * 1.16, b["nir"] + b["red"] + 0.16)


def msavi2(b: BandMap) -> np.ndarray:
    nir = b["nir"].astype("float32", copy=False)
    red = b["red"].astype("float32", copy=False)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (2 * nir + 1 - np.sqrt(np.maximum((2 * nir + 1) ** 2 - 8 * (nir - red), 0))) / 2
    out[~np.isfinite(out)] = np.nan
    return out.astype("float32", copy=False)


def evi(b: BandMap) -> np.ndarray:
    return _safe_div(2.5 * (b["nir"] - b["red"]), b["nir"] + 6 * b["red"] - 7.5 * b["blue"] + 1)


def evi2(b: BandMap) -> np.ndarray:
    return _safe_div(2.5 * (b["nir"] - b["red"]), b["nir"] + 2.4 * b["red"] + 1)


def ndmi(b: BandMap) -> np.ndarray:
    return _safe_div(b["nir"] - b["swir16"], b["nir"] + b["swir16"])


def ndwi(b: BandMap) -> np.ndarray:
    return _safe_div(b["green"] - b["nir"], b["green"] + b["nir"])


def nbr(b: BandMap) -> np.ndarray:
    return _safe_div(b["nir"] - b["swir22"], b["nir"] + b["swir22"])


def gci(b: BandMap) -> np.ndarray:
    return _safe_div(b["nir"], b["green"]) - 1


def ci_rededge(b: BandMap) -> np.ndarray:
    rededge = b["rededge1"] if "rededge1" in b else b["rededge2"]
    return _safe_div(b["nir"], rededge) - 1


def true_color(b: BandMap) -> np.ndarray:
    return np.stack([b["red"], b["green"], b["blue"]], axis=0).astype("float32", copy=False)


def false_color(b: BandMap) -> np.ndarray:
    return np.stack([b["nir"], b["red"], b["green"]], axis=0).astype("float32", copy=False)


@dataclass(frozen=True)
class IndexDefinition:
    key: str
    label: str
    description: str
    resolution: str
    bands: tuple[str, ...]
    compute: Callable[[BandMap], np.ndarray]
    min_value: float | None = -1.0
    max_value: float | None = 1.0
    rgb: bool = False
    priority: int = 100


INDEX_DEFINITIONS: Dict[str, IndexDefinition] = {
    "NDVI": IndexDefinition("NDVI", "NDVI", "Vigor vegetal general", "10m", ("red", "nir"), ndvi, -0.2, 0.95, priority=10),
    "GNDVI": IndexDefinition("GNDVI", "GNDVI", "Clorofila usando banda verde", "10m", ("green", "nir"), gndvi, -0.2, 0.95, priority=11),
    "EVI": IndexDefinition("EVI", "EVI", "Índice de vegetación mejorado", "10m", ("blue", "red", "nir"), evi, -0.2, 1.0, priority=12),
    "EVI2": IndexDefinition("EVI2", "EVI2", "EVI simplificado sin banda azul", "10m", ("red", "nir"), evi2, -0.2, 1.0, priority=13),
    "SAVI": IndexDefinition("SAVI", "SAVI", "Vegetación corrigiendo suelo expuesto", "10m", ("red", "nir"), savi, -0.2, 1.0, priority=14),
    "MSAVI2": IndexDefinition("MSAVI2", "MSAVI2", "Vegetación de baja cobertura", "10m", ("red", "nir"), msavi2, -0.1, 1.0, priority=15),
    "OSAVI": IndexDefinition("OSAVI", "OSAVI", "SAVI optimizado", "10m", ("red", "nir"), osavi, -0.1, 1.0, priority=16),
    "NDRE": IndexDefinition("NDRE", "NDRE", "Red Edge para cultivos densos", "20m", ("rededge1", "nir"), ndre, -0.2, 0.85, priority=20),
    "CI_REDEDGE": IndexDefinition("CI_REDEDGE", "CI Red Edge", "Índice de clorofila Red Edge", "20m", ("rededge1", "nir"), ci_rededge, 0.0, 8.0, priority=21),
    "NDMI": IndexDefinition("NDMI", "NDMI", "Humedad de vegetación", "20m", ("nir", "swir16"), ndmi, -0.8, 0.8, priority=30),
    "NDWI": IndexDefinition("NDWI", "NDWI", "Agua superficial / humedad", "10m", ("green", "nir"), ndwi, -0.8, 0.8, priority=31),
    "NBR": IndexDefinition("NBR", "NBR", "Estrés severo, quema o pérdida de cobertura", "20m", ("nir", "swir22"), nbr, -0.5, 1.0, priority=32),
    "GCI": IndexDefinition("GCI", "GCI", "Índice de clorofila verde", "10m", ("green", "nir"), gci, 0.0, 8.0, priority=33),
    "TRUE_COLOR": IndexDefinition("TRUE_COLOR", "Color real", "RGB natural Sentinel-2", "10m", ("red", "green", "blue"), true_color, None, None, rgb=True, priority=90),
    "FALSE_COLOR": IndexDefinition("FALSE_COLOR", "Falso color", "NIR/rojo/verde", "10m", ("nir", "red", "green"), false_color, None, None, rgb=True, priority=91),
}


BAND_ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "blue": ("blue", "B02", "B2", "B02_10m"),
    "green": ("green", "B03", "B3", "B03_10m"),
    "red": ("red", "B04", "B4", "B04_10m"),
    "nir": ("nir", "B08", "B8", "B08_10m", "nir08"),
    "rededge1": ("rededge1", "B05", "B5", "B05_20m"),
    "rededge2": ("rededge2", "B06", "B6", "B06_20m"),
    "rededge3": ("rededge3", "B07", "B7", "B07_20m"),
    "nir08": ("nir08", "B8A", "B8A_20m"),
    "swir16": ("swir16", "B11", "B11_20m"),
    "swir22": ("swir22", "B12", "B12_20m"),
    "scl": ("scl", "SCL", "SCL_20m"),
}


def normalize_index_key(value: str | None) -> str:
    raw = str(value or "NDVI").strip().upper().replace("-", "_")
    aliases = {
        "MSAVI": "MSAVI2",
        "RECI": "CI_REDEDGE",
        "CIREDEDGE": "CI_REDEDGE",
        "CI_RED_EDGE": "CI_REDEDGE",
        "TRUE_COLOR": "TRUE_COLOR",
        "TRUECOLOR": "TRUE_COLOR",
        "COLOR_REAL": "TRUE_COLOR",
        "FALSE_COLOR": "FALSE_COLOR",
        "FALSECOLOR": "FALSE_COLOR",
        "FALSO_COLOR": "FALSE_COLOR",
    }
    return aliases.get(raw, raw)


def required_bands(index_keys: Iterable[str]) -> list[str]:
    bands: set[str] = set()
    for key in index_keys:
        normalized = normalize_index_key(key)
        definition = INDEX_DEFINITIONS.get(normalized)
        if definition:
            bands.update(definition.bands)
    return sorted(bands)


def normalize_band_values(bands: BandMap) -> BandMap:
    return {key: _clip_reflectance(value) for key, value in bands.items()}
