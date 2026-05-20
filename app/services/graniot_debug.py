from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.config import settings

logger = logging.getLogger("graniot.debug")

_HEADER_SECRET_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "x-csrftoken",
    "csrf-token",
    "x-csrf-token",
}

_BODY_SECRET_MARKERS = (
    "authorization",
    "cookie",
    "csrf",
    "api_key",
    "apikey",
    "access_key",
    "password",
    "secret",
    "token",
)


def debug_enabled() -> bool:
    return bool(getattr(settings, "GRANIOT_DEBUG_LOGS_ENABLED", True))


def _max_chars() -> int:
    try:
        return int(getattr(settings, "GRANIOT_DEBUG_MAX_BODY_CHARS", 30000) or 30000)
    except Exception:
        return 30000


def get_log_file_path() -> Path:
    configured = getattr(settings, "GRANIOT_DEBUG_LOG_FILE", None)
    if configured:
        return Path(str(configured)).expanduser()

    storage_dir = getattr(settings, "DATARIS_COMPAT_STORAGE_DIR", None) or "app/storage"
    return Path(str(storage_dir)).expanduser() / "graniot_debug_logs.jsonl"


def clear_logs() -> None:
    path = get_log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def read_logs(limit: int = 100, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
    path = get_log_file_path()
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected: List[Dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            item = {"raw_line": line}
        if request_id and str(item.get("request_id")) != str(request_id):
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return list(reversed(selected))


def _truncate(value: str) -> str:
    max_chars = _max_chars()
    if max_chars <= 0:
        return value
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}...<truncated {len(value) - max_chars} chars>"


def _should_redact_key(key: Any, *, header: bool = False) -> bool:
    text = str(key or "").strip().lower()
    if header:
        return text in _HEADER_SECRET_KEYS
    return any(marker in text for marker in _BODY_SECRET_MARKERS)


def redact_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    safe: Dict[str, Any] = {}
    for key, value in headers.items():
        if _should_redact_key(key, header=True):
            safe[str(key)] = "<REDACTED>"
        else:
            safe[str(key)] = _safe_value(value)
    return safe


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(k): ("<REDACTED>" if _should_redact_key(k) else _safe_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, depth + 1) for v in value]
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if isinstance(value, str):
        return _truncate(value)
    return value


def safe_payload(value: Any) -> Any:
    return _safe_value(value)


def response_preview(content_type: str, content: bytes, text: Optional[str] = None) -> Any:
    content_type_l = (content_type or "").lower()
    if "image/" in content_type_l or "application/octet-stream" in content_type_l:
        return f"<binary {content_type or 'unknown'} {len(content)} bytes>"
    if text is None:
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            return f"<unreadable {len(content)} bytes>"
    text = _truncate(text)
    if "application/json" in content_type_l:
        try:
            return safe_payload(json.loads(text))
        except Exception:
            return text
    return text


def _important_logs_to_stdout_enabled() -> bool:
    return bool(getattr(settings, "GRANIOT_DEBUG_IMPORTANT_LOGS_TO_STDOUT", True))


def _is_important_event(enriched: Dict[str, Any]) -> bool:
    event_name = str(enriched.get("event") or "")
    if event_name in {
        "graniot.exception",
        "dataris.graniot.wms_proxy.failed",
        "dataris.graniot.recover_wms_data.failed_lookup",
        "dataris.graniot.store_recovered_wms_data.failed",
    }:
        return True
    if event_name == "graniot.response":
        try:
            return int(enriched.get("status_code") or 0) >= 400
        except Exception:
            return False
    return False


def log_event(event: Dict[str, Any]) -> None:
    enriched = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **safe_payload(event),
    }

    line = json.dumps(enriched, ensure_ascii=False, default=str)
    is_debug_enabled = debug_enabled()
    is_important = _is_important_event(enriched)

    # Keep critical Graniot/WMS failures visible in Cloud Run Logs even when
    # full file-based debug logging is disabled. This is what lets us see why
    # the browser only gets a generic 502.
    if is_important and _important_logs_to_stdout_enabled():
        logger.warning("GRANIOT_IMPORTANT %s", line)
    elif is_debug_enabled:
        # Write detailed payloads to file when enabled, but do not flood uvicorn
        # stdout at INFO level during map rendering.
        logger.debug("GRANIOT_DEBUG %s", line)

    if not is_debug_enabled:
        return

    if bool(getattr(settings, "GRANIOT_DEBUG_LOG_TO_FILE", True)):
        try:
            path = get_log_file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            logger.warning("Could not write Graniot debug log: %s", exc)
