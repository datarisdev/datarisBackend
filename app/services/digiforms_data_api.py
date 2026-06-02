from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
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
    """
    Keep original leaf names while also exposing nested keys.

    DigiForms forms are dynamic and can contain nested groups. The parser used by
    Dataris reads aliases such as GeoLocalizacion, UBICACION and State. Keeping
    the leaf key makes the normalizer resilient when DigiForms wraps values in a
    group object.
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
            # Preserve detail groups in the raw payload. A scalar list is also
            # useful as a readable value for fields such as pest categories.
            flattened[path] = value
            flattened.setdefault(key_text, value)
        else:
            flattened[path] = value
            flattened.setdefault(key_text, value)
    return flattened


def _looks_like_submission(payload: Dict[str, Any]) -> bool:
    normalized = {_normalize(key) for key in payload}
    response_keys = {"responseid", "idrespuesta", "idrespuestaformulario", "submissionid"}
    coordinate_keys = {"geolocalizacion", "ubicacion", "location", "coordenadas", "latitud", "latitude"}
    return bool(normalized & response_keys) or bool(normalized & coordinate_keys)


def extract_submission_rows(payload: Any) -> List[Dict[str, Any]]:
    """Extract form submissions from the dynamic JSON returned by DigiForms."""
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

        # Wrapper objects commonly expose Data, Results or Records. Recursing
        # through every nested container also supports renamed wrappers.
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


@dataclass(frozen=True)
class DigiformsDataConfig:
    base_url: str
    client_id: str
    api_user: str
    api_password: str
    timeout_seconds: float


class DigiformsDataAPI:
    """
    Client for the official DigiForms REST/JSON data integration endpoints.

    Documented endpoints:
      GET /results/GetAll/{ClientId}/{FormId}/{LastResponseId}
      GET /images/{ClientId}/{FormId}/{LastResponseId}

    Basic authentication username is "{ClientId}/{AdministratorUser}".
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
            base_url=str(base_url if base_url is not None else settings.DIGIFORMS_DATA_BASE_URL or "").rstrip("/"),
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
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=True) as client:
            try:
                response = await client.get(url, auth=self._auth(), headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise DigiformsDataAPIError(f"No se pudo conectar con DigiForms Data API: {exc}") from exc
        if response.status_code >= 400:
            raise DigiformsDataAPIError(
                f"DigiForms Data API respondió con error HTTP {response.status_code}.",
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

    async def get_images_since(self, form_id: str, last_response_id: int | str) -> Any:
        client_id = quote(self.config.client_id, safe="")
        form_id_q = quote(str(form_id), safe="")
        response_id_q = quote(str(last_response_id), safe="")
        return await self._request_json(f"images/{client_id}/{form_id_q}/{response_id_q}")

    async def test_results_connection(self, form_id: str, last_response_id: int | str = 0) -> Dict[str, Any]:
        payload = await self.get_all_results_since(form_id, last_response_id)
        rows = extract_submission_rows(payload)
        return {"ok": True, "records_received": len(rows), "form_id": str(form_id)}
