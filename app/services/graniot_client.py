from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class GraniotNotConfigured(RuntimeError):
    pass


class GraniotAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class GraniotClient:
    """Small backend-only client for Graniot.

    Graniot's public OpenAPI file does not declare a formal security scheme, so
    the auth header is intentionally configurable through env vars:

    GRANIOT_API_KEY=...
    GRANIOT_AUTH_HEADER=X-API-Key | Api-Key | Authorization
    GRANIOT_AUTH_SCHEME=Bearer
    """

    def __init__(self) -> None:
        self.base_url = (settings.GRANIOT_BASE_URL or "https://app.graniot.com").rstrip("/")
        self.api_key = settings.GRANIOT_API_KEY
        self.auth_header = (settings.GRANIOT_AUTH_HEADER or "X-API-Key").strip()
        self.auth_scheme = (settings.GRANIOT_AUTH_SCHEME or "Bearer").strip()
        self.timeout = float(settings.GRANIOT_TIMEOUT_SECONDS or 60)
        self.client_id = settings.GRANIOT_CLIENT_ID

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        if not self.api_key:
            raise GraniotNotConfigured("GRANIOT_API_KEY no está configurada en el backend")

        headers = {"Accept": accept}
        if self.auth_header.lower() == "authorization":
            value = self.api_key
            if not value.lower().startswith(("bearer ", "token ", "apikey ", "api-key ")):
                value = f"{self.auth_scheme} {value}".strip()
            headers["Authorization"] = value
        else:
            headers[self.auth_header] = self.api_key
        return headers

    def _params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        if self.client_id and "client_id" not in clean:
            clean["client_id"] = self.client_id
        return clean

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        accept: str = "application/json",
    ) -> Any:
        if not path.startswith("/"):
            path = f"/{path}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method.upper(),
                f"{self.base_url}{path}",
                headers=self._headers(accept=accept),
                params=self._params(params),
                json=json_body,
                data=data,
            )

        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            message = payload.get("detail") if isinstance(payload, dict) else None
            message = message or payload.get("message") if isinstance(payload, dict) else message
            message = message or f"Graniot respondió HTTP {response.status_code}"
            raise GraniotAPIError(response.status_code, str(message), payload)

        if accept != "application/json":
            return response

        if not response.content:
            return None
        try:
            return response.json()
        except Exception:
            return response.text

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json_body: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("POST", path, params=params, json_body=json_body)

    async def patch(self, path: str, json_body: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("PATCH", path, params=params, json_body=json_body)

    async def binary_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return await self.request("GET", path, params=params, accept="*/*")

    def build_wms_url(self, *, access_key: str, layer: str, time: Optional[str] = None, width: int = 768, height: int = 768) -> str:
        params = {
            "access_key": access_key,
            "layers": layer,
            "response_format": "image/png",
            "width": width,
            "height": height,
        }
        if time:
            params["time"] = time
        return f"{self.base_url}/api/wms/?{urlencode(params)}"
