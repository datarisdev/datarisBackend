from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Dict, List


MAX_VISUAL_ITEMS = 2
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_LABEL_CHARS = 240
_DATA_URL_RE = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\r\n]+)$", re.IGNORECASE)


def sanitize_visual_evidence(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    for item in raw[:MAX_VISUAL_ITEMS]:
        if not isinstance(item, dict):
            continue
        data_url = str(item.get("data_url") or "").strip()
        match = _DATA_URL_RE.fullmatch(data_url)
        if not match:
            continue
        try:
            decoded_size = len(base64.b64decode(match.group(2), validate=True))
        except (binascii.Error, ValueError):
            continue
        if decoded_size <= 0 or decoded_size > MAX_IMAGE_BYTES:
            continue

        kind = str(item.get("kind") or "screen").strip().lower()
        if kind not in {"screen", "satellite_raster", "aerial_map", "chart", "map"}:
            kind = "screen"
        label = " ".join(str(item.get("label") or "Evidencia visual").split())[:MAX_LABEL_CHARS]
        result.append(
            {
                "kind": kind,
                "label": label,
                "data_url": data_url,
                "bytes": decoded_size,
            }
        )
    return result


def visual_content_parts(visuals: List[Dict[str, Any]], *, detail: str = "high") -> List[Dict[str, Any]]:
    safe_detail = detail if detail in {"low", "high", "original", "auto"} else "high"
    parts: List[Dict[str, Any]] = []
    for visual in visuals:
        parts.append(
            {
                "type": "input_text",
                "text": f"Evidencia visual ({visual['kind']}): {visual['label']}",
            }
        )
        parts.append(
            {
                "type": "input_image",
                "image_url": visual["data_url"],
                "detail": safe_detail,
            }
        )
    return parts
