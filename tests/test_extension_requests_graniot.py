"""La solicitud de extensión respeta el módulo elegido (Graniot ≠ DigiformsApp).

Antes, si faltaba `extension_id`, la solicitud se guardaba silenciosamente como
digiforms; y Graniot no estaba sembrado en platform_modules, así que su nombre
no resolvía. Aquí se comprueba que una solicitud de Graniot queda como Graniot y
que una petición sin módulo se rechaza.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-ext-graniot-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str) -> str:
    r = client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["session"]["access_token"]


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    return _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])


def _new_user(client, admin_token, email):
    r = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={"email": email, "password": "Extension2026!", "first_name": "Ext", "last_name": "User"},
    )
    assert r.status_code == 200, r.text


def test_graniot_esta_en_el_catalogo_de_platform_modules():
    db = compat.read_db(force_refresh=True)
    graniot = next((m for m in compat.table(db, "platform_modules") if m.get("id") == "graniot"), None)
    assert graniot is not None
    assert graniot.get("name") == "Graniot"


def test_solicitud_de_graniot_queda_como_graniot(client: TestClient, admin_token: str):
    email = f"cliente-graniot-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _new_user(client, admin_token, email)
    token = _sign_in(client, email, "Extension2026!")

    r = client.post(
        "/api/compat/extensions/requests",
        headers=_auth(token),
        json={"extension_id": "graniot", "contact_notes": "Quiero activar Graniot"},
    )
    assert r.status_code == 200, r.text
    request = r.json()["data"]["request"]
    assert request["extension_id"] == "graniot"
    assert request["extension_name"] == "Graniot"
    assert request["extension_name"] != "DigiformsApp"


def test_solicitud_sin_extension_id_se_rechaza(client: TestClient, admin_token: str):
    email = f"cliente-vacio-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _new_user(client, admin_token, email)
    token = _sign_in(client, email, "Extension2026!")

    r = client.post(
        "/api/compat/extensions/requests",
        headers=_auth(token),
        json={"contact_notes": "sin módulo"},
    )
    assert r.status_code == 400, r.text
