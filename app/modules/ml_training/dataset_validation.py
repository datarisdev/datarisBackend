"""Validación segura de datasets de visión por computadora.

Cubre: extracción segura de ZIP (protección path traversal / ZIP Slip),
protección contra ZIP bombs, y validación de estructura YOLO (data.yaml,
train/valid/test, clases, imágenes corruptas, desbalance).

No ejecuta ni permite código proveniente del usuario; solo lee archivos.
"""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from PIL import Image

from app.modules.ml_training.schemas import DatasetValidationIssue, DatasetValidationReport
from app.models.ml_training import TrainingTaskType

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
LABEL_EXTENSION = ".txt"
SPLIT_NAMES = ("train", "valid", "val", "test")

# Límites de seguridad de ZIP (ajustables vía settings, ver app/core/config.py)
DEFAULT_MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
DEFAULT_MAX_FILE_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB por archivo
DEFAULT_MAX_COMPRESSION_RATIO = 100  # uncompressed / compressed
DEFAULT_MAX_FILE_COUNT = 200_000


class DatasetSecurityError(ValueError):
    """Se produce cuando el ZIP viola un límite de seguridad (bomb, slip, tamaño)."""


@dataclass
class _MutableReport:
    issues: list[DatasetValidationIssue] = field(default_factory=list)

    def error(self, code: str, message: str) -> None:
        self.issues.append(DatasetValidationIssue(level="error", code=code, message=message))

    def warning(self, code: str, message: str) -> None:
        self.issues.append(DatasetValidationIssue(level="warning", code=code, message=message))


def _is_safe_member_name(name: str) -> bool:
    normalized = name.replace("\\", "/").lstrip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized or normalized == "..":
        return False
    return True


