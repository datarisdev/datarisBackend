"""Cliente para importar datasets/versiones desde Roboflow.

La API key nunca se acepta desde el cliente (frontend) ni se guarda en
PostgreSQL: se lee exclusivamente de la variable de entorno
``ROBOFLOW_API_KEY``, que en Azure se inyecta desde Key Vault (secreto
``<prefix>-<env>-roboflow-api-key``, ver datarisInfra/ml_training_secrets.tf).

Si la variable no está configurada, el servicio falla con un error claro
en vez de silenciarse; no hay credenciales reales disponibles en este
entorno de desarrollo, así que este cliente se valida con tests que
mockean las respuestas HTTP (ver tests/ml_training/test_roboflow_service.py).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

ROBOFLOW_API_BASE = "https://api.roboflow.com"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_EXPORT_POLL_ATTEMPTS = 10
EXPORT_POLL_INTERVAL_SECONDS = 2
SUPPORTED_EXPORT_FORMATS = {"yolov8", "yolov5pytorch", "yolov11", "coco", "voc"}


class RoboflowServiceError(RuntimeError):
    """Error sanitizado de la integración con Roboflow (nunca expone la API key)."""


@dataclass
class RoboflowExport:
    download_url: str
    size_bytes: int | None
    export_format: str


def _get_api_key(explicit_key: str | None = None) -> str:
    api_key = (explicit_key or os.getenv("ROBOFLOW_API_KEY") or "").strip()
    if not api_key:
        raise RoboflowServiceError(
            "ROBOFLOW_API_KEY no está configurada. Cárgala en Azure Key Vault "
            "(secreto roboflow-api-key) o en el entorno local antes de importar "
            "datasets de Roboflow."
        )
    return api_key


class RoboflowService:
    def __init__(self, api_key: str | None = None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._api_key = _get_api_key(api_key)
        self._timeout = timeout_seconds

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=ROBOFLOW_API_BASE, timeout=self._timeout)

    def request_export(self, workspace: str, project: str, version: str, export_format: str) -> RoboflowExport:
        """Solicita (y espera, con reintentos controlados) la generación del export."""
        if export_format not in SUPPORTED_EXPORT_FORMATS:
            raise RoboflowServiceError(f"Formato de exportación no soportado: {export_format}")

        for workspace_part in (workspace, project, version):
            if not workspace_part or "/" in workspace_part:
                raise RoboflowServiceError("workspace, project y version deben ser identificadores simples")

        path = f"/{workspace}/{project}/{version}/{export_format}"
        with self._client() as client:
            for attempt in range(1, MAX_EXPORT_POLL_ATTEMPTS + 1):
                try:
                    response = client.get(path, params={"api_key": self._api_key})
                except httpx.HTTPError as exc:
                    raise RoboflowServiceError("No se pudo conectar con la API de Roboflow") from exc

                if response.status_code == 401:
                    raise RoboflowServiceError("Credenciales de Roboflow inválidas")
                if response.status_code == 404:
                    raise RoboflowServiceError("Workspace, proyecto o versión de Roboflow no encontrados")
                if response.status_code >= 400:
                    logger.warning("Roboflow export request failed: status=%s", response.status_code)
                    raise RoboflowServiceError(f"Roboflow devolvió un error ({response.status_code})")

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RoboflowServiceError("Respuesta inválida de Roboflow") from exc

                export = payload.get("export") or {}
                progress = export.get("progress", 1)
                link = export.get("link")

                if link and (progress is None or float(progress) >= 1):
                    return RoboflowExport(
                        download_url=link,
                        size_bytes=export.get("size"),
                        export_format=export_format,
                    )

                if attempt < MAX_EXPORT_POLL_ATTEMPTS:
                    time.sleep(EXPORT_POLL_INTERVAL_SECONDS)

        raise RoboflowServiceError(
            "Roboflow no terminó de generar el export tras varios intentos. Reintenta en unos minutos."
        )

    def stream_export(self, export: RoboflowExport, chunk_size: int = 1024 * 1024):
        """Generador de bytes para volcar el ZIP de Roboflow directo a Azure Blob."""
        try:
            with httpx.stream("GET", export.download_url, timeout=self._timeout) as response:
                if response.status_code >= 400:
                    raise RoboflowServiceError("No se pudo descargar el export de Roboflow")
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise RoboflowServiceError("Fallo de red descargando el export de Roboflow") from exc
