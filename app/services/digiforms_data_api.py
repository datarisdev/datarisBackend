from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote

import httpx

from app.core.config import settings


class DigiformsDataAPIError(RuntimeError):
    """Raised when the official DigiForms Data API cannot complete a request."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize(value: Any) -> str:
    replacements = str.maketrans("áéíóúüñ", "aeiouun")
    return "".join(ch for ch in _text(value).lower().translate(replacements) if ch.isalnum())


def _first_value(payload: Dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    by_normalized = {_normalize(key): value for key, value in payload.items()}
    for alias in aliases:
        key = _normalize(alias)
        if key in by_normalized and by_normalized[key] not in (None, ""):
            return by_normalized[key]
    return default


def _flatten_dict(payload: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten a dynamic DigiForms response without discarding original values.

    DigiForms formats are editable and every FormId can expose a different set of
    questions. Keeping both dotted paths and leaf names lets the SIG aliases work
    for known agricultural fields while preserving additional questions for
    auditing and future mappings.
    """
    flattened: Dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, dict):
            nested = _flatten_dict(value, path)
            for nested_key, nested_value in nested.items():
                flattened.setdefault(nested_key, nested_value)
                leaf = nested_key.rsplit(".", 1)[-1]
                flattened.setdefault(leaf, nested_value)
        elif isinstance(value, list):
            # Preserve groups/details exactly as returned. Detail groups can vary
            # by FormId and must not be flattened into lossy fixed columns.
            flattened[path] = value
            flattened.setdefault(key_text, value)
        else:
            flattened[path] = value
            flattened.setdefault(key_text, value)
    return flattened


def dynamic_fields_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the dynamic fields of one submission, excluding connector metadata."""
    return {
        str(key): value
        for key, value in payload.items()
        if not str(key).startswith("_")
    }


def discover_field_names(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """List fields discovered in a variable DigiForms response."""
    names: set[str] = set()
    for row in rows:
        for key in row:
            if not str(key).startswith("_"):
                names.add(str(key))
    return sorted(names, key=lambda value: value.lower())


def _looks_like_submission(payload: Dict[str, Any]) -> bool:
    normalized = {_normalize(key) for key in payload}
    response_keys = {"responseid", "idrespuesta", "idrespuestaformulario", "submissionid"}
    coordinate_keys = {"geolocalizacion", "ubicacion", "location", "coordenadas", "latitud", "latitude"}
    return bool(normalized & response_keys) or bool(normalized & coordinate_keys)


def extract_submission_rows(payload: Any) -> List[Dict[str, Any]]:
    """Extract submissions from dynamic DigiForms JSON wrappers such as Results."""
    rows: List[Dict[str, Any]] = []
    visited: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return

        flattened = _flatten_dict(value)
        if _looks_like_submission(flattened):
            flattened["_raw_api_payload"] = value
            rows.append(flattened)
            return

        # Results, Data, Records and detail-group wrappers are intentionally
        # handled generically because their names and structures can vary.
        for item in value.values():
            if isinstance(item, (dict, list)):
                walk(item)

    walk(payload)
    return rows


def extract_image_rows(payload: Any) -> List[Dict[str, Any]]:
    """Extract image descriptors and ImagePath links from DigiForms API JSON."""
    images: List[Dict[str, Any]] = []
    visited: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return

        flattened = _flatten_dict(value)
        image_path = _first_value(flattened, ["ImagePath", "Image Path", "Url", "URL", "Path", "Ruta"], "")
        if image_path:
            flattened["ImagePath"] = _text(image_path)
            flattened["_raw_api_payload"] = value
            images.append(flattened)
            return
        for item in value.values():
            if isinstance(item, (dict, list)):
                walk(item)

    walk(payload)
    return images


def response_id_from_payload(payload: Dict[str, Any]) -> str:
    return _text(
        _first_value(
            payload,
            ["ResponseId", "Response Id", "IdRespuesta", "Id Respuesta", "response_id", "submission_id", "id"],
            "",
        )
    )


def state_from_payload(payload: Dict[str, Any]) -> str:
    return _text(_first_value(payload, ["State", "EstadoAprobacion", "ApprovalState"], "0")) or "0"


def image_path_from_payload(payload: Dict[str, Any]) -> str:
    return _text(_first_value(payload, ["ImagePath", "Image Path", "Url", "URL", "Path", "Ruta"], ""))


def _canonical_base_url(value: str) -> str:
    """Normalize the provider URL to the capitalization validated by AgtechApps."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return raw
    return re.sub(r"/digiformsdata/api$", "/DigiformsData/api", raw, flags=re.IGNORECASE)


def _error_excerpt(value: str, limit: int = 700) -> str:
    compact = " ".join(str(value or "").split())
    return compact[:limit]


def _validate_iso_date(value: str, field_name: str) -> str:
    raw = _text(value)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise DigiformsDataAPIError(f"{field_name} debe usar formato AAAA-MM-DD.") from exc


@dataclass(frozen=True)
class DigiformsDataConfig:
    base_url: str
    client_id: str
    api_user: str
    api_password: str
    timeout_seconds: float