def inspect_zip_safety(
    zip_path: Path,
    max_total_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_file_uncompressed_bytes: int = DEFAULT_MAX_FILE_UNCOMPRESSED_BYTES,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
) -> None:
    """Lanza DatasetSecurityError si el ZIP es peligroso. No extrae nada."""
    with zipfile.ZipFile(zip_path) as zf:
        infolist = zf.infolist()
        if len(infolist) > max_file_count:
            raise DatasetSecurityError(f"El ZIP contiene demasiados archivos ({len(infolist)} > {max_file_count})")

        total_uncompressed = 0
        for member in infolist:
            if not _is_safe_member_name(member.filename):
                raise DatasetSecurityError(f"Nombre de archivo inseguro en el ZIP: {member.filename}")

            if member.file_size > max_file_uncompressed_bytes:
                raise DatasetSecurityError(
                    f"Archivo demasiado grande dentro del ZIP: {member.filename} ({member.file_size} bytes)"
                )

            if member.compress_size > 0:
                ratio = member.file_size / member.compress_size
                if ratio > max_compression_ratio:
                    raise DatasetSecurityError(
                        f"Ratio de compresión sospechoso (posible ZIP bomb) en {member.filename}: {ratio:.1f}x"
                    )

            total_uncompressed += member.file_size
            if total_uncompressed > max_total_uncompressed_bytes:
                raise DatasetSecurityError(
                    f"El contenido descomprimido del ZIP excede el límite permitido ({max_total_uncompressed_bytes} bytes)"
                )


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """Extrae un ZIP ya validado por inspect_zip_safety() con protección ZIP Slip."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/").lstrip("/")
            if not _is_safe_member_name(member.filename):
                continue
            target = (destination / name).resolve()
            if not str(target).startswith(str(root)):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                dst.write(src.read())


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


def validate_yolo_dataset(root: Path, task_type: TrainingTaskType) -> DatasetValidationReport:
    """Valida estructura YOLO ya extraída de forma segura en `root`."""
    report = _MutableReport()

    data_yaml_path = _find_data_yaml(root)
    class_names: list[str] = []
    if data_yaml_path is None:
        report.error("missing_data_yaml", "No se encontró data.yaml en el dataset")
    else:
        try:
            with data_yaml_path.open("r", encoding="utf-8") as fh:
                data_yaml = yaml.safe_load(fh) or {}
            names = data_yaml.get("names")
            if isinstance(names, dict):
                class_names = [str(names[k]) for k in sorted(names, key=lambda x: int(x))]
            elif isinstance(names, list):
                class_names = [str(n) for n in names]
            else:
                report.error("invalid_data_yaml", "data.yaml no define 'names' válidamente")
        except Exception as exc:
            report.error("invalid_data_yaml", f"No se pudo leer data.yaml: {exc}")

    if not class_names:
        report.error("no_classes", "El dataset no define ninguna clase")

    per_split_counts: dict[str, int] = {}
    per_class_counts: dict[str, int] = {name: 0 for name in class_names}
    total_images = 0
    total_size_bytes = 0
    corrupted_images = 0
    orphan_images = 0
    orphan_labels = 0

    found_any_split = False
    for split in ("train", "valid", "test"):
        split_dir = _split_dir(root, split) or _split_dir(root, "val" if split == "valid" else split)
        if split_dir is None:
            if split in ("train", "valid"):
                report.error("missing_split", f"Falta la carpeta obligatoria '{split}'")
            else:
                report.warning("missing_split", f"No se encontró carpeta opcional '{split}'")
            per_split_counts[split] = 0
            continue

        found_any_split = True
        images_dir = split_dir / "images" if (split_dir / "images").exists() else split_dir
        labels_dir = split_dir / "labels" if (split_dir / "labels").exists() else split_dir

        images = [p for p in images_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
        per_split_counts[split] = len(images)
        total_images += len(images)

        for image_path in images:
            total_size_bytes += image_path.stat().st_size
            if not _is_valid_image(image_path):
                corrupted_images += 1
                continue

            label_path = labels_dir / (image_path.stem + LABEL_EXTENSION)
            if task_type in (TrainingTaskType.DETECTION, TrainingTaskType.SEGMENTATION):
                if not label_path.exists():
                    orphan_images += 1
                    continue
                try:
                    lines = [l for l in label_path.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
                except Exception:
                    lines = []
                for line in lines:
                    parts = line.split()
                    if not parts:
                        continue
                    try:
                        class_id = int(parts[0])
                    except ValueError:
                        report.warning("invalid_label", f"Etiqueta no numérica en {label_path.name}")
                        continue
                    if 0 <= class_id < len(class_names):
                        per_class_counts[class_names[class_id]] += 1
                    else:
                        report.error("class_out_of_range", f"Clase {class_id} fuera de rango en {label_path.name}")

        if task_type in (TrainingTaskType.DETECTION, TrainingTaskType.SEGMENTATION) and labels_dir.exists():
            label_files = {p.stem for p in labels_dir.rglob("*" + LABEL_EXTENSION)}
            image_stems = {p.stem for p in images}
            orphan_labels += len(label_files - image_stems)

    if not found_any_split:
        report.error("empty_dataset", "No se encontraron imágenes en ninguna carpeta train/valid/test")

    if corrupted_images:
        report.error("corrupted_images", f"{corrupted_images} imagen(es) corrupta(s) o ilegibles")

    if orphan_images:
        report.warning("images_without_annotations", f"{orphan_images} imagen(es) sin anotación asociada")

    if orphan_labels:
        report.warning("annotations_without_images", f"{orphan_labels} anotación(es) sin imagen asociada")

    if total_images < 10:
        report.error("dataset_too_small", "El dataset tiene muy pocas imágenes (mínimo recomendado: 10)")

    if per_class_counts:
        non_zero = [c for c in per_class_counts.values() if c > 0]
        if non_zero:
            max_c, min_c = max(non_zero), min(non_zero)
            if min_c > 0 and max_c / min_c > 20:
                report.warning("class_imbalance", "Fuerte desbalance entre clases (ratio > 20x)")
        empty_classes = [name for name, count in per_class_counts.items() if count == 0]
        if empty_classes:
            report.warning("empty_classes", f"Clases sin ejemplos: {', '.join(empty_classes)}")

    is_valid = not any(issue.level == "error" for issue in report.issues)

    return DatasetValidationReport(
        is_valid=is_valid,
        image_count=total_images,
        class_count=len(class_names),
        class_names=class_names,
        per_split_counts=per_split_counts,
        per_class_counts=per_class_counts,
        total_size_bytes=total_size_bytes,
        issues=report.issues,
    )
