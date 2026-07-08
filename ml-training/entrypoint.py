#!/usr/bin/env python
"""Wrapper de E/S contra Blob Storage para train.py/predict.py.

Azure ML montaba automáticamente cada input como una carpeta local
(${{inputs.<nombre>}}) y drenaba la salida en vivo (mode="rw_mount"). Azure
Container Apps Jobs no tiene ese mecanismo: este wrapper hace ese trabajo a
mano, sin tocar train.py/predict.py, que siguen recibiendo únicamente rutas
locales normales.

Uso (lo arma training_job_client.submit_command_job, nunca a mano):
    python entrypoint.py -- python train.py --dataset-path /mnt/dataset ... --output-path /mnt/output

Variables de entorno requeridas:
    BLOB_CONTAINER_NAME   Container de Blob Storage con los datos.
    AZURE_STORAGE_ACCOUNT_URL   URL del Storage Account (https://<cuenta>.blob.core.windows.net).
    AZURE_CLIENT_ID       Client ID de la Managed Identity del Container App Job.
    BLOB_INPUTS_JSON      JSON: {"<nombre>": "<prefijo de blob>"} — cada uno se
                          descarga a /mnt/<nombre>/ antes de correr el comando real.
    BLOB_OUTPUT_PREFIX    Prefijo de blob donde se sube /mnt/output/ al terminar
                          (éxito o error) — incluye progress.json en vivo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

PROGRESS_UPLOAD_INTERVAL_SECONDS = 10
MOUNT_ROOT = Path("/mnt")
OUTPUT_DIR = MOUNT_ROOT / "output"


def _get_credential():
    """Misma lógica que app/utils/azure_blob.py::get_token_credential(), pero
    autocontenida: esta imagen no tiene el paquete del backend instalado."""
    client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
    if os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"):
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    kwargs: dict[str, object] = {"exclude_interactive_browser_credential": True}
    if client_id:
        kwargs["managed_identity_client_id"] = client_id
    return DefaultAzureCredential(**kwargs)


def _container_client():
    account_url = os.environ["AZURE_STORAGE_ACCOUNT_URL"]
    container_name = os.environ["BLOB_CONTAINER_NAME"]
    service_client = BlobServiceClient(account_url=account_url, credential=_get_credential())
    return service_client.get_container_client(container_name)


def download_prefix(container_client, blob_prefix: str, local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    found = False
    for blob in container_client.list_blobs(name_starts_with=blob_prefix):
        rel = blob.name[len(blob_prefix):].lstrip("/")
        if not rel:
            continue
        found = True
        dest = local_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as handle:
            container_client.download_blob(blob.name).readinto(handle)
    if not found:
        raise FileNotFoundError(f"No se encontró ningún archivo bajo el prefijo de blob '{blob_prefix}'")


def upload_dir(container_client, local_dir: Path, blob_prefix: str) -> None:
    if not local_dir.exists():
        return
    prefix = blob_prefix.rstrip("/")
    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        with open(path, "rb") as handle:
            container_client.upload_blob(name=f"{prefix}/{rel}", data=handle, overwrite=True)


def _upload_progress_once(container_client, blob_prefix: str) -> float | None:
    progress_path = OUTPUT_DIR / "progress.json"
    if not progress_path.exists():
        return None
    mtime = progress_path.stat().st_mtime
    with open(progress_path, "rb") as handle:
        container_client.upload_blob(name=f"{blob_prefix.rstrip('/')}/progress.json", data=handle, overwrite=True)
    return mtime


def _progress_uploader(container_client, blob_prefix: str, stop_event: threading.Event) -> None:
    """Sube progress.json cada vez que cambia, mientras train.py/predict.py
    corren — best-effort: nunca debe tumbar el job real por un fallo de red
    transitorio al subir el progreso."""
    last_mtime: float | None = None
    while not stop_event.wait(PROGRESS_UPLOAD_INTERVAL_SECONDS):
        try:
            mtime = _upload_progress_once(container_client, blob_prefix)
            if mtime is not None:
                last_mtime = mtime
        except Exception as exc:  # noqa: BLE001 - best-effort, se ignora a propósito
            print(f"[entrypoint] no se pudo subir progress.json: {exc}", flush=True)


def main() -> int:
    argv = sys.argv[1:]
    if "--" not in argv:
        print("[entrypoint] uso: entrypoint.py -- <comando real>", file=sys.stderr)
        return 2
    real_command = argv[argv.index("--") + 1:]

    output_prefix = os.environ["BLOB_OUTPUT_PREFIX"]
    inputs = json.loads(os.environ.get("BLOB_INPUTS_JSON") or "{}")
    container_client = _container_client()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        for name, blob_prefix in inputs.items():
            if not blob_prefix:
                continue
            print(f"[entrypoint] descargando input '{name}' desde '{blob_prefix}' a /mnt/{name}", flush=True)
            download_prefix(container_client, blob_prefix, MOUNT_ROOT / name)
    except Exception as exc:
        # Mismo formato de error.json que train.py/predict.py escriben en sus
        # propios except — refresh_job_status ya sabe leer este archivo para
        # dar un mensaje real en vez de uno genérico (ver service.py).
        (OUTPUT_DIR / "error.json").write_text(
            json.dumps({"error": str(exc), "traceback": traceback.format_exc()}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        upload_dir(container_client, OUTPUT_DIR, output_prefix)
        print(f"[entrypoint] fallo al descargar inputs: {exc}", file=sys.stderr, flush=True)
        return 1

    stop_event = threading.Event()
    uploader = threading.Thread(
        target=_progress_uploader, args=(container_client, output_prefix, stop_event), daemon=True
    )
    uploader.start()

    print(f"[entrypoint] ejecutando: {real_command}", flush=True)
    result = subprocess.run(real_command)

    stop_event.set()
    uploader.join(timeout=PROGRESS_UPLOAD_INTERVAL_SECONDS)
    # Última subida de progress.json por si cambió entre el último tick del
    # hilo y el fin real del proceso.
    try:
        _upload_progress_once(container_client, output_prefix)
    except Exception:
        pass

    print(f"[entrypoint] subiendo salida de {OUTPUT_DIR} a '{output_prefix}'", flush=True)
    upload_dir(container_client, OUTPUT_DIR, output_prefix)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
