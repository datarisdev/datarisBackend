"""Wrapper sobre Azure Machine Learning SDK v2 (azure-ai-ml).

Plano de control únicamente: este módulo crea/consulta/cancela `command`
jobs de Azure ML y registra modelos. El entrenamiento pesado (PyTorch,
Ultralytics) corre en la imagen Docker separada
(datarisBackend/ml-training/), nunca dentro de este proceso FastAPI.

Reutiliza la misma cadena de credenciales que app/utils/azure_blob.py
(Managed Identity en Azure, DefaultAzureCredential/Azure CLI en local) para
no duplicar lógica de autenticación.

No hay un Azure ML Workspace desplegado todavía en este entorno (ver
datarisInfra/ml_training_workspace.tf, ml_enabled=false por defecto), así
que este cliente se valida con mocks del SDK en tests
(tests/ml_training/test_azure_ml_client.py). Variables requeridas para
funcionar contra un workspace real: AZURE_ML_SUBSCRIPTION_ID,
AZURE_ML_RESOURCE_GROUP, AZURE_ML_WORKSPACE_NAME, AZURE_ML_COMPUTE_NAME.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.models.ml_training import TrainingJobStatus
from app.utils.azure_blob import get_token_credential, AzureBlobStorageError

logger = logging.getLogger(__name__)

_AZURE_ML_SDK_IMPORT_ERROR: Exception | None = None

try:
    from azure.ai.ml import MLClient, command, Input, Output
    from azure.ai.ml.entities import Environment, Model
    from azure.ai.ml.constants import AssetTypes
    from azure.core.exceptions import AzureError, ResourceNotFoundError
except Exception as exc:  # pragma: no cover - exercised only without dependencies.
    _AZURE_ML_SDK_IMPORT_ERROR = exc
    MLClient = None  # type: ignore[assignment]
    command = None  # type: ignore[assignment]
    Input = None  # type: ignore[assignment]
    Output = None  # type: ignore[assignment]
    Environment = None  # type: ignore[assignment]
    Model = None  # type: ignore[assignment]
    AssetTypes = None  # type: ignore[assignment]
    AzureError = Exception  # type: ignore[assignment]
    ResourceNotFoundError = Exception  # type: ignore[assignment]


class AzureMLClientError(RuntimeError):
    """Error sanitizado de la integración con Azure ML."""


# Azure ML job status -> estado interno de Dataris. Cualquier valor no
# reconocido se trata como RUNNING (nunca se pierde un job "en curso").
_STATUS_MAP: dict[str, TrainingJobStatus] = {
    "NotStarted": TrainingJobStatus.QUEUED,
    "Starting": TrainingJobStatus.QUEUED,
    "Provisioning": TrainingJobStatus.PROVISIONING_COMPUTE,
    "Preparing": TrainingJobStatus.PROVISIONING_COMPUTE,
    "Queued": TrainingJobStatus.QUEUED,
    "Running": TrainingJobStatus.RUNNING,
    "Finalizing": TrainingJobStatus.FINALIZING,
    "CancelRequested": TrainingJobStatus.RUNNING,
    "Completed": TrainingJobStatus.COMPLETED,
    "Failed": TrainingJobStatus.FAILED,
    "Canceled": TrainingJobStatus.CANCELLED,
    "NotResponding": TrainingJobStatus.RUNNING,
    "Paused": TrainingJobStatus.RUNNING,
}


def translate_azure_status(azure_status: str | None) -> TrainingJobStatus:
    if not azure_status:
        return TrainingJobStatus.QUEUED
    return _STATUS_MAP.get(azure_status, TrainingJobStatus.RUNNING)


@dataclass
class AzureMLJobSpec:
    job_name: str
    display_name: str
    command_line: str
    docker_image: str
    compute_target: str
    dataset_uri: str
    output_uri: str
    timeout_minutes: int
    tags: dict[str, str] = field(default_factory=dict)
    environment_variables: dict[str, str] = field(default_factory=dict)


def _require_sdk() -> None:
    if _AZURE_ML_SDK_IMPORT_ERROR is not None:
        raise AzureMLClientError(
            "El SDK azure-ai-ml no está instalado. Añade azure-ai-ml a las dependencias del backend."
        ) from _AZURE_ML_SDK_IMPORT_ERROR


def azure_ml_disabled() -> bool:
    return (os.getenv("AZURE_ML_ENABLED") or "false").strip().lower() not in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise AzureMLClientError(f"{name} no está configurada. El módulo de entrenamiento no está habilitado.")
    return value


@lru_cache(maxsize=1)
def get_ml_client() -> "MLClient":
    _require_sdk()
    try:
        credential = get_token_credential()
    except AzureBlobStorageError as exc:
        raise AzureMLClientError("No se pudo obtener credenciales de Azure") from exc

    return MLClient(
        credential=credential,
        subscription_id=_required_env("AZURE_ML_SUBSCRIPTION_ID"),
        resource_group_name=_required_env("AZURE_ML_RESOURCE_GROUP"),
        workspace_name=_required_env("AZURE_ML_WORKSPACE_NAME"),
    )


def reset_azure_ml_client() -> None:
    get_ml_client.cache_clear()


def default_compute_target() -> str:
    return (os.getenv("AZURE_ML_COMPUTE_NAME") or "").strip() or "gpu-cluster"


def default_docker_image() -> str:
    return (os.getenv("AZURE_ML_TRAINING_IMAGE") or "").strip()


def submit_training_job(spec: AzureMLJobSpec) -> str:
    """Envía un command job a Azure ML. Devuelve el nombre del job (== su ID)."""
    if azure_ml_disabled():
        raise AzureMLClientError(
            "El módulo de Azure ML está deshabilitado (AZURE_ML_ENABLED=false). "
            "Actívalo solo tras aprobar la infraestructura y la cuota GPU."
        )
    client = get_ml_client()
    _require_sdk()

    env = Environment(image=spec.docker_image)

    job = command(
        name=spec.job_name,
        display_name=spec.display_name,
        command=spec.command_line,
        environment=env,
        compute=spec.compute_target,
        inputs={"dataset": Input(type=AssetTypes.URI_FOLDER, path=spec.dataset_uri)},
        outputs={"output": Output(type=AssetTypes.URI_FOLDER, path=spec.output_uri)},
        timeout=spec.timeout_minutes * 60,
        tags=spec.tags,
        environment_variables=spec.environment_variables,
    )

    try:
        submitted = client.jobs.create_or_update(job)
    except AzureError as exc:
        logger.exception("Azure ML job submission failed")
        raise AzureMLClientError("No se pudo enviar el job a Azure ML") from exc

    return submitted.name


def get_job(azure_ml_job_name: str) -> dict[str, Any]:
    client = get_ml_client()
    try:
        job = client.jobs.get(azure_ml_job_name)
    except ResourceNotFoundError as exc:
        raise AzureMLClientError("El job ya no existe en Azure ML") from exc
    except AzureError as exc:
        raise AzureMLClientError("No se pudo consultar el job en Azure ML") from exc

    return {
        "name": job.name,
        "status": job.status,
        "internal_status": translate_azure_status(job.status),
        "studio_url": getattr(job, "studio_url", None),
        "tags": dict(job.tags or {}),
    }


def cancel_job(azure_ml_job_name: str) -> None:
    client = get_ml_client()
    try:
        client.jobs.begin_cancel(azure_ml_job_name)
    except ResourceNotFoundError:
        return
    except AzureError as exc:
        raise AzureMLClientError("No se pudo cancelar el job en Azure ML") from exc


def get_job_metrics(azure_ml_job_name: str) -> dict[str, Any]:
    """Lee métricas registradas por el job (MLFlow tracking de Azure ML)."""
    client = get_ml_client()
    try:
        job = client.jobs.get(azure_ml_job_name)
    except AzureError as exc:
        raise AzureMLClientError("No se pudo leer métricas del job") from exc
    return dict(getattr(job, "properties", {}) or {})


def register_model(
    *,
    name: str,
    version: str,
    path: str,
    description: str | None = None,
    tags: dict[str, str] | None = None,
) -> str:
    """Registra un modelo en el Azure ML Model Registry. Devuelve el asset ID."""
    if azure_ml_disabled():
        raise AzureMLClientError("El módulo de Azure ML está deshabilitado (AZURE_ML_ENABLED=false).")
    client = get_ml_client()
    _require_sdk()
    model = Model(
        path=path,
        name=name,
        version=version,
        description=description,
        tags=tags or {},
        type=AssetTypes.CUSTOM_MODEL,
    )
    try:
        registered = client.models.create_or_update(model)
    except AzureError as exc:
        raise AzureMLClientError("No se pudo registrar el modelo en Azure ML") from exc
    return f"{registered.name}:{registered.version}"