class DigiformsDataAPI:
    """Client for the official DigiForms REST/JSON integration.

    Validated provider routes:
      GET /results/GetAll/{ClientId}/{FormId}/{FechaInicio}/{FechaFinal}
      GET /results/GetAll/{ClientId}/{FormId}/{LastResponseId}
      GET /images/{ClientId}/{FormId}/{FechaInicio}/{FechaFinal}
      GET /images/{ClientId}/{FormId}/{LastResponseId}

    Basic Auth username is exactly "{ClientId}/{UserId}" without spaces.
    Each FormId is the internal id exposed by FFormEdit.aspx?FormId=..., not the
    short visible number in the portal grid.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        api_user: Optional[str] = None,
        api_password: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.config = DigiformsDataConfig(
            base_url=_canonical_base_url(str(base_url if base_url is not None else settings.DIGIFORMS_DATA_BASE_URL or "")),
            client_id=str(client_id if client_id is not None else settings.DIGIFORMS_CLIENT_ID or "").strip(),
            api_user=str(api_user if api_user is not None else settings.DIGIFORMS_DATA_API_USER or settings.DIGIFORMS_API_USER or "").strip(),
            api_password=str(api_password if api_password is not None else settings.DIGIFORMS_DATA_API_PASSWORD or settings.DIGIFORMS_API_PASSWORD or ""),
            timeout_seconds=float(timeout_seconds if timeout_seconds is not None else settings.DIGIFORMS_DATA_TIMEOUT_SECONDS or 45),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.config.base_url and self.config.client_id and self.config.api_user and self.config.api_password)

    def _auth(self) -> httpx.BasicAuth:
        if not self.is_configured:
            raise DigiformsDataAPIError(
                "DigiForms Data API no está configurada para esta empresa. Completa ClientId, usuario técnico y contraseña desde Extensiones → DigiForms."
            )
        return httpx.BasicAuth(f"{self.config.client_id}/{self.config.api_user}", self.config.api_password)

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    async def _request_json(self, path: str) -> Any:
        url = self._url(path)
        # Replicate the provider-tested curl behavior. Brotli is declared as a
        # backend dependency so httpx can transparently decode `br` responses.
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Dataris-DigiForms-Connector/1.1",
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=True, headers=headers) as client:
            try:
                response = await client.get(url, auth=self._auth())
            except httpx.HTTPError as exc:
                raise DigiformsDataAPIError(f"No se pudo conectar con DigiForms Data API: {exc}") from exc
        if response.status_code >= 400:
            excerpt = _error_excerpt(response.text)
            suffix = f" Respuesta del proveedor: {excerpt}" if excerpt else ""
            raise DigiformsDataAPIError(
                f"DigiForms Data API respondió con error HTTP {response.status_code}.{suffix}",
                status_code=response.status_code,
                response_text=response.text,
            )
        try:
            return response.json()
        except Exception as exc:
            raise DigiformsDataAPIError(
                "DigiForms Data API respondió contenido no JSON.",
                status_code=response.status_code,
                response_text=response.text,
            ) from exc

    async def get_all_results_since(self, form_id: str, last_response_id: int | str) -> Any:
        client_id = quote(self.config.client_id, safe="")
        form_id_q = quote(str(form_id), safe="")
        response_id_q = quote(str(last_response_id), safe="")
        return await self._request_json(f"results/GetAll/{client_id}/{form_id_q}/{response_id_q}")

    async def get_all_results_by_dates(self, form_id: str, start_date: str, end_date: str) -> Any:
        client_id = quote(self.config.client_id, safe="")
        form_id_q = quote(str(form_id), safe="")
        start_q = quote(_validate_iso_date(start_date, "Fecha inicial"), safe="")
        end_q = quote(_validate_iso_date(end_date, "Fecha final"), safe="")
        return await self._request_json(f"results/GetAll/{client_id}/{form_id_q}/{start_q}/{end_q}")

    async def get_images_since(self, form_id: str, last_response_id: int | str) -> Any:
        client_id = quote(self.config.client_id, safe="")
        form_id_q = quote(str(form_id), safe="")
        response_id_q = quote(str(last_response_id), safe="")
        return await self._request_json(f"images/{client_id}/{form_id_q}/{response_id_q}")

    async def get_images_by_dates(self, form_id: str, start_date: str, end_date: str) -> Any:
        client_id = quote(self.config.client_id, safe="")
        form_id_q = quote(str(form_id), safe="")
        start_q = quote(_validate_iso_date(start_date, "Fecha inicial"), safe="")
        end_q = quote(_validate_iso_date(end_date, "Fecha final"), safe="")
        return await self._request_json(f"images/{client_id}/{form_id_q}/{start_q}/{end_q}")

    async def test_results_connection(
        self,
        form_id: str,
        last_response_id: int | str = 0,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        numeric_cursor = int(last_response_id or 0)
        if numeric_cursor > 0:
            payload = await self.get_all_results_since(form_id, numeric_cursor)
            mode = "incremental_response_id"
        else:
            year = date.today().year
            initial_start = start_date or f"{year}-01-01"
            initial_end = end_date or f"{year}-12-31"
            payload = await self.get_all_results_by_dates(form_id, initial_start, initial_end)
            mode = "initial_date_range"
        rows = extract_submission_rows(payload)
        return {
            "ok": True,
            "records_received": len(rows),
            "form_id": str(form_id),
            "request_mode": mode,
            "discovered_fields": discover_field_names(rows),
        }
