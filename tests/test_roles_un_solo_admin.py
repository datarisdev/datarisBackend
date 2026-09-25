"""H9: un solo rol de administrador cuyo alcance depende de la empresa.

El 24 sep 2026 se dio de alta en ASV un usuario como SuperAdmin: veía todos los
módulos por su rol y podía administrar todas las empresas. Ahora el
administrador de la empresa de Dataris es superadmin y el de cualquier otra
empresa es administrador de empresa, se pida lo que se pida.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-h9-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Seguridad2026!"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist_para_pruebas():
    previo = os.environ.get("DATARIS_ADMIN_PANEL_EMAILS")
    os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = "admin@dataris.local,*@dataris-test.com"
    yield
    if previo is None:
        os.environ.pop("DATARIS_ADMIN_PANEL_EMAILS", None)
    else:
        os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = previo


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str) -> str:
    response = client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    return _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])


def _dataris_company_id() -> str:
    db = compat.read_db(force_refresh=True)
    return next(c["id"] for c in compat.table(db, "companies") if c.get("name") == "DATARIS")


def _create_company(client: TestClient, token: str) -> str:
    response = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(token),
        json={"data": {"name": f"Cliente {uuid.uuid4().hex[:6]}", "max_hectares": 1000}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _create(client, token, *, company_id, admin_role):
    return client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(token),
        json={
            "email": f"u-{uuid.uuid4().hex[:8]}@dataris-test.com",
            "password": PASSWORD,
            "company_id": company_id,
            "admin_role": admin_role,
        },
    )


def _admin_row(user_id: str) -> dict:
    db = compat.read_db(force_refresh=True)
    return next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user_id)


@pytest.mark.parametrize("pedido", ["superadmin", "company_admin", "admin"])
def test_admin_de_un_cliente_nunca_es_superadmin(client, admin_token, pedido):
    company_id = _create_company(client, admin_token)
    response = _create(client, admin_token, company_id=company_id, admin_role=pedido)
    assert response.status_code == 200, response.text
    user_id = response.json()["data"]["user"]["id"]
    assert _admin_row(user_id)["admin_role"] == "company_admin"


@pytest.mark.parametrize("pedido", ["superadmin", "company_admin", "admin"])
def test_admin_de_dataris_es_superadmin(client, admin_token, pedido):
    response = _create(client, admin_token, company_id=_dataris_company_id(), admin_role=pedido)
    assert response.status_code == 200, response.text
    user_id = response.json()["data"]["user"]["id"]
    assert _admin_row(user_id)["admin_role"] == "superadmin"


def test_usuario_operativo_sigue_siendo_usuario(client, admin_token):
    response = _create(client, admin_token, company_id=_dataris_company_id(), admin_role="company_user")
    assert response.status_code == 200, response.text
    assert _admin_row(response.json()["data"]["user"]["id"])["admin_role"] == "company_user"


def test_admin_sin_empresa_se_rechaza(client, admin_token):
    response = _create(client, admin_token, company_id=None, admin_role="superadmin")
    # El superadmin que crea tiene empresa (DATARIS) y el alta la hereda si no
    # se manda otra: el caso sin empresa real solo se da con un id inexistente.
    assert response.status_code == 200, response.text
    response = _create(client, admin_token, company_id=str(uuid.uuid4()), admin_role="superadmin")
    assert response.status_code == 400


def test_editar_por_tablas_no_convierte_a_un_cliente_en_superadmin(client, admin_token):
    """La edición de usuarios del panel guarda el rol por el API genérico."""
    company_id = _create_company(client, admin_token)
    user_id = _create(client, admin_token, company_id=company_id, admin_role="company_user").json()["data"]["user"]["id"]
    row_id = _admin_row(user_id)["id"]

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(admin_token),
        json={"data": {"admin_role": "superadmin"}, "filters": [{"column": "id", "op": "eq", "value": row_id}]},
    )
    assert response.status_code == 200, response.text
    assert _admin_row(user_id)["admin_role"] == "company_admin"


def test_editar_a_un_superadmin_de_cliente_lo_corrige(client, admin_token):
    """Los superadmins que ya existen en empresas de cliente bajan al editarlos."""
    company_id = _create_company(client, admin_token)
    user_id = _create(client, admin_token, company_id=company_id, admin_role="company_user").json()["data"]["user"]["id"]
    db = compat.read_db(force_refresh=True)
    row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user_id)
    row["admin_role"] = "superadmin"  # como gmateo@asv.com en prod
    compat.write_db(db)

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(admin_token),
        json={"data": {"admin_role": "superadmin", "is_active": True}, "filters": [{"column": "id", "op": "eq", "value": row["id"]}]},
    )
    assert response.status_code == 200, response.text
    assert _admin_row(user_id)["admin_role"] == "company_admin"


def test_el_panel_sabe_que_empresa_es_la_de_dataris(client, admin_token):
    company_id = _create_company(client, admin_token)
    response = client.get("/api/compat/admin/module-access/companies", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    flags = {c["id"]: c["is_dataris_team"] for c in response.json()["data"]["companies"]}
    assert flags[_dataris_company_id()] is True
    assert flags[company_id] is False
