from __future__ import annotations

import asyncio
import base64

from app.services.azure_openai_client import AIResponse
from app.services.contextual_copilot import _prepare_context, process_contextual_copilot
from app.services.copilot_vision import sanitize_visual_evidence


def _jpeg_data_url(payload: bytes = b"small-jpeg") -> str:
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def test_visual_evidence_accepts_supported_data_url_and_rejects_remote_url():
    result = sanitize_visual_evidence(
        [
            {
                "kind": "satellite_raster",
                "label": " NDVI   lote norte ",
                "data_url": _jpeg_data_url(),
            },
            {
                "kind": "screen",
                "label": "URL externa",
                "data_url": "https://example.com/image.jpg",
            },
        ]
    )

    assert len(result) == 1
    assert result[0]["kind"] == "satellite_raster"
    assert result[0]["label"] == "NDVI lote norte"
    assert result[0]["bytes"] == len(b"small-jpeg")


def test_context_redacts_secrets_and_omits_raw_geometry():
    context = _prepare_context(
        {
            "context": {
                "path": "/satelite",
                "api_key": "should-never-leave",
                "parcel": {"geometry": {"type": "Polygon", "coordinates": [1, 2, 3]}},
            },
            "question": "Analiza el lote",
        }
    )

    assert context["api_key"] == "[REDACTADO]"
    assert context["parcel"]["geometry"] == "[GEOMETRIA_OMITIDA]"


def test_contextual_copilot_sends_multimodal_evidence_and_reports_azure(monkeypatch):
    captured = {}

    async def fake_create_response(body, **_kwargs):
        captured["body"] = body
        return AIResponse(
            payload={
                "status": "completed",
                "output_text": "Diagnóstico verificable",
                "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            },
            provider="azure_openai",
            model="dataris-agro-vision",
            request_id="azure-request-1",
        )

    monkeypatch.setattr("app.services.contextual_copilot.ai_provider_configured", lambda: True)
    monkeypatch.setattr("app.services.contextual_copilot.create_response", fake_create_response)

    result = asyncio.run(
        process_contextual_copilot(
            {
                "context": {
                    "path": "/satelite",
                    "section_label": "Satélite",
                    "visible_text": "NDVI promedio 0.62",
                },
                "visual_evidence": [
                    {
                        "kind": "satellite_raster",
                        "label": "NDVI del lote A",
                        "data_url": _jpeg_data_url(),
                    }
                ],
            }
        )
    )

    content = captured["body"]["input"][1]["content"]
    assert [part["type"] for part in content] == ["input_text", "input_text", "input_image"]
    assert content[-1]["detail"] in {"high", "original", "auto", "low"}
    assert result["source"] == "azure_openai"
    assert result["model"] == "dataris-agro-vision"
    assert result["context_stats"]["visualEvidence"] == 1
    assert result["usage"]["totalTokens"] == 150
    assert result["request_id"] == "azure-request-1"
