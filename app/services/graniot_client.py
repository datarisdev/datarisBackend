from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
import time
import uuid

import httpx

from app.core.config import settings
from app.services.graniot_debug import log_event, redact_headers, response_preview, safe_payload


class GraniotNotConfigured(RuntimeError):
    pass


class GraniotAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class GraniotClient:
    """Backend-only client for Graniot.

    The OpenAPI file supplied by Graniot does not declare a global security
    scheme. In practice, deployments may accept different API key formats.
    This client first tries the configured format and, only for auth failures,
    retries common Graniot/API-key formats automatically.

    Keep GRANIOT_API_KEY only in backend env vars. Never expose it with VITE_*.

    Acting on behalf of one Graniot account
    --------------------------------------
    Farm/parcel endpoints are documented as "Provides get_effective_user() for
    views where a privileged user can act on behalf of another user via
    `client_id` parameter". Dataris uses that so a lot uploaded by a Dataris
    user is created inside *their* Graniot account (the same account whose
    portal is embedded in the Satélite module), not in the API key owner's
    account. Two ways, both supported here:

    - ``access_token``: the account's own token (``account_access`` from
      ``/api/accounts/``). Requests are sent as that user. When this is set the
      API-key fallbacks are disabled on purpose: silently falling back to the
      partner key would create the parcel in the wrong account.
    - ``client_id``: sent as a query parameter with the privileged API key.
    """

    AUTH_FAILURE_CODES = {401, 403}

    def __init__(self, *, access_token: Optional[str] = None, client_id: Optional[str] = None) -> None:
        self.base_url = (settings.GRANIOT_BASE_URL or "https://app.graniot.com").rstrip("/")
        self.api_key = settings.GRANIOT_API_KEY
        # Graniot accepts API keys through X-API-Key. Keep env overrides for
        # private deployments, but make the confirmed production format the default.
        self.auth_header = (settings.GRANIOT_AUTH_HEADER or "X-API-Key").strip()
        self.auth_scheme = (settings.GRANIOT_AUTH_SCHEME or "").strip()
        self.timeout = float(settings.GRANIOT_TIMEOUT_SECONDS or 60)
        self.access_token = str(access_token or "").strip() or None
        self.client_id = str(client_id).strip() if client_id not in (None, "") else settings.GRANIOT_CLIENT_ID

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key or self.access_token)

    @property
    def acts_on_behalf(self) -> bool:
        """True when requests are scoped to one specific Graniot account."""
        return bool(self.access_token or self.client_id)

    def _auth_header_value(self, scheme: str) -> str:
        raw = (self.api_key or "").strip()
        if not raw:
            raise GraniotNotConfigured("GRANIOT_API_KEY no está configurada en el backend")
        lowered = raw.lower()
        if lowered.startswith(("bearer ", "token ", "api-key ", "apikey ")):
            return raw
        return f"{scheme} {raw}".strip()

    def _headers_for(self, header_name: str, scheme: str, accept: str) -> Dict[str, str]:
        if self.access_token:
            return {"Accept": accept, "Authorization": f"Bearer {self.access_token}"}
        if not self.api_key:
            raise GraniotNotConfigured("GRANIOT_API_KEY no está configurada en el backend")

        headers = {"Accept": accept}
        normalized = header_name.lower()
        if normalized == "authorization":
            headers["Authorization"] = self._auth_header_value(scheme)
        else:
            headers[header_name] = self.api_key
        return headers

    def _auth_candidates(self, accept: str) -> List[Dict[str, str]]:
        configured = self._headers_for(self.auth_header, self.auth_scheme, accept)
        if self.access_token:
            # Impersonated requests must never fall back to the partner API key:
            # that would write into the wrong Graniot account without failing.
            return [configured]
        candidates = [configured]

        common_pairs: Iterable[Tuple[str, str]] = (
            ("X-API-Key", ""),
            ("Authorization", "Bearer"),
            ("Authorization", "Token"),
            ("Authorization", "Api-Key"),
            ("Api-Key", ""),
            ("api-key", ""),
        )
        seen = {tuple(sorted(configured.items()))}
        for header_name, scheme in common_pairs:
            try:
                headers = self._headers_for(header_name, scheme or self.auth_scheme, accept)
            except GraniotNotConfigured:
                raise
            key = tuple(sorted(headers.items()))
            if key not in seen:
                candidates.append(headers)
                seen.add(key)
        return candidates

    def _params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        # With an account token the request already *is* that user, so client_id
        # is unnecessary (and Graniot rejects it for non-privileged tokens).
        if self.client_id and not self.access_token and "client_id" not in clean:
            clean["client_id"] = self.client_id
        return clean

    @staticmethod
    def _payload_from_response(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text

    @staticmethod
    def _message_from_payload(payload: Any, status_code: int) -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("detail") or detail
            message = detail or payload.get("message") or payload.get("error")
            if message:
                return str(message)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()[:500]
        return f"Graniot respondió HTTP {status_code}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        accept: str = "application/json",
        use_auth: bool = True,
        debug_context: Optional[Dict[str, Any]] = None,
        include_client_id: bool = True,
    ) -> Any:
        is_absolute_url = path.startswith(("http://", "https://"))
        if not is_absolute_url and not path.startswith("/"):
            path = f"/{path}"

        # /api/wms/ is a custom image endpoint documented by Graniot with a
        # very small query surface. Do not append optional Dataris client_id
        # there because some Graniot WMS handlers return 500/502 when they
        # receive unknown query parameters.
        request_params = self._params(params) if include_client_id else {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = path if is_absolute_url else f"{self.base_url}{path}"
        method_name = method.upper()

        header_candidates = self._auth_candidates(accept) if use_auth else [{"Accept": accept}]
        last_error: Optional[GraniotAPIError] = None

        request_id = uuid.uuid4().hex[:12]
        base_debug = {
            "request_id": request_id,
            "context": debug_context or {},
            "method": method_name,
            "path": path,
            "url": url,
            "params": request_params,
            "accept": accept,
            "use_auth": use_auth,
            "auth_candidates": len(header_candidates),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for index, headers in enumerate(header_candidates):
                attempt_no = index + 1
                started = time.perf_counter()
                log_event({
                    "event": "graniot.request",
                    **base_debug,
                    "attempt_no": attempt_no,
                    "headers": redact_headers(headers),
                    "json_body": safe_payload(json_body),
                    "form_data": safe_payload(data),
                })
                try:
                    response = await client.request(
                        method_name,
                        url,
                        headers=headers,
                        params=request_params,
                        json=json_body,
                        data=data,
                    )
                except Exception as exc:
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                    log_event({
                        "event": "graniot.exception",
                        **base_debug,
                        "attempt_no": attempt_no,
                        "elapsed_ms": elapsed_ms,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    })
                    raise

                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                content_type = response.headers.get("content-type", "")
                preview = response_preview(content_type, response.content, None)
                log_event({
                    "event": "graniot.response",
                    **base_debug,
                    "attempt_no": attempt_no,
                    "elapsed_ms": elapsed_ms,
                    "status_code": response.status_code,
                    "reason_phrase": response.reason_phrase,
                    "response_headers": redact_headers(dict(response.headers)),
                    "content_type": content_type,
                    "content_length": len(response.content or b""),
                    "response_preview": preview,
                })

                if response.status_code < 400:
                    if accept != "application/json":
                        return response
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except Exception:
                        return response.text

                payload = self._payload_from_response(response)
                message = self._message_from_payload(payload, response.status_code)
                last_error = GraniotAPIError(response.status_code, message, payload)

                # Try the next auth format only on authentication failures.
                if not use_auth or response.status_code not in self.AUTH_FAILURE_CODES or index == len(header_candidates) - 1:
                    raise last_error

        if last_error:
            raise last_error
        raise GraniotAPIError(500, "No se pudo contactar Graniot")

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None, *, use_auth: bool = True, debug_context: Optional[Dict[str, Any]] = None, include_client_id: bool = True) -> Any:
        return await self.request("GET", path, params=params, use_auth=use_auth, debug_context=debug_context, include_client_id=include_client_id)

    async def post(self, path: str, json_body: Any = None, params: Optional[Dict[str, Any]] = None, debug_context: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("POST", path, params=params, json_body=json_body, debug_context=debug_context)

    async def post_form(self, path: str, data: Any = None, params: Optional[Dict[str, Any]] = None, debug_context: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("POST", path, params=params, data=data, debug_context=debug_context)

    async def patch(self, path: str, json_body: Any = None, params: Optional[Dict[str, Any]] = None, debug_context: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("PATCH", path, params=params, json_body=json_body, debug_context=debug_context)

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None, debug_context: Optional[Dict[str, Any]] = None) -> Any:
        return await self.request("DELETE", path, params=params, debug_context=debug_context)

    async def binary_get(self, path: str, params: Optional[Dict[str, Any]] = None, *, use_auth: bool = True, debug_context: Optional[Dict[str, Any]] = None, include_client_id: bool = True) -> httpx.Response:
        return await self.request("GET", path, params=params, accept="*/*", use_auth=use_auth, debug_context=debug_context, include_client_id=include_client_id)

    def build_wms_url(
        self,
        *,
        access_key: str,
        layer: str,
        time: Optional[str] = None,
        width: int = 768,
        height: int = 768,
    ) -> str:
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
