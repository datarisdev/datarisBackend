#!/usr/bin/env python
"""Exporta un checkpoint .pt entrenado a formato ONNX (opcional, ver
ModelArtifactType.ONNX en el backend). No forma parte del flujo obligatorio
de un entrenamiento; se invoca solo si el usuario pide el artefacto ONNX.

Uso:
    python export_model.py --weights best.pt --output model.onnx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Exporta un modelo Ultralytics a ONNX")
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args()

    if not args.weights.exists():
        print(f"No existe el archivo de pesos: {args.weights}", file=sys.stderr)
        return 1

    from ultralytics import YOLO

    model = YOLO(str(args.weights))
    exported_path = model.export(format="onnx", imgsz=args.imgsz)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    Path(exported_path).rename(args.output)
    print(f"Modelo exportado a {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
