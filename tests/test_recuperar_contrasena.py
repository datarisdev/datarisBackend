"""«Olvidé mi contraseña» de verdad.

`/auth/reset-password` respondía «ok» sin enviar nada. Ahora manda un enlace de un
solo uso que caduca, y `/auth/recover` fija la contraseña nueva con él. Cubre
además que el enlace no delate qué correos existen ni pueda llevar fuera de
Dataris, y que el envío use Resend cuando está configurado.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-recuperar-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Original2026!"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def buzon(monkeypatch):
    """Captura los correos en vez de enviarlos."""
    enviados: list[dict] = []

    def fake_send(to, subject, text):
        enviados.append({"to": to, "subject": subject, "text": text})
        return {"sent": True, "reason": None}

    monkeypatch.setattr(compat, "send_email", fake_send)
    monkeypatch.setenv("FRONTEND_URL", "https://app.dataris.es")
    return enviados


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str):
    return client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})


def _create_user(client: TestClient) -> str:
    admin = _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"]).json()["data"]["session"]["access_token"]
    email = f"recupera-{uuid.uuid4().hex[:8]}@cliente-final.com"
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin),
        json={"email": email, "password": PASSWORD, "first_name": "Ana", "admin_role": "company_user"},
    )
    assert response.status_code == 200, response.text
    return email


def _link_from(mail: dict) -> str:
    return next(line for line in mail["text"].splitlines() if line.startswith("http"))


def _token_from(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def test_un_correo_desconocido_responde_igual_y_no_envia_nada(client, buzon):
    response = client.post("/api/compat/auth/reset-password", json={"email": "nadie@cliente-final.com"})
    assert response.status_code == 200
    assert response.json()["data"]["message"] == compat.PASSWORD_RESET_GENERIC_MESSAGE
    assert buzon == []


def test_el_enlace_permite_elegir_contrasena_nueva_una_sola_vez(client, buzon):
    email = _create_user(client)

    response = client.post(
        "/api/compat/auth/reset-password",
        json={"email": email.upper(), "options": {"redirectTo": "https://app.dataris.es/reset-password"}},
    )
    assert response.status_code == 200
    assert response.json()["data"]["message"] == compat.PASSWORD_RESET_GENERIC_MESSAGE
    assert len(buzon) == 1 and buzon[0]["to"] == email

    link = _link_from(buzon[0])
    assert link.startswith("https://app.dataris.es/reset-password?type=recovery&token=")
    token = _token_from(link)

    # El token no queda legible en el almacén: solo su hash, y fuera de `tables`.
    db = compat.read_db(force_refresh=True)
    assert token not in str(db.get(compat.PASSWORD_RESETS_KEY))
    assert compat.PASSWORD_RESETS_KEY not in db["tables"]

    corta = client.post("/api/compat/auth/recover", json={"token": token, "password": "corta"})
    assert corta.status_code == 400

    ok = client.post("/api/compat/auth/recover", json={"token": token, "password": "NuevaClave2026!"})
    assert ok.status_code == 200, ok.text
    assert _sign_in(client, email, "NuevaClave2026!").status_code == 200
    assert _sign_in(client, email, PASSWORD).status_code == 401

    # Un solo uso.
    otra_vez = client.post("/api/compat/auth/recover", json={"token": token, "password": "OtraClave2026!"})
    assert otra_vez.status_code == 400


def test_el_enlace_nunca_lleva_fuera_de_dataris(client, buzon):
    email = _create_user(client)

    client.post(
        "/api/compat/auth/reset-password",
        json={"email": email, "options": {"redirectTo": "https://sitio-falso.com/admin/reset-password"}},
    )
    client.post(
        "/api/compat/auth/reset-password",
        json={"email": email, "options": {"redirectTo": "https://sitio-falso.com/robar"}},
    )
    assert _link_from(buzon[0]).startswith("https://app.dataris.es/admin/reset-password?")
    assert _link_from(buzon[1]).startswith("https://app.dataris.es/reset-password?")

    # Pedir un enlace nuevo invalida el anterior.
    viejo = client.post(
        "/api/compat/auth/recover",
        json={"token": _token_from(_link_from(buzon[0])), "password": "NuevaClave2026!"},
    )
    assert viejo.status_code == 400


def test_un_enlace_caducado_no_sirve(client, buzon):
    email = _create_user(client)
    client.post("/api/compat/auth/reset-password", json={"email": email})
    token = _token_from(_link_from(buzon[0]))

    with compat.LOCK:
        db = compat.read_db(force_refresh=True)
        for entry in db[compat.PASSWORD_RESETS_KEY]:
            entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        compat.write_db(db)

    response = client.post("/api/compat/auth/recover", json={"token": token, "password": "NuevaClave2026!"})
    assert response.status_code == 400
    assert _sign_in(client, email, PASSWORD).status_code == 200


def test_con_resend_configurado_se_envia_por_su_api(monkeypatch):
    llamadas: list[dict] = []

    class Respuesta:
        status_code = 200
        text = '{"id": "abc"}'

    def fake_post(url, json=None, headers=None, timeout=None):
        llamadas.append({"url": url, "json": json, "headers": headers})
        return Respuesta()

    monkeypatch.setenv("RESEND_API_KEY", "re_prueba")
    monkeypatch.setenv("EMAIL_FROM", "no-responder@ejemplo.com")
    monkeypatch.setattr(compat.httpx, "post", fake_post)

    result = compat.send_email("persona@cliente-final.com", "Asunto", "Texto")
    assert result == {"sent": True, "reason": None}
    assert llamadas[0]["url"] == "https://api.resend.com/emails"
    assert llamadas[0]["json"]["from"] == "Dataris <no-responder@ejemplo.com>"
    assert llamadas[0]["json"]["to"] == ["persona@cliente-final.com"]
    assert llamadas[0]["headers"]["Authorization"] == "Bearer re_prueba"


def test_si_resend_rechaza_el_envio_no_revienta(monkeypatch):
    class Rechazo:
        status_code = 403
        text = "The domain is not verified"

    monkeypatch.setenv("RESEND_API_KEY", "re_prueba")
    monkeypatch.setenv("EMAIL_FROM", "no-responder@ejemplo.com")
    monkeypatch.setattr(compat.httpx, "post", lambda *a, **k: Rechazo())

    result = compat.send_email("persona@cliente-final.com", "Asunto", "Texto")
    assert result["sent"] is False
    assert "403" in result["reason"]
