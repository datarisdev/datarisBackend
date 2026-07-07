"""Troceo en tiles con solape y fusión de detecciones (NMS) para imágenes
grandes (TIFF de dron/aéreas).

Puro y sin dependencias pesadas (nada de rasterio/ultralytics/numpy en las
firmas públicas) para poder testearlo con pytest normal, sin necesitar la
imagen Docker de entrenamiento.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    col_off: int
    row_off: int
    width: int
    height: int


def compute_tile_grid(width: int, height: int, tile_size: int, overlap_ratio: float) -> list[Tile]:
    """Grilla de ventanas cubriendo toda la imagen a resolución nativa (sin
    downsamplear), con solape `overlap_ratio` entre tiles vecinos para no
    perder objetos que caigan justo en un borde. El último tile de cada fila
    y columna se ajusta para llegar exacto al borde de la imagen, sin dejar
    una franja residual sin cubrir."""
    if width <= 0 or height <= 0:
        raise ValueError("width y height deben ser positivos")
    if tile_size <= 0:
        raise ValueError("tile_size debe ser positivo")
    if not (0 <= overlap_ratio < 1):
        raise ValueError("overlap_ratio debe estar en [0, 1)")

    stride = max(1, int(tile_size * (1 - overlap_ratio)))

    def _offsets(total: int) -> list[int]:
        if total <= tile_size:
            return [0]
        offsets = list(range(0, total - tile_size + 1, stride))
        last = total - tile_size
        if offsets[-1] != last:
            offsets.append(last)
        return offsets

    tiles: list[Tile] = []
    seen: set[tuple[int, int, int, int]] = set()
    for row_off in _offsets(height):
        for col_off in _offsets(width):
            tile_w = min(tile_size, width - col_off)
            tile_h = min(tile_size, height - row_off)
            key = (col_off, row_off, tile_w, tile_h)
            if key in seen:
                continue
            seen.add(key)
            tiles.append(Tile(col_off=col_off, row_off=row_off, width=tile_w, height=tile_h))
    return tiles


@dataclass(frozen=True)
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int
    class_name: str = ""


def translate_detection(det: Detection, tile: Tile) -> Detection:
    """Traduce una detección de coordenadas LOCALES del tile a coordenadas de
    la imagen ORIGINAL completa, sumando el offset del tile."""
    return Detection(
        x1=det.x1 + tile.col_off,
        y1=det.y1 + tile.row_off,
        x2=det.x2 + tile.col_off,
        y2=det.y2 + tile.row_off,
        confidence=det.confidence,
        class_id=det.class_id,
        class_name=det.class_name,
    )


def _iou(a: Detection, b: Detection) -> float:
    ix1, iy1 = max(a.x1, b.x1), max(a.y1, b.y1)
    ix2, iy2 = min(a.x2, b.x2), min(a.y2, b.y2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, a.x2 - a.x1) * max(0.0, a.y2 - a.y1)
    area_b = max(0.0, b.x2 - b.x1) * max(0.0, b.y2 - b.y1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def merge_overlapping_detections(detections: list[Detection], iou_threshold: float) -> list[Detection]:
    """NMS agrupado por clase: nunca fusiona detecciones de clases distintas.
    Dentro de una misma clase, descarta una caja si se solapa (IoU >=
    iou_threshold) con otra de mayor confianza — así un objeto que quedó
    partido entre dos tiles colapsa a una sola detección sobre la imagen
    completa, sin tocar objetos vecinos de otra clase que casualmente se
    toquen."""
    by_class: dict[int, list[Detection]] = {}
    for det in detections:
        by_class.setdefault(det.class_id, []).append(det)

    kept: list[Detection] = []
    for boxes in by_class.values():
        remaining = sorted(boxes, key=lambda d: d.confidence, reverse=True)
        while remaining:
            best = remaining.pop(0)
            kept.append(best)
            remaining = [d for d in remaining if _iou(d, best) < iou_threshold]
    return kept
