#!/usr/bin/env python
"""Inferencia de un modelo Ultralytics YOLO ya entrenado contra una imagen de
prueba (PNG/JPG/TIFF).

Espejo deliberado de train.py: mismos principios (argparse estricto, log JSON
estructurado por evento, nada de comandos arbitrarios). Corre en la misma
imagen Docker y el mismo Compute Cluster GPU que el entrenamiento — ver
app/modules/ml_training/inference_service.py y azure_ml_client.py.

Para imágenes más grandes que --tile-size (típico en TIFF de dron/aéreas), la
imagen se lee por ventanas con rasterio (mismo lector para PNG/JPG/TIFF, sin
ramas por formato) y se trocea en tiles con solape (ver tiling.py). Cada tile
se corre a resolución nativa (sin downsamplear, para no perder detalle), las
detecciones se traducen a coordenadas de la imagen completa y se fusionan
(NMS por clase) para que el resultado final sea un único conjunto de
detecciones sobre la imagen entera, sin duplicados en los bordes de los
tiles.

Uso real (generado por Azure ML, ver azure_ml_client.py):
    python predict.py --model-path <input> --image-path <input> \
        --image-file foto.tif --tile-size 640 --overlap 0.25 \
        --conf 0.25 --iou 0.45 --job-id <uuid> --output-path <output>
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from PIL import Image, ImageDraw

from create_manifest import build_manifest
from tiling import Detection, Tile, compute_tile_grid, merge_overlapping_detections, translate_detection

MAX_PREVIEW_SIDE_PX = 4096


def _log(event: str, **fields) -> None:
    """Log estructurado en una línea JSON por evento (mismo formato que train.py)."""
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Corre un modelo Ultralytics YOLO ya entrenado contra una imagen de prueba")
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--image-path", required=True, type=Path)
    parser.add_argument("--image-file", required=True)
    parser.add_argument("--tile-size", required=True, type=int)
    parser.add_argument("--overlap", required=True, type=float)
    parser.add_argument("--conf", required=True, type=float)
    parser.add_argument("--iou", required=True, type=float)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-path", required=True, type=Path)
    args = parser.parse_args()

    if not (32 <= args.tile_size <= 4096):
        parser.error("tile-size fuera de rango (32-4096)")
    if not (0 <= args.overlap < 1):
        parser.error("overlap fuera de rango [0-1)")
    if not (0 < args.conf <= 1):
        parser.error("conf fuera de rango (0-1]")
    if not (0 < args.iou <= 1):
        parser.error("iou fuera de rango (0-1]")

    return args


def _find_weights(model_path: Path) -> Path:
    for name in ("best.pt", "last.pt"):
        candidate = model_path / name
        if candidate.exists():
            return candidate
    candidates = list(model_path.rglob("*.pt"))
    if not candidates:
        raise FileNotFoundError("No se encontraron pesos .pt en model_path")
    return candidates[0]


def _to_rgb_uint8(array: np.ndarray) -> np.ndarray:
    """rasterio entrega bandas primero (C,H,W); ultralytics espera (H,W,C)
    RGB uint8. Colapsa a 3 canales y normaliza el rango dinámico si la imagen
    viene en 16-bit u otro rango (frecuente en GeoTIFF aéreo/multiespectral)."""
    if array.dtype != np.uint8:
        array = array.astype(np.float32)
        max_val = float(array.max()) if array.size else 1.0
        if max_val <= 0:
            max_val = 1.0
        array = np.clip(array / max_val * 255.0, 0, 255).astype(np.uint8)

    bands = array.shape[0]
    rgb = array[:3] if bands >= 3 else np.repeat(array[:1], 3, axis=0)
    return np.ascontiguousarray(np.transpose(rgb, (1, 2, 0)))


def _run_tile_inference(model, tile_rgb: np.ndarray, conf: float) -> list[Detection]:
    results = model.predict(tile_rgb, conf=conf, verbose=False)
    detections: list[Detection] = []
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        names = result.names or {}
        for box in boxes:
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
            class_id = int(box.cls[0].item())
            detections.append(
                Detection(
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    confidence=float(box.conf[0].item()),
                    class_id=class_id,
                    class_name=str(names.get(class_id, class_id)),
                )
            )
    return detections


def _build_preview(image_file: Path, detections: list[Detection], width: int, height: int, output_path: Path) -> None:
    """Overview reescalada de la imagen completa (nunca el TIFF a resolución
    completa en memoria, evita OOM en orthomosaics de varios GB) con las
    cajas fusionadas dibujadas encima — la respuesta visual a "volver a unir
    toda la imagen y mostrar las detecciones"."""
    scale = min(1.0, MAX_PREVIEW_SIDE_PX / max(width, height))
    preview_w = max(1, round(width * scale))
    preview_h = max(1, round(height * scale))

    with rasterio.open(image_file) as src:
        array = src.read(out_shape=(min(src.count, 3), preview_h, preview_w))
    preview_rgb = _to_rgb_uint8(array)

    image = Image.fromarray(preview_rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    for det in detections:
        box = [det.x1 * scale, det.y1 * scale, det.x2 * scale, det.y2 * scale]
        draw.rectangle(box, outline=(255, 64, 64), width=2)
        draw.text((box[0] + 2, max(0, box[1] - 12)), f"{det.class_name} {det.confidence:.2f}", fill=(255, 64, 64))

    image.save(output_path, format="PNG")


def main() -> int:
    args = _parse_args()
    output_dir = args.output_path
    output_dir.mkdir(parents=True, exist_ok=True)

    _log("inference_started", job_id=args.job_id)

    try:
        weights_path = _find_weights(args.model_path)
        image_file = args.image_path / args.image_file
        if not image_file.exists():
            raise FileNotFoundError(f"Imagen de prueba no encontrada: {image_file}")

        from ultralytics import YOLO

        _log("loading_model", weights=str(weights_path))
        model = YOLO(str(weights_path))

        progress_path = output_dir / "progress.json"
        all_detections: list[Detection] = []

        with rasterio.open(image_file) as src:
            width, height = src.width, src.height
            tiles: list[Tile] = compute_tile_grid(width, height, args.tile_size, args.overlap)
            _log("tiling_computed", tile_count=len(tiles), width=width, height=height)

            for index, tile in enumerate(tiles):
                window = Window(tile.col_off, tile.row_off, tile.width, tile.height)
                tile_rgb = _to_rgb_uint8(src.read(window=window))
                tile_detections = _run_tile_inference(model, tile_rgb, args.conf)
                all_detections.extend(translate_detection(det, tile) for det in tile_detections)

                progress_path.write_text(
                    json.dumps(
                        {
                            "tiles_processed": index + 1,
                            "tile_count": len(tiles),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )

        merged = merge_overlapping_detections(all_detections, args.iou)
        _log("detections_merged", raw=len(all_detections), merged=len(merged))

        _build_preview(image_file, merged, width, height, output_dir / "preview.png")

        detections_payload = [
            {
                "class_id": det.class_id,
                "class_name": det.class_name,
                "confidence": round(det.confidence, 4),
                "bbox": [round(det.x1, 2), round(det.y1, 2), round(det.x2, 2), round(det.y2, 2)],
            }
            for det in merged
        ]
        (output_dir / "detections.json").write_text(
            json.dumps(
                {
                    "image_width": width,
                    "image_height": height,
                    "tile_count": len(tiles),
                    "detection_count": len(merged),
                    "detections": detections_payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        manifest = build_manifest(
            job_id=args.job_id,
            project_id="",
            recipe="inference",
            task="detection",
            model_base=weights_path.name,
            config={"tile_size": args.tile_size, "overlap": args.overlap, "conf": args.conf, "iou": args.iou},
            metrics={"detection_count": len(merged)},
            output_dir=output_dir,
            docker_image_ref=None,
        )
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        _log("job_finished", status="completed", detections=len(merged))
        return 0

    except Exception as exc:  # noqa: BLE001 - error sanitizado hacia stdout/error.json
        _log("job_failed", error=str(exc))
        error_payload = {"error": str(exc), "traceback": traceback.format_exc()}
        (output_dir / "error.json").write_text(json.dumps(error_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
