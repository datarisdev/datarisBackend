"""Cliente PostHog para eventos de negocio del backend.

Deshabilitado por defecto (POSTHOG_ENABLED != "true"). Reglas duras:

- No captura requests, respuestas, payloads, healthchecks, logs ni excepciones completas.
- Solo emite los eventos de negocio explícitos en `_ALLOWED_EVENTS`, y solo con las
  propiedades permitidas por evento (whitelist + blocklist de substrings, igual que el
  sanitizer del frontend en src/lib/analytics/sanitizeAnalyticsPayload.ts).
- GeoIP deshabilitado (`disable_geoip=True`).
- Nunca lanza: un fallo de analytics no puede afectar una respuesta real del backend.

Ver docs/analytics/posthog.md (datarisInfra) para la política completa.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Whitelist por evento. Agregar un evento nuevo = agregar una entrada acá.
_ALLOWED_EVENTS: dict[str, frozenset[str]] = {
    "satellite analysis completed": frozenset(
        {"analysis_type", "index_type", "result", "duration_bucket", "area_bucket", "environment"}
    ),
    "satellite analysis failed": frozenset(
        {"analysis_type", "index_type", "error_category", "duration_bucket", "area_bucket", "environment"}
    ),
    "digiforms sync completed": frozenset({"result", "duration_bucket", "environment"}),
    "digiforms sync failed": frozenset({"error_category", "duration_bucket", "environment"}),
    # Preparados para cuando exista un punto de enganche seguro confirmado
    # (ver docs/analytics/posthog.md, sección "Eventos no implementados"):
    "report generated": frozenset({"module", "result", "duration_bucket", "environment"}),
    "export completed": frozenset({"export_format", "module", "result", "duration_bucket", "environment"}),
    "aerial analysis completed": frozenset(
        {"aircraft_type", "analysis_type", "result", "duration_bucket", "area_bucket", "environment"}
    ),
    "aerial analysis failed": frozenset(
        {"aircraft_type", "analysis_type", "error_category", "duration_bucket", "area_bucket", "environment"}
    ),
}

# Defensa en profundidad: aunque una clave esté en una whitelist por error futuro,
# si su nombre contiene alguno de estos fragmentos se descarta igual.
_FORBIDDEN_KEY_FRAGMENTS = (
    "email",
    "name",
    "phone",
    "password",
    "token",
    "jwt",
    "authorization",
    "cookie",
    "session",
    "company",
    "field",
    "parcel",
    "farm",
    "geometry",
    "geojson",
    "polygon",
    "coordinate",
    "latitude",
    "longitude",
    "lat",
    "lng",
    "image",
    "file",
    "document",
    "report_url",
    "request",
    "response",
    "body",
    "payload",
    "sql",
    "stack",
    "error_message",
    "url",
    "query",
    "hash",
    "search",
    "path",
)

_MAX_STRING_LENGTH = 200

_client: Optional[Any] = None
_setup_attempted = False


def _is_enabled() -> bool:
    return os.getenv("POSTHOG_ENABLED", "false").strip().lower() == "true"


def _current_environment() -> str:
    # DATARIS_COMPAT_STATE_KEY ya lo inyecta Terraform con el nombre del ambiente
    # ("dev"/"prod"/"staging"); se reutiliza en vez de agregar una variable nueva.
    return os.getenv("DATARIS_COMPAT_STATE_KEY", "unknown")


def _get_client() -> Optional[Any]:
    global _client, _setup_attempted
    if _setup_attempted:
        return _client
    _setup_attempted = True

    if not _is_enabled():
        return None

    project_token = os.getenv("POSTHOG_PROJECT_TOKEN", "").strip()
    host = os.getenv("POSTHOG_HOST", "").strip()
    if not project_token or not host:
        logger.warning(
            "POSTHOG_ENABLED=true pero falta POSTHOG_PROJECT_TOKEN o POSTHOG_HOST; "
            "analytics del backend queda deshabilitado en este runtime."
        )
        return None

    try:
        from posthog import Posthog

        _client = Posthog(project_token, host=host, disable_geoip=True)
    except Exception:
        logger.warning("No se pudo inicializar el cliente de PostHog del backend.", exc_info=True)
        _client = None

    return _client


def _is_safe_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return 0 < len(value) <= _MAX_STRING_LENGTH
    return False


def _sanitize(event: str, properties: Optional[dict[str, Any]]) -> dict[str, Any]:
    allowed = _ALLOWED_EVENTS.get(event, frozenset())
    safe: dict[str, Any] = {}
    if not properties:
        return safe
    for key, value in properties.items():
        if key not in allowed:
            continue
        lower_key = key.lower()
        if any(fragment in lower_key for fragment in _FORBIDDEN_KEY_FRAGMENTS):
            continue
        if not _is_safe_value(value):
            continue
        safe[key] = value
    return safe


def track_event(event: str, *, distinct_id: Optional[str] = None, properties: Optional[dict[str, Any]] = None) -> None:
    """Registra un evento de negocio confirmado.

    No-op si PostHog está deshabilitado, si el evento no está en la whitelist, o si
    el cliente no pudo inicializarse. Nunca lanza.
    """
    if event not in _ALLOWED_EVENTS:
        return

    client = _get_client()
    if client is None:
        return

    try:
        safe_properties = _sanitize(event, properties)
        safe_properties["environment"] = _current_environment()
        client.capture(
            event=event,
            distinct_id=distinct_id or "backend-system",
            properties=safe_properties,
            disable_geoip=True,
        )
    except Exception:
        logger.warning("No se pudo registrar el evento de analytics: %s", event, exc_info=True)


def shutdown() -> None:
    """Flush y cierre limpio. Se llama desde el shutdown de FastAPI (app/main.py)."""
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception:
        logger.warning("No se pudo cerrar limpiamente el cliente de PostHog.", exc_info=True)
