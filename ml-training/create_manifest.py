#!/usr/bin/env python
"""Genera manifest.json con la trazabilidad completa de un entrenamiento.

Registra: receta, modelo base, configuración usada, referencia de la imagen
Docker, versiones de dependencias, referencias a dataset/job, métricas y la
lista de artefactos generados con su tamaño. Este manifest es uno de los
artefactos que el backend registra en ModelArtifact (artifact_type=MANIFEST).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {"python": platform.python_version()}
    for module_name in ("torch", "ultralytics", "cv2", "numpy", "PIL"):
        try:
            module = __import__(module_name)
            versions[module_name] = getattr(module, "__version__", "unknown")
        except Exception:
            versions[module_name] = "not_installed"
    return versions


def build_manifest(
    *,
    job_id: str,
    project_id: str,
    recipe: str,
    task: str,
    model_base: str,
    config: dict,
    metrics: dict,
    output_dir: Path,
    docker_image_ref: str | None,
) -> dict:
    artifacts = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file():
            artifacts.append(
                {
                    "relative_path": str(path.relative_to(output_dir)),
                    "size_bytes": path.stat().st_size,
                }
            )

    return {
        "schema_version": 1,
        "job_id": job_id,
        "project_id": project_id,
        "recipe": recipe,
        "task": task,
        "model_base": model_base,
        "config": config,
        "metrics": metrics,
        "docker_image_ref": docker_image_ref,
        "dependency_versions": _dependency_versions(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera el manifest.json de un entrenamiento")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--model-base", required=True)
    parser.add_argument("--config-json", required=True, help="JSON string con la configuración usada")
    parser.add_argument("--metrics-json", default="{}", help="JSON string con métricas finales")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--docker-image-ref", default=None)
    args = parser.parse_args()

    manifest = build_manifest(
        job_id=args.job_id,
        project_id=args.project_id,
        recipe=args.recipe,
        task=args.task,
        model_base=args.model_base,
        config=json.loads(args.config_json),
        metrics=json.loads(args.metrics_json),
        output_dir=args.output_dir,
        docker_image_ref=args.docker_image_ref,
    )

    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Manifest escrito en {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
