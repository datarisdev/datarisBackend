from __future__ import annotations

from io import BytesIO
from typing import Dict

import numpy as np
from PIL import Image

from app.services.sentinel2.indices import INDEX_DEFINITIONS, normalize_index_key

# Color ramps are local constants so no matplotlib colormap dependency is needed
# during image serving. Values are RGB stops from low to high.
PALETTES: Dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    "default": [
        (0.00, (127, 29, 29)),
        (0.20, (249, 115, 22)),
        (0.40, (254, 240, 138)),
        (0.60, (163, 230, 53)),
        (0.80, (34, 197, 94)),
        (1.00, (20, 83, 45)),
    ],
    "water": [
        (0.00, (127, 45, 18)),
        (0.35, (253, 224, 71)),
        (0.55, (186, 230, 253)),
        (0.75, (56, 189, 248)),
        (1.00, (30, 58, 138)),
    ],
    "moisture": [
        (0.00, (153, 27, 27)),
        (0.30, (249, 115, 22)),
        (0.50, (254, 240, 138)),
        (0.70, (45, 212, 191)),
        (1.00, (30, 64, 175)),
    ],
}


def _normalize(arr: np.ndarray, min_value: float | None, max_value: float | None) -> np.ndarray:
    arr = arr.astype("float32", copy=False)
    if min_value is None or max_value is None:
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros_like(arr, dtype="float32")
        p2, p98 = np.nanpercentile(finite, [2, 98])
        min_value = float(p2)
        max_value = float(p98)
    if max_value <= min_value:
        max_value = min_value + 1.0
    out = (arr - min_value) / (max_value - min_value)
    return np.clip(out, 0, 1).astype("float32", copy=False)


def _apply_palette(norm: np.ndarray, palette: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    h, w = norm.shape
    rgb = np.zeros((h, w, 3), dtype="uint8")
    stops = sorted(palette, key=lambda item: item[0])
    for idx in range(len(stops) - 1):
        left_pos, left_color = stops[idx]
        right_pos, right_color = stops[idx + 1]
        mask = (norm >= left_pos) & (norm <= right_pos)
        if not np.any(mask):
            continue
        denom = max(right_pos - left_pos, 1e-6)
        t = ((norm[mask] - left_pos) / denom).reshape(-1, 1)
        left = np.array(left_color, dtype="float32").reshape(1, 3)
        right = np.array(right_color, dtype="float32").reshape(1, 3)
        rgb[mask] = np.clip(left + (right - left) * t, 0, 255).astype("uint8")
    rgb[norm <= stops[0][0]] = stops[0][1]
    rgb[norm >= stops[-1][0]] = stops[-1][1]
    return rgb


def _render_rgb(arr: np.ndarray, opacity: float) -> bytes:
    if arr.ndim != 3 or arr.shape[0] != 3:
        raise ValueError("RGB arrays must have shape (3, height, width)")
    bands = []
    valid = np.ones(arr.shape[1:], dtype=bool)
    for band in arr:
        valid &= np.isfinite(band)
        norm = _normalize(band, None, None)
        # Gentle gamma correction for a less washed-out Sentinel-2 preview.
        bands.append((np.power(norm, 1 / 1.8) * 255).astype("uint8"))
    rgb = np.stack(bands, axis=-1)
    alpha = np.where(valid, int(np.clip(opacity, 0, 1) * 255), 0).astype("uint8")
    rgba = np.dstack([rgb, alpha])
    buffer = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_index_png(index_key: str, arr: np.ndarray, opacity: float = 0.88) -> bytes:
    normalized_key = normalize_index_key(index_key)
    definition = INDEX_DEFINITIONS.get(normalized_key) or INDEX_DEFINITIONS["NDVI"]

    if definition.rgb:
        return _render_rgb(arr, opacity)

    valid = np.isfinite(arr)
    norm = _normalize(arr, definition.min_value, definition.max_value)
    palette = PALETTES["default"]
    if normalized_key in {"NDWI"}:
        palette = PALETTES["water"]
    elif normalized_key in {"NDMI"}:
        palette = PALETTES["moisture"]

    rgb = _apply_palette(norm, palette)
    alpha = np.where(valid, int(np.clip(opacity, 0, 1) * 255), 0).astype("uint8")
    rgba = np.dstack([rgb, alpha])
    buffer = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def compute_statistics(arr: np.ndarray) -> dict:
    if arr.ndim == 3:
        arr = np.nanmean(arr.astype("float32"), axis=0)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"min": None, "max": None, "mean": None, "median": None, "std": None, "valid_pixels": 0}
    percentiles = np.nanpercentile(finite, [5, 25, 50, 75, 95])
    return {
        "min": float(np.nanmin(finite)),
        "max": float(np.nanmax(finite)),
        "mean": float(np.nanmean(finite)),
        "median": float(np.nanmedian(finite)),
        "std": float(np.nanstd(finite)),
        "p05": float(percentiles[0]),
        "p25": float(percentiles[1]),
        "p50": float(percentiles[2]),
        "p75": float(percentiles[3]),
        "p95": float(percentiles[4]),
        "valid_pixels": int(finite.size),
    }
