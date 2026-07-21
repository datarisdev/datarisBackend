import asyncio

import pytest
from fastapi import HTTPException, Response

from app.api.routers import graniot


class _FakeGraniotClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.payload


def test_select_embed_account_returns_only_safe_embed_fields():
    result = graniot._select_embed_account(
        [
            {
                "id": "other",
                "account_email": "other@example.com",
                "embedded_url": "https://embed.graniot.com/?auth_id=other",
                "account_access": "must-not-leak",
            },
            {
                "id": "target",
                "account_email": "GMateo@IngeoProyectos.com",
                "embedded_url": "https://embed.graniot.com/?auth_id=current-token",
                "account_access": "must-not-leak",
            },
        ],
        "gmateo@ingeoproyectos.com",
    )

    assert result == {
        "account_email": "GMateo@IngeoProyectos.com",
        "embedded_url": "https://embed.graniot.com/?auth_id=current-token",
    }
    assert "account_access" not in result


@pytest.mark.parametrize(
    "embedded_url",
    [
        "http://embed.graniot.com/?auth_id=token",
        "https://evil.example/?auth_id=token",
        "https://embed.graniot.com/",
    ],
)
def test_select_embed_account_rejects_untrusted_urls(embedded_url):
    with pytest.raises(HTTPException) as exc_info:
        graniot._select_embed_account(
            [{"account_email": "gmateo@ingeoproyectos.com", "embedded_url": embedded_url}],
            "gmateo@ingeoproyectos.com",
        )

    assert exc_info.value.status_code == 502


def test_embed_endpoint_resolves_current_url_without_client_id(monkeypatch):
    fake_client = _FakeGraniotClient(
        [{
            "account_email": "gmateo@ingeoproyectos.com",
            "embedded_url": "https://embed.graniot.com/?auth_id=fresh-token",
            "account_access": "private",
        }],
    )
    monkeypatch.setattr(graniot, "GraniotClient", lambda: fake_client)
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_URL", None)
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"]["embedded_url"].endswith("auth_id=fresh-token")
    assert result["error"] is None
    assert response.headers["cache-control"] == "no-store"
    assert fake_client.calls == [(
        "/api/accounts/",
        {"include_client_id": False, "debug_context": {"operation": "resolve-embed-url"}},
    )]


def test_embed_endpoint_prefers_configured_url_without_calling_graniot(monkeypatch):
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(
        graniot.settings,
        "GRANIOT_EMBED_URL",
        "https://embed.graniot.com/?auth_id=configured-token",
    )
    monkeypatch.setattr(
        graniot,
        "GraniotClient",
        lambda: pytest.fail("GraniotClient should not be created when an embed URL is configured"),
    )
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"] == {
        "account_email": "gmateo@ingeoproyectos.com",
        "embedded_url": "https://embed.graniot.com/?auth_id=configured-token",
    }
    assert response.headers["cache-control"] == "no-store"
