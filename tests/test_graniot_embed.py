import asyncio
import base64
import json
import time

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


@pytest.fixture(autouse=True)
def _default_embed_settings(monkeypatch):
    """Deterministic defaults: on-demand minting OFF unless a test enables it."""
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_HOST", "embed.graniot.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", None)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PASSWORD", None)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_REFRESH_TOKEN", None)


def _fake_jwt(exp: int) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64({'token_type': 'access', 'exp': exp})}.sig"


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Minimal async-context httpx.AsyncClient stand-in that records POSTs."""

    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._recorder.append((url, json))
        return self._response


def _patch_httpx(monkeypatch, response):
    recorder = []
    monkeypatch.setattr(
        graniot.httpx, "AsyncClient", lambda **kwargs: _FakeAsyncClient(response, recorder)
    )
    return recorder


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


def test_embed_endpoint_prefers_live_url_over_configured(monkeypatch):
    """A fresh live token must win over a possibly-stale configured URL."""
    fake_client = _FakeGraniotClient(
        [{
            "account_email": "gmateo@ingeoproyectos.com",
            "embedded_url": "https://embed.graniot.com/?auth_id=fresh-token",
        }],
    )
    monkeypatch.setattr(graniot, "GraniotClient", lambda: fake_client)
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(
        graniot.settings,
        "GRANIOT_EMBED_URL",
        "https://embed.graniot.com/?auth_id=stale-configured-token",
    )
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"]["embedded_url"].endswith("auth_id=fresh-token")
    assert response.headers["cache-control"] == "no-store"
    assert fake_client.calls  # the live lookup was attempted


def test_embed_endpoint_falls_back_to_configured_url_when_live_fails(monkeypatch):
    """When Graniot is unreachable, serve the configured URL instead of erroring."""

    class _BoomClient:
        async def get(self, path, **kwargs):
            raise RuntimeError("graniot down")

    monkeypatch.setattr(graniot, "GraniotClient", _BoomClient)
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(
        graniot.settings,
        "GRANIOT_EMBED_URL",
        "https://embed.graniot.com/?auth_id=configured-token",
    )
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"] == {
        "account_email": "gmateo@ingeoproyectos.com",
        "embedded_url": "https://embed.graniot.com/?auth_id=configured-token",
    }
    assert response.headers["cache-control"] == "no-store"


def test_embed_endpoint_reraises_when_live_fails_and_no_fallback(monkeypatch):
    """Without a configured fallback, a live failure must surface as an error."""

    class _BoomClient:
        async def get(self, path, **kwargs):
            raise RuntimeError("graniot down")

    monkeypatch.setattr(graniot, "GraniotClient", _BoomClient)
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_URL", None)
    response = Response()

    with pytest.raises(HTTPException):
        asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))


def test_embed_endpoint_mints_fresh_token_with_credentials(monkeypatch):
    """With embed credentials, a fresh access token is minted (no /api/accounts/)."""
    fresh = _fake_jwt(int(time.time()) + 3600)
    recorder = _patch_httpx(monkeypatch, _FakeResponse(200, {"access": fresh, "refresh": "r"}))
    monkeypatch.setattr(
        graniot, "GraniotClient", lambda: pytest.fail("must not call /api/accounts/ when minting")
    )
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", "svc@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PASSWORD", "secret")
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"]["embedded_url"] == f"https://embed.graniot.com/?auth_id={fresh}"
    assert response.headers["cache-control"] == "no-store"
    # Logged into /api/token/ with username+password (credentials never leaked elsewhere).
    assert recorder == [("https://embed.graniot.com/api/token/", {"username": "svc@ingeoproyectos.com", "password": "secret"})]


def test_embed_endpoint_mints_with_refresh_token(monkeypatch):
    """With only a refresh token, mint via /api/token/refresh/."""
    fresh = _fake_jwt(int(time.time()) + 3600)
    recorder = _patch_httpx(monkeypatch, _FakeResponse(200, {"access": fresh}))
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_REFRESH_TOKEN", "refresh-abc")
    response = Response()

    result = asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))

    assert result["data"]["embedded_url"].endswith(f"auth_id={fresh}")
    assert recorder == [("https://embed.graniot.com/api/token/refresh/", {"refresh": "refresh-abc"})]


def test_embed_endpoint_mint_rejects_non_200(monkeypatch):
    """A failed mint surfaces as 502 (never a stale token) and hides the body."""
    _patch_httpx(monkeypatch, _FakeResponse(401, {"detail": "No active account found"}))
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_REFRESH_TOKEN", "bad-refresh")
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))
    assert exc_info.value.status_code == 502
    assert "bad-refresh" not in str(exc_info.value.detail)


def test_embed_endpoint_mint_rejects_expired_access(monkeypatch):
    """If the minted token is already expired, fail closed with 502."""
    expired = _fake_jwt(int(time.time()) - 60)
    _patch_httpx(monkeypatch, _FakeResponse(200, {"access": expired}))
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: {"id": "user-1"})
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", "svc")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PASSWORD", "secret")
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(graniot.get_embed_url(response=response, authorization="Bearer test"))
    assert exc_info.value.status_code == 502
