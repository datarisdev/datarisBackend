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
        allowed_model_bases=("yolo11n.pt", "yolo11s.pt", "yolo11m.pt"),
        default_model_base="yolo11n.pt",
    ),
    "ultralytics_yolo_segmentation": TrainingRecipe(
        key="ultralytics_yolo_segmentation",
        task_type=TrainingTaskType.SEGMENTATION,
        label="Segmentación de instancias (Ultralytics YOLO)",
        allowed_model_bases=("yolo11n-seg.pt", "yolo11s-seg.pt", "yolo11m-seg.pt"),
        default_model_base="yolo11n-seg.pt",
    ),
    "ultralytics_yolo_classification": TrainingRecipe(
        key="ultralytics_yolo_classification",
        task_type=TrainingTaskType.CLASSIFICATION,
        label="Clasificación de imágenes (Ultralytics YOLO)",
        allowed_model_bases=("yolo11n-cls.pt", "yolo11s-cls.pt", "yolo11m-cls.pt"),
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


def build_train_command(
    *,
    recipe: TrainingRecipe,
    model_base: str,
    config: TrainingJobConfig,
    job_id: str,
    project_id: str,
) -> str:
    """Genera la línea de comando fija que ejecuta train.py dentro de la imagen
    Docker de entrenamiento. Los valores están todos validados por Pydantic /
    esta misma función — nunca se interpola texto libre del usuario."""
    return (
        "python train.py "
        "--dataset-path ${{inputs.dataset}} "
        f"--task {recipe.task_type.value} "
        f"--recipe {recipe.key} "
        f"--model {model_base} "
        f"--epochs {int(config.epochs)} "
        f"--imgsz {int(config.image_size)} "
        f"--batch {int(config.batch_size)} "
        f"--lr {float(config.learning_rate)} "
        f"--patience {int(config.patience)} "
        f"--val-split {float(config.val_split)} "
        f"--seed {int(config.seed)} "
        f"--augment {'1' if config.augmentations_enabled else '0'} "
        f"--project-id {project_id} "
        f"--job-id {job_id} "
        "--output-path ${{outputs.output}}"
    )
