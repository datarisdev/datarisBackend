"""El país del perfil solo lo cambia la administración.

El país decide la marca con la que se sirve el producto (Innovagro en México,
Dataris en el resto). El campo se retiró de la pantalla de perfil, pero eso solo
lo esconde: aquí se comprueba que un usuario normal tampoco puede cambiarlo por
la vía del API, y que el resto de su perfil se sigue guardando igual.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-country-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Paises2026!"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str) -> str:
    response = client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    return _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])


@pytest.fixture(scope="module")
def usuario(client: TestClient, admin_token: str) -> dict:
    """Cliente de México dado de alta por la administración."""
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": PASSWORD,
            "first_name": "Cliente",
            "last_name": "De Prueba",
            "admin_role": "company_user",
            "country": "MX",
        },
    )
    assert response.status_code == 200, response.text
    return {"id": response.json()["data"]["user"]["id"], "email": email}


def _country_of(client: TestClient, token: str, user_id: str) -> str | None:
    response = client.post(
        "/api/compat/tables/profiles/query",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": user_id}]},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    return rows[0].get("country") if rows else None


def test_el_alta_desde_el_panel_guarda_el_pais(client: TestClient, admin_token: str, usuario: dict):
    assert _country_of(client, admin_token, usuario["id"]) == "MX"


def test_el_usuario_no_puede_cambiarse_el_pais(client: TestClient, admin_token: str, usuario: dict):
    token = _sign_in(client, usuario["email"], PASSWORD)

    response = client.post(
        "/api/compat/tables/profiles/upsert",
        headers=_auth(token),
        json={
            "data": {
                "id": usuario["id"],
                "user_id": usuario["id"],
                "email": usuario["email"],
                "phone": "+52 55 1234 5678",
                "country": "GT",
            },
            "onConflict": "id",
        },
    )
    # No es un error: se guarda el resto del perfil y el país se ignora.
    assert response.status_code == 200, response.text
    assert _country_of(client, admin_token, usuario["id"]) == "MX"


def test_el_resto_del_perfil_si_se_guarda(client: TestClient, admin_token: str, usuario: dict):
    token = _sign_in(client, usuario["email"], PASSWORD)
    client.post(
        "/api/compat/tables/profiles/upsert",
        headers=_auth(token),
        json={
            "data": {
                "id": usuario["id"],
                "user_id": usuario["id"],
                "email": usuario["email"],
                "phone": "+52 55 9999 0000",
                "location": "Culiacán",
                "country": "GT",
            },
            "onConflict": "id",
        },
    )
    response = client.post(
        "/api/compat/tables/profiles/query",
        headers=_auth(admin_token),
        json={"filters": [{"column": "id", "op": "eq", "value": usuario["id"]}]},
    )
    fila = response.json()["data"][0]
    assert fila["phone"] == "+52 55 9999 0000"
    assert fila["location"] == "Culiacán"
    assert fila["country"] == "MX"


def test_tampoco_por_update(client: TestClient, admin_token: str, usuario: dict):
    token = _sign_in(client, usuario["email"], PASSWORD)
    response = client.post(
        "/api/compat/tables/profiles/update",
        headers=_auth(token),
        json={
            "data": {"country": "GT", "phone": "+52 55 1111 2222"},
            "filters": [{"column": "id", "op": "eq", "value": usuario["id"]}],
        },
    )
    assert response.status_code == 200, response.text
    assert _country_of(client, admin_token, usuario["id"]) == "MX"


def test_la_administracion_si_lo_cambia(client: TestClient, admin_token: str, usuario: dict):
    response = client.post(
        "/api/compat/tables/profiles/update",
        headers=_auth(admin_token),
        json={
            "data": {"country": "GT"},
            "filters": [{"column": "id", "op": "eq", "value": usuario["id"]}],
        },
    )
    assert response.status_code == 200, response.text
    assert _country_of(client, admin_token, usuario["id"]) == "GT"

    # Y se devuelve a México para no dejar el dato cambiado a medias.
    client.post(
        "/api/compat/tables/profiles/update",
        headers=_auth(admin_token),
        json={
            "data": {"country": "MX"},
            "filters": [{"column": "id", "op": "eq", "value": usuario["id"]}],
        },
    )
    assert _country_of(client, admin_token, usuario["id"]) == "MX"
