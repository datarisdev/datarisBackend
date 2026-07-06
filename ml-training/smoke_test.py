#!/usr/bin/env python
"""Smoke test local de la imagen de entrenamiento — SIN GPU y SIN entrenar
un modelo real (eso requeriría GPU y minutos/horas). Verifica:

1. Que todas las dependencias pesadas importan correctamente.
2. Que validate_dataset.py detecta un dataset sintético válido e inválido.
3. Que create_manifest.py genera un manifest.json con las claves esperadas.
4. Que train.py rechaza argumentos fuera de rango (sin llegar a entrenar).

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


def main() -> int:
    checks = [check_imports, check_validate_dataset, check_manifest_generation, check_train_cli_validation]
    results = [check() for check in checks]
    if all(results):
        print("\nSMOKE TEST: PASS")
        return 0
    print("\nSMOKE TEST: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
