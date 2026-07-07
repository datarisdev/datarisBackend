#!/usr/bin/env python
"""Smoke test local de la imagen de entrenamiento — SIN GPU y SIN entrenar
un modelo real (eso requeriría GPU y minutos/horas). Verifica:

1. Que todas las dependencias pesadas importan correctamente.
2. Que validate_dataset.py detecta un dataset sintético válido e inválido.
3. Que create_manifest.py genera un manifest.json con las claves esperadas.
4. Que train.py rechaza argumentos fuera de rango (sin llegar a entrenar).
5. Que tiling.py trocea correctamente y fusiona detecciones solapadas (NMS).
6. Que predict.py rechaza argumentos fuera de rango (sin correr inferencia).
7. Que predict.py corre de punta a punta con un modelo real (yolov8n
   pretrained, en CPU) contra una imagen sintética más grande que un tile,
   y produce preview.png + detections.json + manifest.json coherentes.

Uso:
    python smoke_test.py
Exit code 0 si todo pasa, 1 si algo falla.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from PIL import Image

from tiling import Detection, Tile, compute_tile_grid, merge_overlapping_detections, translate_detection


def _make_image(path: Path, size=(32, 32)):
    Image.new("RGB", size, (128, 64, 32)).save(path, format="JPEG")


def check_imports() -> bool:
    print("[1/4] Verificando imports...")
    try:
        import torch  # noqa: F401
        import ultralytics  # noqa: F401
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import yaml  # noqa: F401
        from PIL import Image as _Image  # noqa: F401
        from azure.storage.blob import BlobServiceClient  # noqa: F401
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    print("  OK")
    return True


def check_validate_dataset() -> bool:
    print("[2/4] Verificando validate_dataset.py con dataset sintético...")
    from validate_dataset import validate

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data.yaml").write_text(yaml.safe_dump({"names": ["weed", "crop"], "nc": 2}))
        for split, n in (("train", 12), ("valid", 4)):
            images_dir = root / split / "images"
            labels_dir = root / split / "labels"
            images_dir.mkdir(parents=True)
            labels_dir.mkdir(parents=True)
            for i in range(n):
                _make_image(images_dir / f"i{i}.jpg")
                (labels_dir / f"i{i}.txt").write_text(f"{i % 2} 0.5 0.5 0.2 0.2\n")

        report = validate(root, "detection")
        if not report["is_valid"]:
            print(f"  FAIL: dataset válido reportado como inválido: {report['errors']}")
            return False
        if report["image_count"] != 16:
            print(f"  FAIL: conteo de imágenes incorrecto: {report['image_count']}")
            return False

        # Dataset vacío debe fallar
        empty_root = root / "empty"
        empty_root.mkdir()
        empty_report = validate(empty_root, "detection")
        if empty_report["is_valid"]:
            print("  FAIL: dataset vacío no fue rechazado")
            return False

    print("  OK")
    return True


def check_manifest_generation() -> bool:
    print("[3/4] Verificando create_manifest.py...")
    from create_manifest import build_manifest

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        (output_dir / "best.pt").write_bytes(b"fake-weights")
        manifest = build_manifest(
            job_id="job-1",
            project_id="project-1",
            recipe="ultralytics_yolo_detection",
            task="detection",
            model_base="yolo11n.pt",
            config={"epochs": 1},
            metrics={"mAP50": 0.5},
            output_dir=output_dir,
            docker_image_ref="acr.azurecr.io/ml-training:test",
        )
        required_keys = {"job_id", "project_id", "recipe", "artifacts", "dependency_versions"}
        if not required_keys.issubset(manifest.keys()):
            print(f"  FAIL: faltan claves en el manifest: {required_keys - manifest.keys()}")
            return False
        if not manifest["artifacts"]:
            print("  FAIL: el manifest no listó ningún artefacto")
            return False

    print("  OK")
    return True


def check_train_cli_validation() -> bool:
    print("[4/4] Verificando validación de argumentos de train.py (sin entrenar)...")
    result = subprocess.run(
        [
            sys.executable,
            "train.py",
            "--dataset-path", "/nonexistent",
            "--task", "detection",
            "--recipe", "ultralytics_yolo_detection",
            "--model", "yolo11n.pt",
            "--epochs", "99999",  # fuera de rango a propósito
            "--imgsz", "640",
            "--batch", "16",
            "--lr", "0.01",
            "--patience", "20",
            "--val-split", "0.2",
            "--seed", "0",
            "--augment", "1",
            "--project-id", "p1",
            "--job-id", "j1",
            "--output-path", "/tmp/out",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
    )
    if result.returncode == 0:
        print("  FAIL: train.py aceptó epochs fuera de rango")
        return False
    print("  OK (rechazado correctamente)")
    return True


def check_tiling() -> bool:
    print("[5/7] Verificando tiling.py (grid + fusión de detecciones)...")

    # Grid: imagen de 1000x600 con tiles de 640 y 25% de solape debe cubrir
    # toda la imagen, incluyendo el borde derecho/inferior exacto.
    tiles = compute_tile_grid(1000, 600, 640, 0.25)
    if len(tiles) < 2:
        print(f"  FAIL: se esperaban al menos 2 tiles, se obtuvieron {len(tiles)}")
        return False
    max_right = max(t.col_off + t.width for t in tiles)
    max_bottom = max(t.row_off + t.height for t in tiles)
    if max_right != 1000 or max_bottom != 600:
        print(f"  FAIL: el grid no cubre la imagen completa (right={max_right}, bottom={max_bottom})")
        return False

    # Traducción de coordenadas: una detección local del tile debe
    # desplazarse exactamente por el offset del tile.
    tile = Tile(col_off=500, row_off=200, width=640, height=400)
    local_det = Detection(x1=10, y1=20, x2=110, y2=120, confidence=0.9, class_id=0, class_name="weed")
    global_det = translate_detection(local_det, tile)
    if (global_det.x1, global_det.y1, global_det.x2, global_det.y2) != (510, 220, 610, 320):
        print(f"  FAIL: traducción de coordenadas incorrecta: {global_det}")
        return False

    # Fusión: dos detecciones casi idénticas de la MISMA clase (el mismo
    # objeto visto en dos tiles solapados) deben colapsar a una sola;
    # una tercera de OTRA clase, aunque se solape, nunca debe fusionarse.
    same_class_dup = Detection(x1=505, y1=505, x2=605, y2=605, confidence=0.8, class_id=0, class_name="weed")
    same_class_orig = Detection(x1=500, y1=500, x2=600, y2=600, confidence=0.95, class_id=0, class_name="weed")
    other_class = Detection(x1=502, y1=502, x2=602, y2=602, confidence=0.7, class_id=1, class_name="crop")
    merged = merge_overlapping_detections([same_class_dup, same_class_orig, other_class], iou_threshold=0.5)
    if len(merged) != 2:
        print(f"  FAIL: se esperaban 2 detecciones tras la fusión, se obtuvieron {len(merged)}")
        return False
    kept_weed = [d for d in merged if d.class_id == 0]
    if len(kept_weed) != 1 or kept_weed[0].confidence != 0.95:
        print("  FAIL: la fusión no se quedó con la detección de mayor confianza")
        return False
    if not any(d.class_id == 1 for d in merged):
        print("  FAIL: la detección de otra clase se perdió en la fusión")
        return False

    print("  OK")
    return True


def check_predict_cli_validation() -> bool:
    print("[6/7] Verificando validación de argumentos de predict.py (sin correr inferencia)...")
    result = subprocess.run(
        [
            sys.executable,
            "predict.py",
            "--model-path", "/nonexistent",
            "--image-path", "/nonexistent",
            "--image-file", "foto.tif",
            "--tile-size", "99999",  # fuera de rango a propósito
            "--overlap", "0.25",
            "--conf", "0.25",
            "--iou", "0.45",
            "--job-id", "j1",
            "--output-path", "/tmp/out",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
    )
    if result.returncode == 0:
        print("  FAIL: predict.py aceptó tile-size fuera de rango")
        return False
    print("  OK (rechazado correctamente)")
    return True


def check_predict_end_to_end() -> bool:
    print("[7/7] Verificando predict.py de punta a punta (yolov8n real, CPU, imagen sintética con múltiples tiles)...")
    from ultralytics import YOLO

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        model_dir = root / "model"
        image_dir = root / "image"
        output_dir = root / "output"
        model_dir.mkdir()
        image_dir.mkdir()

        # yolov8n.pt se descarga solo (pesos públicos de Ultralytics) y se
        # copia como si fuera el best.pt de un ModelVersion ya entrenado.
        pretrained = YOLO("yolov8n.pt")
        pretrained.save(str(model_dir / "best.pt"))

        # Imagen sintética de 1280x960 (> tile-size de 640) para forzar
        # varios tiles reales, con una forma simple para tener alguna chance
        # de detección real (no es determinante: el check no exige >0
        # detecciones, solo que el pipeline corra sin errores tile por tile).
        image_path = image_dir / "foto.jpg"
        Image.new("RGB", (1280, 960), (110, 140, 90)).save(image_path, format="JPEG")

        result = subprocess.run(
            [
                sys.executable,
                "predict.py",
                "--model-path", str(model_dir),
                "--image-path", str(image_dir),
                "--image-file", "foto.jpg",
                "--tile-size", "640",
                "--overlap", "0.25",
                "--conf", "0.25",
                "--iou", "0.45",
                "--job-id", "smoke-test",
                "--output-path", str(output_dir),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
        )
        if result.returncode != 0:
            print(f"  FAIL: predict.py terminó con código {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            return False

        detections_path = output_dir / "detections.json"
        preview_path = output_dir / "preview.png"
        manifest_path = output_dir / "manifest.json"
        for path in (detections_path, preview_path, manifest_path):
            if not path.exists():
                print(f"  FAIL: no se generó {path.name}")
                return False

        detections = json.loads(detections_path.read_text())
        if detections["tile_count"] < 2:
            print(f"  FAIL: se esperaban >=2 tiles para una imagen 1280x960 con tile-size 640, se obtuvieron {detections['tile_count']}")
            return False
        if detections["image_width"] != 1280 or detections["image_height"] != 960:
            print(f"  FAIL: dimensiones de imagen incorrectas en detections.json: {detections}")
            return False

    print(f"  OK ({detections['tile_count']} tiles procesados, {detections['detection_count']} detecciones)")
    return True


def main() -> int:
    checks = [
        check_imports,
        check_validate_dataset,
        check_manifest_generation,
        check_train_cli_validation,
        check_tiling,
        check_predict_cli_validation,
        check_predict_end_to_end,
    ]
    results = [check() for check in checks]
    if all(results):
        print("\nSMOKE TEST: PASS")
        return 0
    print("\nSMOKE TEST: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
