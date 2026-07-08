"""Registro controlado de recetas de entrenamiento.

Los usuarios nunca envían comandos ni código: solo eligen una receta
registrada aquí y parámetros validados por TrainingJobConfig (schemas.py).
Este módulo es el único lugar que sabe traducir esos parámetros en el
comando real que ejecuta la imagen Docker de entrenamiento
(datarisBackend/ml-training/train.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.ml_training import TrainingTaskType
from app.modules.ml_training.schemas import TrainingJobConfig


class UnknownRecipeError(ValueError):
    pass


class UnsupportedModelBaseError(ValueError):
    pass


@dataclass(frozen=True)
class TrainingRecipe:
    key: str
    task_type: TrainingTaskType
    label: str
    allowed_model_bases: tuple[str, ...]
    default_model_base: str
    implemented: bool = True


# Recetas soportadas hoy (Ultralytics YOLO) + placeholders documentados para
# el futuro (implemented=False -> rechazadas explícitamente hasta activarse).
RECIPES: dict[str, TrainingRecipe] = {
    "ultralytics_yolo_detection": TrainingRecipe(
        key="ultralytics_yolo_detection",
        task_type=TrainingTaskType.DETECTION,
        label="Detección de objetos (Ultralytics YOLO)",
        allowed_model_bases=("yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt", "yolo11x.pt"),
        default_model_base="yolo11n.pt",
    ),
    "ultralytics_yolo_segmentation": TrainingRecipe(
        key="ultralytics_yolo_segmentation",
        task_type=TrainingTaskType.SEGMENTATION,
        label="Segmentación de instancias (Ultralytics YOLO)",
        allowed_model_bases=("yolo11n-seg.pt", "yolo11s-seg.pt", "yolo11m-seg.pt", "yolo11l-seg.pt", "yolo11x-seg.pt"),
        default_model_base="yolo11n-seg.pt",
    ),
    "ultralytics_yolo_classification": TrainingRecipe(
        key="ultralytics_yolo_classification",
        task_type=TrainingTaskType.CLASSIFICATION,
        label="Clasificación de imágenes (Ultralytics YOLO)",
        allowed_model_bases=("yolo11n-cls.pt", "yolo11s-cls.pt", "yolo11m-cls.pt", "yolo11l-cls.pt", "yolo11x-cls.pt"),
        default_model_base="yolo11n-cls.pt",
    ),
    "future_detectron2_detection": TrainingRecipe(
        key="future_detectron2_detection",
        task_type=TrainingTaskType.DETECTION,
        label="Detección de objetos (Detectron2) — próximamente",
        allowed_model_bases=(),
        default_model_base="",
        implemented=False,
    ),
    "future_mmdetection_segmentation": TrainingRecipe(
        key="future_mmdetection_segmentation",
        task_type=TrainingTaskType.SEGMENTATION,
        label="Segmentación (MMDetection) — próximamente",
        allowed_model_bases=(),
        default_model_base="",
        implemented=False,
    ),
    "future_tensorflow_classification": TrainingRecipe(
        key="future_tensorflow_classification",
        task_type=TrainingTaskType.CLASSIFICATION,
        label="Clasificación (TensorFlow) — próximamente",
        allowed_model_bases=(),
        default_model_base="",
        implemented=False,
    ),
}

MODE_PRESETS: dict[str, dict[str, object]] = {
    "fast": {"epochs": 20, "batch_size": 16, "image_size": 480, "patience": 5},
    "balanced": {"epochs": 50, "batch_size": 16, "image_size": 640, "patience": 20},
    "accurate": {"epochs": 120, "batch_size": 8, "image_size": 832, "patience": 40},
}


def get_recipe(recipe_key: str) -> TrainingRecipe:
    recipe = RECIPES.get(recipe_key)
    if recipe is None:
        raise UnknownRecipeError(f"Receta desconocida: {recipe_key}")
    if not recipe.implemented:
        raise UnknownRecipeError(f"La receta '{recipe_key}' aún no está disponible")
    return recipe


def validate_model_base(recipe: TrainingRecipe, model_base: str) -> None:
    if model_base not in recipe.allowed_model_bases:
        raise UnsupportedModelBaseError(
            f"model_base '{model_base}' no es válido para la receta '{recipe.key}'. "
            f"Opciones permitidas: {', '.join(recipe.allowed_model_bases)}"
        )


def apply_mode_preset(config: TrainingJobConfig) -> TrainingJobConfig:
    """En modo simple (fast/balanced/accurate) sobrescribe con valores seguros
    recomendados; en modo 'advanced' respeta los valores que ya vengan en config."""
    if config.mode == "advanced":
        return config
    preset = MODE_PRESETS.get(config.mode)
    if not preset:
        return config
    return config.model_copy(update=preset)


# Rutas locales fijas dentro del contenedor de entrenamiento/inferencia. El
# wrapper (ml-training/entrypoint.py) descarga cada input de Blob Storage a
# su carpeta /mnt/<nombre> antes de invocar train.py/predict.py, y sube
# /mnt/output de vuelta al terminar — Container Apps Jobs, a diferencia de
# Azure ML, no monta carpetas de blob automáticamente.
DATASET_MOUNT_PATH = "/mnt/dataset"
MODEL_MOUNT_PATH = "/mnt/model"
IMAGE_MOUNT_PATH = "/mnt/image"
OUTPUT_MOUNT_PATH = "/mnt/output"


def build_train_command(
    *,
    recipe: TrainingRecipe,
    model_base: str,
    config: TrainingJobConfig,
    job_id: str,
    project_id: str,
) -> tuple[list[str], list[str]]:
    """Genera el (command, args) fijo que ejecuta train.py dentro de la imagen
    Docker de entrenamiento, como lista de argv (sin shell de por medio, así
    que no hace falta escapar nada). Los valores están todos validados por
    Pydantic / esta misma función — nunca se interpola texto libre del
    usuario."""
    return (
        ["python", "train.py"],
        [
            "--dataset-path", DATASET_MOUNT_PATH,
            "--task", recipe.task_type.value,
            "--recipe", recipe.key,
            "--model", model_base,
            "--epochs", str(int(config.epochs)),
            "--imgsz", str(int(config.image_size)),
            "--batch", str(int(config.batch_size)),
            "--lr", str(float(config.learning_rate)),
            "--patience", str(int(config.patience)),
            "--val-split", str(float(config.val_split)),
            "--seed", str(int(config.seed)),
            "--augment", "1" if config.augmentations_enabled else "0",
            "--project-id", project_id,
            "--job-id", job_id,
            "--output-path", OUTPUT_MOUNT_PATH,
        ],
    )


def build_predict_command(
    *,
    image_file_name: str,
    tile_size: int,
    overlap: float,
    confidence_threshold: float,
    iou_threshold: float,
    job_id: str,
) -> tuple[list[str], list[str]]:
    """Genera el (command, args) fijo que ejecuta predict.py dentro de la
    misma imagen Docker de entrenamiento (mismo mecanismo que
    build_train_command). `image_file_name` viene del nombre original de un
    archivo subido por el usuario; al no pasar por un shell no necesita
    shlex.quote (ya se sanea igual antes en inference_service.py)."""
    return (
        ["python", "predict.py"],
        [
            "--model-path", MODEL_MOUNT_PATH,
            "--image-path", IMAGE_MOUNT_PATH,
            "--image-file", image_file_name,
            "--tile-size", str(int(tile_size)),
            "--overlap", str(float(overlap)),
            "--conf", str(float(confidence_threshold)),
            "--iou", str(float(iou_threshold)),
            "--job-id", job_id,
            "--output-path", OUTPUT_MOUNT_PATH,
        ],
    )
