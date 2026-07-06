#!/usr/bin/env python
"""Entrenamiento controlado de modelos Ultralytics YOLO.

Este script SOLO acepta parámetros ya validados por el backend
(app/modules/ml_training/training_recipes.py::build_train_command). No
ejecuta código ni comandos arbitrarios: cada argumento tiene un tipo y un
rango fijos vía argparse, igual que TrainingJobConfig en el backend.

Uso real (generado por Azure ML, ver azure_ml_client.py):
    python train.py --dataset-path <input> --task detection \
        --recipe ultralytics_yolo_detection --model yolo11n.pt \
        --epochs 50 --imgsz 640 --batch 16 --lr 0.01 --patience 20 \
        --val-split 0.2 --seed 0 --augment 1 \
        --project-id <uuid> --job-id <uuid> --output-path <output>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from validate_dataset import validate as validate_dataset
from create_manifest import build_manifest

SUPPORTED_RECIPES = (
    "ultralytics_yolo_detection",
    "ultralytics_yolo_segmentation",
    "ultralytics_yolo_classification",
)


def _log(event: str, **fields) -> None:
    """Log estructurado en una línea JSON por evento (fácil de parsear en
    Azure ML / Application Insights sin depender de un formato de texto libre)."""
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Entrena un modelo Ultralytics YOLO para Dataris")
    parser.add_argument("--dataset-path", required=True, type=Path)
    parser.add_argument("--task", required=True, choices=["detection", "segmentation", "classification"])
    parser.add_argument("--recipe", required=True, choices=list(SUPPORTED_RECIPES))
    parser.add_argument("--model", required=True)
    parser.add_argument("--epochs", required=True, type=int)
    parser.add_argument("--imgsz", required=True, type=int)
    parser.add_argument("--batch", required=True, type=int)
    parser.add_argument("--lr", required=True, type=float)
    parser.add_argument("--patience", required=True, type=int)
    parser.add_argument("--val-split", required=True, type=float)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--augment", required=True, choices=["0", "1"])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-path", required=True, type=Path)
    args = parser.parse_args()

    if not (1 <= args.epochs <= 500):
        parser.error("epochs fuera de rango (1-500)")
    if not (1 <= args.batch <= 128):
        parser.error("batch fuera de rango (1-128)")
    if not (32 <= args.imgsz <= 1920):
        parser.error("imgsz fuera de rango (32-1920)")
    if not (0 < args.lr <= 1):
        parser.error("lr fuera de rango (0-1]")

    return args


def _find_data_yaml(dataset_path: Path) -> Path:
    candidates = list(dataset_path.rglob("data.yaml")) + list(dataset_path.rglob("data.yml"))
    if not candidates:
        raise FileNotFoundError("data.yaml no encontrado en el dataset")
    return candidates[0]


def _make_progress_callback(output_dir: Path, total_epochs: int):
    """progress.json se escribe en la raíz de output_dir (no en el subdirectorio
    del run) en cada fin de época, para que el backend lo lea vía blob storage
    y muestre avance real (app/modules/ml_training/service.py::_sync_job_progress)."""
    progress_path = output_dir / "progress.json"

    def _on_train_epoch_end(trainer) -> None:
        metrics = {}
        try:
            metrics = {k: float(v) for k, v in (trainer.metrics or {}).items()}
        except Exception:
            pass
        payload = {
            "current_epoch": trainer.epoch + 1,
            "total_epochs": total_epochs,
            "metrics": metrics,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            progress_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    return _on_train_epoch_end


def main() -> int:
    args = _parse_args()
    output_dir = args.output_path
    output_dir.mkdir(parents=True, exist_ok=True)

    _log("job_started", job_id=args.job_id, project_id=args.project_id, recipe=args.recipe)

    try:
        _log("validating_dataset")
        report = validate_dataset(args.dataset_path, args.task)
        if not report["is_valid"]:
            raise ValueError(f"Dataset inválido: {report['errors']}")
        _log("dataset_valid", image_count=report["image_count"], class_count=report["class_count"])

        data_yaml_path = _find_data_yaml(args.dataset_path)

        from ultralytics import YOLO

        _log("loading_model", model=args.model)
        model = YOLO(args.model)
        model.add_callback("on_train_epoch_end", _make_progress_callback(output_dir, args.epochs))

        run_name = f"job-{args.job_id}"
        _log("training_started", epochs=args.epochs, imgsz=args.imgsz, batch=args.batch)
        results = model.train(
            data=str(data_yaml_path),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            lr0=args.lr,
            patience=args.patience,
            seed=args.seed,
            augment=args.augment == "1",
            project=str(output_dir),
            name=run_name,
            exist_ok=True,
        )
        _log("training_completed")

        run_dir = output_dir / run_name
        weights_dir = run_dir / "weights"
        for weight_name in ("best.pt", "last.pt"):
            src = weights_dir / weight_name
            if src.exists():
                shutil.copy2(src, output_dir / weight_name)

        metrics = {}
        try:
            metrics = {k: float(v) for k, v in (results.results_dict or {}).items()}
        except Exception:
            pass
        (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        shutil.copy2(data_yaml_path, output_dir / "data.yaml")

        config = {
            "epochs": args.epochs,
            "imgsz": args.imgsz,
            "batch": args.batch,
            "lr": args.lr,
            "patience": args.patience,
            "val_split": args.val_split,
            "seed": args.seed,
            "augment": args.augment == "1",
        }
        (output_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

        manifest = build_manifest(
            job_id=args.job_id,
            project_id=args.project_id,
            recipe=args.recipe,
            task=args.task,
            model_base=args.model,
            config=config,
            metrics=metrics,
            output_dir=output_dir,
            docker_image_ref=None,
        )
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        _log("job_finished", status="completed")
        return 0

    except Exception as exc:  # noqa: BLE001 - error sanitizado hacia stdout/error.json
        _log("job_failed", error=str(exc))
        error_payload = {"error": str(exc), "traceback": traceback.format_exc()}
        (output_dir / "error.json").write_text(json.dumps(error_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
