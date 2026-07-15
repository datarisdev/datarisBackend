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
from typing import Any, Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


class EOSNotConfigured(RuntimeError):
    """Raised when EOS_API_KEY is not configured in the backend."""


class EOSApiError(RuntimeError):
    """Raised when the EOS API returns an error response."""


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
        raise EOSApiError(f"EOS {context} falló ({resp.status_code}): {body}")


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
    """Fetch a single 256x256 render tile. Returns None for empty/no-data tiles."""
    url = f"{_base_url()}/render/{view_id}/{band}/{z}/{x}/{y}"
    resp = requests.get(url, params=_params(), timeout=settings.EOS_TIMEOUT_SECONDS)
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
