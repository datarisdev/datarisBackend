"""Cliente sobre la API de gestión de Azure Container Apps Jobs.

Plano de control únicamente: este módulo arranca/consulta/cancela
ejecuciones ("executions") de un único Container App Job de CPU
(TRAINING_JOB_NAME, ver datarisInfra/ml_training_job.tf). El entrenamiento
pesado (PyTorch, Ultralytics) corre en la imagen Docker separada
(datarisBackend/ml-training/), nunca dentro de este proceso FastAPI.

Reemplaza al cliente original basado en el SDK de Azure ML (azure-ai-ml):
el Azure ML Compute Cluster GPU nunca llegó a funcionar en esta suscripción
(bloqueo real de cuota, ver datarisInfra/envs/prod.tfvars) y se decidió
migrar a un Container App Job de CPU sobre el mismo entorno de Container
Apps que ya usa el backend, en vez de esperar la aprobación de cuota de
Microsoft.

No hay SDK de gestión de Azure Container Apps como dependencia — se llama
directamente a la API REST de Azure Resource Manager (verificada
manualmente contra un job real con `az containerapp job start --debug`)
reusando la misma cadena de credenciales que app/utils/azure_blob.py
(Managed Identity en Azure, DefaultAzureCredential/Azure CLI en local).
Variables requeridas para funcionar contra un Job real:
TRAINING_JOB_SUBSCRIPTION_ID, TRAINING_JOB_RESOURCE_GROUP, TRAINING_JOB_NAME.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.utils.azure_blob import get_token_credential, ml_training_container_name, AzureBlobStorageError
from app.models.ml_training import TrainingJobStatus

logger = logging.getLogger(__name__)

_ARM_BASE_URL = "https://management.azure.com"
_ARM_API_VERSION = "2025-07-01"
_ARM_SCOPE = "https://management.azure.com/.default"


class TrainingJobClientError(RuntimeError):
    """Error sanitizado de la integración con Azure Container Apps Jobs."""


# Estados reales de una execution de Container Apps Job -> estado interno de
# Dataris. Cualquier valor no reconocido se trata como RUNNING (nunca se
# pierde un job "en curso"). "Processing" es el estado transitorio mientras
# Azure asigna una réplica (equivalente a "Provisioning" en Azure ML).
_STATUS_MAP: dict[str, TrainingJobStatus] = {
    "Running": TrainingJobStatus.RUNNING,
    "Processing": TrainingJobStatus.PROVISIONING_COMPUTE,
    "Succeeded": TrainingJobStatus.COMPLETED,
    "Failed": TrainingJobStatus.FAILED,
    "Stopped": TrainingJobStatus.CANCELLED,
}


def translate_azure_status(azure_status: str | None) -> TrainingJobStatus:
    if not azure_status:
        return TrainingJobStatus.QUEUED
    return _STATUS_MAP.get(azure_status, TrainingJobStatus.RUNNING)


@dataclass
class TrainingJobSpec:
    job_name: str
    command: list[str]
    args: list[str]
    environment_variables: dict[str, str] = field(default_factory=dict)


def azure_ml_disabled() -> bool:
    return (os.getenv("TRAINING_JOB_ENABLED") or "false").strip().lower() not in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise TrainingJobClientError(f"{name} no está configurada. El módulo de entrenamiento no está habilitado.")
    return value


def build_blob_io_env(*, inputs: dict[str, str], output_prefix: str) -> dict[str, str]:
    """Env vars que el wrapper de la imagen de entrenamiento
    (ml-training/entrypoint.py) usa para descargar sus inputs de Blob
    Storage antes de correr y subir su salida (incluido progress.json en
    vivo) después. A diferencia de Azure ML, Container Apps Jobs no monta
    carpetas de blob automáticamente — el wrapper hace ese trabajo a mano.

    `inputs` es nombre lógico -> prefijo de blob (ej. {"dataset": "..."}
    para entrenamiento, {"model": "...", "image": "..."} para inferencia);
    el wrapper descarga cada uno a /mnt/<nombre>/, igual que antes montaba
    Azure ML cada input en ${{inputs.<nombre>}}.

    No lanza si falta configuración de Storage (ej. TRAINING_JOB_ENABLED=false
    en un ambiente que tampoco configuró AZURE_STORAGE_CONTAINER_NAME): esa
    combinación ya la rechaza submit_command_job con un mensaje claro, y no
    tiene sentido que construir estas env vars informativas sea lo que rompa
    la ruta con una excepción distinta y menos clara."""
    try:
        container_name = ml_training_container_name()
    except AzureBlobStorageError:
        container_name = ""
    return {
        "BLOB_CONTAINER_NAME": container_name,
        "AZURE_STORAGE_ACCOUNT_URL": (os.getenv("AZURE_STORAGE_ACCOUNT_URL") or "").strip(),
        "AZURE_CLIENT_ID": (os.getenv("TRAINING_JOB_IDENTITY_CLIENT_ID") or "").strip(),
        "BLOB_INPUTS_JSON": json.dumps(inputs),
        "BLOB_OUTPUT_PREFIX": output_prefix,
    }


def default_docker_image() -> str:
    return (os.getenv("TRAINING_JOB_IMAGE") or "").strip()


def default_job_resource_name() -> str:
    """Nombre del Container App Job (mostrado en la UI como "compute" del
    job, ver TrainingJob.compute_target — se conserva ese nombre de columna
    aunque ya no exista el concepto de "compute target" de Azure ML)."""
    return (os.getenv("TRAINING_JOB_NAME") or "").strip()


def _job_resource_id() -> str:
    subscription_id = _required_env("TRAINING_JOB_SUBSCRIPTION_ID")
    resource_group = _required_env("TRAINING_JOB_RESOURCE_GROUP")
    job_name = _required_env("TRAINING_JOB_NAME")
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/jobs/{job_name}"
    )


def _auth_headers() -> dict[str, str]:
    try:
        credential = get_token_credential()
        token = credential.get_token(_ARM_SCOPE).token
    except AzureBlobStorageError as exc:
        raise TrainingJobClientError("No se pudo obtener credenciales de Azure") from exc
    return {"Authorization": f"Bearer {token}"}


def _request(method: str, url: str, *, json_body: dict[str, Any] | None = None) -> httpx.Response:
    try:
        response = httpx.request(method, url, headers=_auth_headers(), json=json_body, timeout=30.0)
    except httpx.HTTPError as exc:
        raise TrainingJobClientError("No se pudo comunicar con la API de Azure Container Apps") from exc
    if response.status_code >= 400:
        raise TrainingJobClientError(
            f"Azure Container Apps respondió {response.status_code} al {method} {url.split('?')[0]}: "
            f"{response.text[:500]}"
        )
    return response


def submit_command_job(spec: TrainingJobSpec) -> str:
    """Arranca una nueva execution del Container App Job de entrenamiento con
    el command/args/env de esta ejecución específica. Devuelve el nombre de
    la execution (== su ID para efectos de get_job/cancel_job).

    spec.command/spec.args son el comando "real" (ej. python train.py
    --dataset-path ...) armado por training_recipes.py — esta función lo
    envuelve con ml-training/entrypoint.py, que descarga los inputs de Blob
    Storage antes de correr y sube la salida después (ver build_blob_io_env).
    training_recipes.py no necesita saber que existe ese wrapper."""
    if azure_ml_disabled():
        raise TrainingJobClientError(
            "El módulo de entrenamiento está deshabilitado (TRAINING_JOB_ENABLED=false). "
            "Actívalo solo tras aprobar la infraestructura correspondiente."
        )

    url = f"{_ARM_BASE_URL}{_job_resource_id()}/start?api-version={_ARM_API_VERSION}"
    body = {
        "containers": [
            {
                "name": "ml-training",
                "command": ["python", "entrypoint.py"],
                "args": ["--", *spec.command, *spec.args],
                "env": [{"name": k, "value": v} for k, v in spec.environment_variables.items()],
            }
        ]
    }
    response = _request("POST", url, json_body=body)
    payload = response.json()
    execution_name = payload.get("name")
    if not execution_name:
        raise TrainingJobClientError(f"Azure no devolvió el nombre de la execution: {payload}")
    return execution_name


def get_job(azure_ml_job_name: str) -> dict[str, Any]:
    url = f"{_ARM_BASE_URL}{_job_resource_id()}/executions/{azure_ml_job_name}?api-version={_ARM_API_VERSION}"
    response = _request("GET", url)
    payload = response.json()
    properties = payload.get("properties") or {}
    status = properties.get("status")
    return {
        "name": payload.get("name"),
        "status": status,
        "internal_status": translate_azure_status(status),
        "studio_url": _portal_url(payload.get("id")),
        "tags": {},
    }


def cancel_job(azure_ml_job_name: str) -> None:
    url = f"{_ARM_BASE_URL}{_job_resource_id()}/stop/{azure_ml_job_name}?api-version={_ARM_API_VERSION}"
    try:
        _request("POST", url)
    except TrainingJobClientError as exc:
        # Una execution ya terminada (Succeeded/Failed) responde 409 al
        # intentar detenerla — no es un error real, el job ya no corre.
        if "409" in str(exc):
            return
        raise


def _portal_url(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    return f"https://portal.azure.com/#@/resource{resource_id}"
