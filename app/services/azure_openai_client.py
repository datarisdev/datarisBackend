from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping

import httpx
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from app.core.config import settings


_credential: DefaultAzureCredential | ManagedIdentityCredential | None = None
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIResponse:
    payload: Dict[str, Any]
    provider: str
    model: str
    request_id: str | None


def _setting(name: str, default: str = "") -> str:
    value = getattr(settings, name, None) or os.getenv(name) or default
    return str(value).strip()


def azure_openai_configured() -> bool:
    return bool(_setting("AZURE_OPENAI_ENDPOINT") and _setting("AZURE_OPENAI_DEPLOYMENT"))


def ai_provider_configured() -> bool:
    return azure_openai_configured() or bool(
        _setting("OPENAI_API_KEY")
        or _setting("OPENAI_API_TOKEN")
        or _setting("CHATGPT_API_KEY")
    )


def configured_model(fallback: str, azure_deployment: str | None = None) -> str:
    if azure_openai_configured():
        return str(azure_deployment or _setting("AZURE_OPENAI_DEPLOYMENT")).strip()
    return fallback


def configured_provider() -> str:
    return "azure_openai" if azure_openai_configured() else "openai"


def _responses_url() -> str:
    endpoint = _setting("AZURE_OPENAI_ENDPOINT").rstrip("/")
    if endpoint:
        if endpoint.endswith("/openai/v1"):
            return f"{endpoint}/responses"
        return f"{endpoint}/openai/v1/responses"
    return "https://api.openai.com/v1/responses"


def _get_credential() -> DefaultAzureCredential | ManagedIdentityCredential:
    global _credential
    if _credential is not None:
        return _credential

    client_id = _setting("AZURE_CLIENT_ID")
    if os.getenv("CONTAINER_APP_NAME") or os.getenv("CONTAINER_APP_ENV_DNS_SUFFIX"):
        _credential = ManagedIdentityCredential(client_id=client_id or None)
    else:
        _credential = DefaultAzureCredential(
            managed_identity_client_id=client_id or None,
            exclude_interactive_browser_credential=True,
        )
    return _credential


async def _headers() -> Dict[str, str]:
    azure_key = _setting("AZURE_OPENAI_API_KEY")
    if azure_openai_configured():
        if azure_key:
            return {"api-key": azure_key, "Content-Type": "application/json"}

        scope = _setting("AZURE_OPENAI_TOKEN_SCOPE", "https://ai.azure.com/.default")
        token = await asyncio.to_thread(_get_credential().get_token, scope)
        return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}

    public_key = (
        _setting("OPENAI_API_KEY")
        or _setting("OPENAI_API_TOKEN")
        or _setting("CHATGPT_API_KEY")
    )
    if not public_key:
        raise RuntimeError("Azure OpenAI no está configurado.")
    return {"Authorization": f"Bearer {public_key}", "Content-Type": "application/json"}


def _safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or error.get("type") or "").strip()
            if message:
                return f"{code}: {message}"[:700] if code else message[:700]
    except Exception:
        pass
    return response.text[:700]


async def create_response(
    body: Mapping[str, Any],
    *,
    fallback_model: str,
    azure_deployment: str | None = None,
    timeout_seconds: float,
    max_attempts: int = 3,
) -> AIResponse:
    """Call Azure OpenAI Responses API, with a public OpenAI dev fallback.

    Production uses Azure Managed Identity when ``AZURE_OPENAI_ENDPOINT`` and
    ``AZURE_OPENAI_DEPLOYMENT`` are configured. No credential is sent by the
    browser and responses are never stored by the provider.
    """

    model = configured_model(fallback_model, azure_deployment)
    request_body = dict(body)
    request_body["model"] = model
    request_body["store"] = False

    headers = await _headers()
    retry_statuses = {408, 409, 429, 500, 502, 503, 504}
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                response = await client.post(_responses_url(), headers=headers, json=request_body)
                if response.status_code < 400:
                    payload = response.json()
                    usage = payload.get("usage") if isinstance(payload, dict) else None
                    logger.info(
                        "Copilot AI request completed provider=%s model=%s request_id=%s input_tokens=%s output_tokens=%s",
                        configured_provider(),
                        model,
                        response.headers.get("apim-request-id") or response.headers.get("x-request-id"),
                        usage.get("input_tokens") if isinstance(usage, dict) else None,
                        usage.get("output_tokens") if isinstance(usage, dict) else None,
                    )
                    return AIResponse(
                        payload=payload,
                        provider=configured_provider(),
                        model=model,
                        request_id=response.headers.get("apim-request-id") or response.headers.get("x-request-id"),
                    )

                message = _safe_error(response)
                error = RuntimeError(f"El proveedor de IA respondió {response.status_code}: {message}")
                if response.status_code not in retry_statuses or attempt >= max_attempts:
                    raise error
                last_error = error
                logger.warning(
                    "Copilot AI transient response provider=%s model=%s status=%s attempt=%s",
                    configured_provider(), model, response.status_code, attempt,
                )
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(4.0, max(0.25, float(retry_after))) if retry_after else min(4.0, 0.5 * (2 ** (attempt - 1)))
                except ValueError:
                    delay = min(4.0, 0.5 * (2 ** (attempt - 1)))
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning(
                    "Copilot AI transport failure provider=%s model=%s attempt=%s error_type=%s",
                    configured_provider(), model, attempt, type(exc).__name__,
                )
                if attempt >= max_attempts:
                    raise RuntimeError(f"No fue posible conectar con el proveedor de IA: {exc}") from exc
                await asyncio.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))

    raise RuntimeError(f"No fue posible completar la consulta de IA: {last_error}")


def response_usage(payload: Mapping[str, Any]) -> Dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    if not isinstance(usage, Mapping):
        return {}

    result: Dict[str, int] = {}
    for source, target in (
        ("input_tokens", "inputTokens"),
        ("output_tokens", "outputTokens"),
        ("total_tokens", "totalTokens"),
    ):
        try:
            result[target] = int(usage.get(source) or 0)
        except (TypeError, ValueError):
            continue
    return result
