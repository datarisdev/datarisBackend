import io
import zipfile
from pathlib import Path

import pytest
import yaml
from PIL import Image

from app.models.ml_training import TrainingTaskType
from app.modules.ml_training.dataset_validation import (
    DatasetSecurityError,
    inspect_zip_safety,
    safe_extract_zip,
    validate_yolo_dataset,
)


def _make_image_bytes(size=(32, 32), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _write_zip(path: Path, entries: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


class TestZipSecurity:
    def test_rejects_path_traversal(self, tmp_path):
        zip_path = tmp_path / "evil.zip"
        _write_zip(zip_path, {"../../etc/passwd": b"pwned"})
        with pytest.raises(DatasetSecurityError):
            inspect_zip_safety(zip_path)

    def test_safe_extract_ignores_traversal_entries(self, tmp_path):
        # Incluso si inspect_zip_safety no se llamara antes, safe_extract_zip
        # por sí solo debe negarse a escribir fuera del destino.
        zip_path = tmp_path / "evil2.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zi = zipfile.ZipInfo("../../outside.txt")
            zf.writestr(zi, b"pwned")
        dest = tmp_path / "dest"
        safe_extract_zip(zip_path, dest)
        assert not (tmp_path / "outside.txt").exists()
        assert list(dest.rglob("*")) == [] or all("outside" not in str(p) for p in dest.rglob("*"))

    def test_rejects_oversized_uncompressed_total(self, tmp_path):
        zip_path = tmp_path / "big.zip"
        _write_zip(zip_path, {"a.txt": b"x" * 1000})
        with pytest.raises(DatasetSecurityError):
            inspect_zip_safety(zip_path, max_total_uncompressed_bytes=100)

    def test_rejects_zip_bomb_ratio(self, tmp_path):
        # 10MB de ceros comprime a un ratio absurdo con ZIP_DEFLATED.
        zip_path = tmp_path / "bomb.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.bin", b"\x00" * (10 * 1024 * 1024))
        with pytest.raises(DatasetSecurityError):
            inspect_zip_safety(zip_path, max_compression_ratio=50)

    def test_accepts_safe_zip(self, tmp_path):
        zip_path = tmp_path / "ok.zip"
        _write_zip(zip_path, {"images/a.jpg": _make_image_bytes()})
        inspect_zip_safety(zip_path)  # no debe lanzar


class TestYoloDatasetValidation:
    def _build_dataset(self, root: Path, n_images_train=15, n_images_valid=5, classes=("weed", "crop")):
        data_yaml = {"names": list(classes), "nc": len(classes)}
        (root / "data.yaml").write_text(yaml.safe_dump(data_yaml))

        for split, n in (("train", n_images_train), ("valid", n_images_valid)):
            images_dir = root / split / "images"
            labels_dir = root / split / "labels"
            images_dir.mkdir(parents=True)
            labels_dir.mkdir(parents=True)
            for i in range(n):
                img_path = images_dir / f"img_{i}.jpg"
                img_path.write_bytes(_make_image_bytes())
                label_path = labels_dir / f"img_{i}.txt"
                class_id = i % len(classes)
                label_path.write_text(f"{class_id} 0.5 0.5 0.2 0.2\n")

    def test_valid_dataset_passes(self, tmp_path):
        self._build_dataset(tmp_path)
        report = validate_yolo_dataset(tmp_path, TrainingTaskType.DETECTION)
        assert report.is_valid
        assert report.image_count == 20
        assert report.class_count == 2
        assert not any(i.level == "error" for i in report.issues)

    def test_missing_data_yaml_is_error(self, tmp_path):
        (tmp_path / "train" / "images").mkdir(parents=True)
        report = validate_yolo_dataset(tmp_path, TrainingTaskType.DETECTION)
        assert not report.is_valid
        assert any(i.code == "missing_data_yaml" for i in report.issues)

    def test_missing_valid_split_is_error(self, tmp_path):
        data_yaml = {"names": ["weed"], "nc": 1}
        (tmp_path / "data.yaml").write_text(yaml.safe_dump(data_yaml))
        images_dir = tmp_path / "train" / "images"
        labels_dir = tmp_path / "train" / "labels"
        images_dir.mkdir(parents=True)
        labels_dir.mkdir(parents=True)
        for i in range(15):
            (images_dir / f"i{i}.jpg").write_bytes(_make_image_bytes())
            (labels_dir / f"i{i}.txt").write_text("0 0.5 0.5 0.2 0.2\n")

        report = validate_yolo_dataset(tmp_path, TrainingTaskType.DETECTION)
        assert not report.is_valid
        assert any(i.code == "missing_split" and "valid" in i.message for i in report.issues)

    def test_corrupted_image_detected(self, tmp_path):
        self._build_dataset(tmp_path, n_images_train=12, n_images_valid=5)
        corrupted = tmp_path / "train" / "images" / "img_0.jpg"
        corrupted.write_bytes(b"not a real image")
        report = validate_yolo_dataset(tmp_path, TrainingTaskType.DETECTION)
        assert not report.is_valid
        assert any(i.code == "corrupted_images" for i in report.issues)

    def test_dataset_too_small_is_error(self, tmp_path):
        self._build_dataset(tmp_path, n_images_train=2, n_images_valid=1)
        report = validate_yolo_dataset(tmp_path, TrainingTaskType.DETECTION)
        assert not report.is_valid
        assert any(i.code == "dataset_too_small" for i in report.issues)
