#!/usr/bin/env python
"""Validación de estructura YOLO dentro de la imagen de entrenamiento.

Versión standalone (sin depender del paquete `app` del backend, ya que esta
imagen se construye y ejecuta de forma completamente aislada). La lógica
replica app/modules/ml_training/dataset_validation.py del backend —
mantener ambas en sync si se cambia el formato esperado del dataset.

Uso:
    python validate_dataset.py --dataset-path /ruta/al/dataset --task detection
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
LABEL_EXTENSION = ".txt"


def _find_data_yaml(root: Path) -> Path | None:
    candidates = list(root.rglob("data.yaml")) + list(root.rglob("data.yml"))
    return candidates[0] if candidates else None


def _split_dir(root: Path, split: str) -> Path | None:
    for candidate in root.rglob(split):
        if candidate.is_dir():
            return candidate
    return None


def _is_valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def validate(dataset_path: Path, task: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    data_yaml_path = _find_data_yaml(dataset_path)
    class_names: list[str] = []
    if data_yaml_path is None:
        errors.append("No se encontró data.yaml en el dataset")
    else:
        data_yaml = yaml.safe_load(data_yaml_path.read_text(encoding="utf-8")) or {}
        names = data_yaml.get("names")
        if isinstance(names, dict):
            class_names = [str(names[k]) for k in sorted(names, key=lambda x: int(x))]
        elif isinstance(names, list):
            class_names = [str(n) for n in names]
        else:
            errors.append("data.yaml no define 'names' válidamente")

    total_images = 0
    corrupted = 0
    per_split_counts: dict[str, int] = {}

    for split in ("train", "valid", "test"):
        split_dir = _split_dir(dataset_path, split) or (_split_dir(dataset_path, "val") if split == "valid" else None)
        if split_dir is None:
            per_split_counts[split] = 0
            if split in ("train", "valid"):
                errors.append(f"Falta la carpeta obligatoria '{split}'")
            continue
        images_dir = split_dir / "images" if (split_dir / "images").exists() else split_dir
        images = [p for p in images_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
        per_split_counts[split] = len(images)
        total_images += len(images)
        for image_path in images:
            if not _is_valid_image(image_path):
                corrupted += 1

    if corrupted:
        errors.append(f"{corrupted} imagen(es) corrupta(s)")
    if total_images < 10:
        errors.append("El dataset tiene muy pocas imágenes (mínimo recomendado: 10)")
    if not class_names:
        errors.append("El dataset no define ninguna clase")

    return {
        "is_valid": len(errors) == 0,
        "image_count": total_images,
        "class_count": len(class_names),
        "class_names": class_names,
        "per_split_counts": per_split_counts,
        "errors": errors,
        "warnings": warnings,
        "task": task,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Valida un dataset YOLO antes de entrenar")
    parser.add_argument("--dataset-path", required=True, type=Path)
    parser.add_argument("--task", required=True, choices=["detection", "segmentation", "classification"])
    args = parser.parse_args()

    report = validate(args.dataset_path, args.task)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["is_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
