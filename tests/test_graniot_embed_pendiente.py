"""Quién puede ver el portal compartido de Graniot y quién ve «por configurar».

El portal de la cuenta de servicio enseña las fincas y los lotes de OTRA cuenta.
Servírselo a un cliente nuevo, cuyo portal todavía no está enlazado, le hacía
creer que su monitoreo satelital ya estaba configurado y que esos lotes ajenos
eran los suyos. Desde ahora ese usuario recibe ``source: "pending"`` sin URL y el
frontend le enseña el cartel de «pendiente de configurar». Las cuentas de
servicio y las de demostración —donde el mapa compartido ES el contenido— lo
siguen viendo, y ``GRANIOT_EMBED_SHARED_FALLBACK_ENABLED`` devuelve el
comportamiento anterior si hiciera falta.
"""

import asyncio
import base64
import json
import time

import pytest
from fastapi import Response

from app.api.routers import graniot


class _FakeGraniotClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.payload


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._recorder.append(url)
        return self._response


def _fake_jwt(exp: int) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64({'token_type': 'access', 'exp': exp})}.sig"


def _patch_httpx(monkeypatch, response):
    recorder: list[str] = []
    monkeypatch.setattr(
        graniot.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(response, recorder),
    )
    return recorder


@pytest.fixture(autouse=True)
def _embed_settings(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_HOST", "embed.graniot.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PER_USER_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_SHARED_FALLBACK_ENABLED", False)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "gmateo@ingeoproyectos.com")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", "gmateo@dataris.es")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PASSWORD", "secret")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_URL", None)
    # Sin datos locales: la política decide por el email y por los metadatos demo.
    monkeypatch.setattr(graniot, "read_db", lambda: {})
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)
    graniot._cache_delete_prefix(graniot._COMPANY_FARMS_CACHE_KEY)


def _accounts_without_the_user():
    return _FakeGraniotClient([
        {
            "account_email": "otra@cuenta.com",
            "embedded_url": f"https://embed.graniot.com/?auth_id={_fake_jwt(int(time.time()) + 3600)}",
        },
        {
            "account_email": "gmateo@ingeoproyectos.com",
            "embedded_url": f"https://embed.graniot.com/?auth_id={_fake_jwt(int(time.time()) + 3600)}",
        },
    ])


def _resolve(monkeypatch, user, *, client=None):
    monkeypatch.setattr(graniot, "GraniotClient", lambda: client or _accounts_without_the_user())
    monkeypatch.setattr(graniot, "bearer_user", lambda authorization: user)
    recorder = _patch_httpx(monkeypatch, _FakeResponse(200, {"access": _fake_jwt(int(time.time()) + 3600)}))
    result = asyncio.run(graniot.get_embed_url(response=Response(), authorization="Bearer test"))
    return result, recorder


def test_usuario_nuevo_sin_portal_recibe_pendiente_y_ninguna_url(monkeypatch):
    result, recorder = _resolve(monkeypatch, {"id": "u1", "email": "nuevo@cliente.com"})

    assert result["data"]["source"] == "pending"
    assert result["data"]["status"] == "pending_setup"
    assert result["data"]["embedded_url"] is None
    assert result["data"]["account_email"] is None
    assert result["error"] is None
    # Ni siquiera se acuña el token de la cuenta de servicio: no hay nada que servir.
    assert recorder == []


def test_usuario_sin_email_tambien_queda_pendiente(monkeypatch):
    """Sin email no hay portal que aprovisionar; tampoco el mapa de otro."""
    result, _ = _resolve(monkeypatch, {"id": "u-sin-correo"})

    assert result["data"]["source"] == "pending"


def test_cuenta_de_servicio_sigue_viendo_su_portal(monkeypatch):
    result, recorder = _resolve(monkeypatch, {"id": "svc", "email": "gmateo@dataris.es"})

    assert result["data"]["source"] == "service"
    assert result["data"]["status"] == "ready"
    assert result["data"]["embedded_url"].startswith("https://embed.graniot.com/?auth_id=")
    assert recorder  # el token se acuña fresco


def test_usuario_de_demostracion_sigue_viendo_el_mapa_compartido(monkeypatch):
    """En la demo comercial el mapa compartido ES el contenido de la demo."""
    demo_user = {
        "id": "demo-1",
        "email": "demo@dataris.app",
        "user_metadata": {"demo_profile": "commercial"},
    }
    result, recorder = _resolve(monkeypatch, demo_user)

    assert result["data"]["source"] == "service"
    assert result["data"]["embedded_url"]
    assert recorder


def test_el_interruptor_devuelve_el_comportamiento_anterior(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_SHARED_FALLBACK_ENABLED", True)

    result, recorder = _resolve(monkeypatch, {"id": "u1", "email": "nuevo@cliente.com"})

    assert result["data"]["source"] == "service"
    assert result["data"]["embedded_url"]
    assert recorder


def test_quien_tiene_su_portal_no_se_ve_afectado(monkeypatch):
    personal = _fake_jwt(int(time.time()) + 3600)
    client = _FakeGraniotClient([
        {
            "account_email": "cliente@example.com",
            "embedded_url": f"https://embed.graniot.com/?auth_id={personal}",
        },
    ])

    result, recorder = _resolve(
        monkeypatch, {"id": "u1", "email": "cliente@example.com"}, client=client
    )

    assert result["data"]["source"] == "personal"
    assert result["data"]["status"] == "ready"
    assert result["data"]["embedded_url"].endswith(f"auth_id={personal}")
    assert recorder == []
