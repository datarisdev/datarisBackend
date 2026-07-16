"""Low-level HTTP client for the EOSDA API Connect service.

Endpoints used (base ``https://api-connect.eos.com/api``):
- Search:      POST /lms/search/v2/{dataset}
- Statistics:  POST /gdw/api        (type=mt_stats, async)  +  GET /gdw/api/{task_id}
- Render tile: GET  /render/{view_id}/{band}/{z}/{x}/{y}

Authentication is sent both as the ``x-api-key`` header and the ``api_key``
query parameter, since the EOS documentation is inconsistent about which one a
given endpoint expects (render tiles use the query parameter).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

# EOS aplica un límite de ~10 solicitudes/minuto al endpoint de render. Una
# sola capa necesita varios tiles (a veces más de 10), así que un 429 aislado
# no debe hacer fallar el render completo: se reintenta con backoff.
RENDER_MAX_RETRIES_429 = 4
RENDER_RETRY_BASE_SECONDS = 5.0


class EOSNotConfigured(RuntimeError):
    """Raised when EOS_API_KEY is not configured in the backend."""


class EOSApiError(RuntimeError):
    """Raised when the EOS API returns an error response.

    ``status_code`` conserva el código HTTP de EOS cuando existe, para que las
    capas superiores puedan reaccionar distinto a un 429 (límite de peticiones,
    que no se resuelve reintentando otra escena) frente a otros errores.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    return bool((settings.EOS_API_KEY or "").strip())


def _api_key() -> str:
    key = (settings.EOS_API_KEY or "").strip()
    if not key:
        raise EOSNotConfigured("EOS_API_KEY no está configurada en el backend.")
    return key


def _base_url() -> str:
    return settings.EOS_BASE_URL.rstrip("/")


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


def _params() -> dict[str, str]:
    return {"api_key": _api_key()}


def _raise_for_status(resp: requests.Response, context: str) -> None:
    if resp.status_code >= 400:
        body = resp.text[:400] if resp.text else ""
        raise EOSApiError(f"EOS {context} falló ({resp.status_code}): {body}", status_code=resp.status_code)


def search_scenes(
    *,
    geometry: dict[str, Any],
    date_from: str,
    date_to: str,
    cloud_max: float,
    dataset: Optional[str] = None,
    limit: int = 100,
    shape_relation: str = "INTERSECTS",
) -> list[dict[str, Any]]:
    """Search available satellite scenes intersecting ``geometry``."""
    dataset = dataset or settings.EOS_DATASET
    url = f"{_base_url()}/lms/search/v2/{dataset}"
    body = {
        "fields": ["sceneID", "cloudCoverage", "date", "view_id", "tms", "dataCoveragePercentage"],
        "limit": int(limit),
        "page": 1,
        "search": {
            "date": {"from": date_from, "to": date_to},
            "cloudCoverage": {"from": 0, "to": float(cloud_max)},
            "shapeRelation": shape_relation,
            "shape": geometry,
        },
        "sort": {"date": "desc"},
    }
    resp = requests.post(
        url,
        json=body,
        headers=_headers(),
        params=_params(),
        timeout=settings.EOS_TIMEOUT_SECONDS,
    )
    _raise_for_status(resp, "search")
    data = resp.json() if resp.content else {}
    results = data.get("results")
    return results if isinstance(results, list) else []


def fetch_render_tile(view_id: str, band: str, z: int, x: int, y: int) -> Optional[bytes]:
    """Fetch a single 256x256 render tile. Returns None for empty/no-data tiles.

    Retries with backoff on HTTP 429 (rate limit), since EOS limits the render
    endpoint to ~10 req/min and a single map layer needs several tiles.
    """
    url = f"{_base_url()}/render/{view_id}/{band}/{z}/{x}/{y}"
    attempt = 0
    while True:
        resp = requests.get(url, params=_params(), timeout=settings.EOS_TIMEOUT_SECONDS)
        if resp.status_code == 429 and attempt < RENDER_MAX_RETRIES_429:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else RENDER_RETRY_BASE_SECONDS * (attempt + 1)
            except ValueError:
                delay = RENDER_RETRY_BASE_SECONDS * (attempt + 1)
            delay += random.uniform(0, 2.0)  # jitter: evita que varios tiles reintenten al mismo instante
            logger.info("EOS render: 429 en %s, reintentando en %.1fs (intento %s)", url, delay, attempt + 1)
            time.sleep(delay)
            attempt += 1
            continue
        break

    if resp.status_code == 404:
        return None
    _raise_for_status(resp, "render")
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "image" not in content_type or not resp.content:
        return None
    return resp.content


def create_stats_task(
    *,
    geometry: dict[str, Any],
    date_start: str,
    date_end: str,
    indices: list[str],
    dataset: Optional[str] = None,
    reference: str = "dataris",
) -> dict[str, Any]:
    """Create an asynchronous multi-temporal statistics (mt_stats) task."""
    url = f"{_base_url()}/gdw/api"
    body = {
        "type": "mt_stats",
        "params": {
            "bm_type": [str(i).upper() for i in indices[:3]],
            "date_start": date_start,
            "date_end": date_end,
            "geometry": geometry,
            "reference": reference,
            "sensors": [dataset or settings.EOS_DATASET],
        },
    }
    resp = requests.post(
        url,
        json=body,
        headers=_headers(),
        params=_params(),
        timeout=settings.EOS_TIMEOUT_SECONDS,
    )
    _raise_for_status(resp, "stats-create")
    return resp.json() if resp.content else {}


def get_stats_task(task_id: str) -> dict[str, Any]:
    """Poll the result of a previously created mt_stats task."""
    url = f"{_base_url()}/gdw/api/{task_id}"
    resp = requests.get(
        url,
        headers={"x-api-key": _api_key()},
        params=_params(),
        timeout=settings.EOS_TIMEOUT_SECONDS,
    )
    _raise_for_status(resp, "stats-poll")
    return resp.json() if resp.content else {}
