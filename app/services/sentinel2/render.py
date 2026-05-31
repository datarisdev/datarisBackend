from __future__ import annotations

from io import BytesIO
from typing import Dict

import numpy as np
from PIL import Image

from app.services.sentinel2.indices import INDEX_DEFINITIONS, normalize_index_key

# High-contrast palettes for Sentinel-2 previews. The numeric statistics remain
# untouched; only the visual stretch is adapted to the selected lot so subtle
# differences are visible without inventing spatial detail.
PALETTES: Dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    "default": [
        (0.00, (153, 27, 27)),
        (0.13, (220, 38, 38)),
        (0.28, (249, 115, 22)),
        (0.43, (250, 204, 21)),
        (0.56, (217, 249, 157)),
        (0.70, (132, 204, 22)),
        (0.84, (22, 163, 74)),
        (1.00, (20, 83, 45)),
    ],
    "water": [
        (0.00, (124, 45, 18)),
        (0.22, (234, 179, 8)),
        (0.44, (186, 230, 253)),
        (0.64, (56, 189, 248)),
        (0.82, (37, 99, 235)),
        (1.00, (30, 58, 138)),
    ],
    "moisture": [
        (0.00, (153, 27, 27)),
        (0.20, (249, 115, 22)),
        (0.40, (254, 240, 138)),
        (0.58, (94, 234, 212)),
        (0.78, (14, 165, 233)),
        (1.00, (30, 64, 175)),
    ],
}


def _finite_values(arr: np.ndarray) -> np.ndarray:
    return arr[np.isfinite(arr)].astype("float32", copy=False)


def _resolve_display_range(
    arr: np.ndarray,
    min_value: float | None,
    max_value: float | None,
) -> tuple[float, float]:
    """Return a robust visual range for the current lot.

    Sentinel pixels keep their real values. We only stretch the palette between
    robust percentiles, which avoids a flat-looking raster when the parcel has a
    narrow but meaningful variation range.
    """
    finite = _finite_values(arr)
    if finite.size == 0:
        return 0.0, 1.0

    p03, p97 = [float(value) for value in np.nanpercentile(finite, [3, 97])]
    absolute_min = float(np.nanmin(finite))
    absolute_max = float(np.nanmax(finite))

    lower = p03 if np.isfinite(p03) else absolute_min
    upper = p97 if np.isfinite(p97) else absolute_max
    if min_value is not None:
        lower = max(lower, float(min_value))
    if max_value is not None:
        upper = min(upper, float(max_value))

    if upper <= lower:
        center = float(np.nanmedian(finite))
        configured_span = (float(max_value) - float(min_value)) if min_value is not None and max_value is not None else 1.0
        minimum_span = max(abs(configured_span) * 0.04, 0.02)
        lower = center - minimum_span / 2
        upper = center + minimum_span / 2
    else:
        configured_span = (float(max_value) - float(min_value)) if min_value is not None and max_value is not None else upper - lower
        minimum_span = max(abs(configured_span) * 0.035, 0.015)
        span = upper - lower
        padding = max(span * 0.08, minimum_span * 0.08)
        center = (lower + upper) / 2
        span = max(span + padding * 2, minimum_span)
        lower = center - span / 2
        upper = center + span / 2

    if min_value is not None and upper <= float(min_value):
        lower = float(min_value)
        upper = float(min_value) + max(0.02, abs(float(max_value or 1) - float(min_value)) * 0.04)
    if max_value is not None and lower >= float(max_value):
        upper = float(max_value)
        lower = float(max_value) - max(0.02, abs(float(max_value) - float(min_value or 0)) * 0.04)

    if min_value is not None:
        lower = max(lower, float(min_value))
    if max_value is not None:
        upper = min(upper, float(max_value))
    if upper <= lower:
        upper = lower + 0.02
    return float(lower), float(upper)


def _normalize(arr: np.ndarray, min_value: float | None, max_value: float | None) -> np.ndarray:
    arr = arr.astype("float32", copy=False)
    lower, upper = _resolve_display_range(arr, min_value, max_value)
    out = (arr - lower) / (upper - lower)
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
        bands.append((np.power(norm, 1 / 1.7) * 255).astype("uint8"))
    rgb = np.stack(bands, axis=-1)
    alpha = np.where(valid, int(np.clip(opacity, 0, 1) * 255), 0).astype("uint8")
    rgba = np.dstack([rgb, alpha])
    buffer = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_index_png(index_key: str, arr: np.ndarray, opacity: float = 0.94) -> bytes:
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


def _relative_zone_distribution(arr: np.ndarray) -> list[dict]:
    finite = _finite_values(arr)
    if finite.size == 0:
        return []

    low_threshold, high_threshold = [float(value) for value in np.nanpercentile(finite, [33.333, 66.667])]
    if high_threshold <= low_threshold:
        center = float(np.nanmedian(finite))
        spread = max(float(np.nanstd(finite)), 0.01)
        low_threshold = center - spread * 0.35
        high_threshold = center + spread * 0.35

    masks = [
        finite < low_threshold,
        (finite >= low_threshold) & (finite < high_threshold),
        finite >= high_threshold,
    ]
    definitions = [
        ("low", "Vigor relativo bajo", None, low_threshold),
        ("medium", "Vigor relativo medio", low_threshold, high_threshold),
        ("high", "Vigor relativo alto", high_threshold, None),
    ]

    total = max(int(finite.size), 1)
    zones: list[dict] = []
    for (key, label, minimum, maximum), mask in zip(definitions, masks):
        pixels = int(np.count_nonzero(mask))
        zones.append({
            "key": key,
            "label": label,
            "min": minimum,
            "max": maximum,
            "pixels": pixels,
            "percentage": round((pixels / total) * 100, 4),
        })
    return zones


def compute_statistics(arr: np.ndarray, index_key: str | None = None) -> dict:
    if arr.ndim == 3:
        arr = np.nanmean(arr.astype("float32"), axis=0)
    finite = _finite_values(arr)
    if finite.size == 0:
        return {
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "std": None,
            "valid_pixels": 0,
            "zone_distribution": [],
        }

    percentiles = np.nanpercentile(finite, [5, 25, 33.333, 50, 66.667, 75, 95])
    definition = INDEX_DEFINITIONS.get(normalize_index_key(index_key)) if index_key else None
    visual_min, visual_max = _resolve_display_range(
        arr,
        definition.min_value if definition else None,
        definition.max_value if definition else None,
    )
    return {
        "min": float(np.nanmin(finite)),
        "max": float(np.nanmax(finite)),
        "mean": float(np.nanmean(finite)),
        "median": float(np.nanmedian(finite)),
        "std": float(np.nanstd(finite)),
        "p05": float(percentiles[0]),
        "p25": float(percentiles[1]),
        "p33": float(percentiles[2]),
        "p50": float(percentiles[3]),
        "p67": float(percentiles[4]),
        "p75": float(percentiles[5]),
        "p95": float(percentiles[6]),
        "valid_pixels": int(finite.size),
        "visualization_min": visual_min,
        "visualization_max": visual_max,
        "zone_distribution": _relative_zone_distribution(arr),
    }
