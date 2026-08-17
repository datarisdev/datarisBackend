"""La carga de lotes pasó del perfil del cliente al panel de administración.

Cubre las dos mitades del cambio:

* el cliente ya no puede dar de alta ni borrar sus lotes (ni por los endpoints
  de carga ni por el API genérico de tablas), y
* el equipo de Dataris —administradores y los comerciales con el permiso
  `can_manage_parcels`— sí puede hacerlo en nombre de un usuario concreto,
  siempre dentro de su alcance.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-admin-parcels-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}


def polygon(offset: float = 0.0) -> dict:
    west, south = -90.5 + offset, 14.5 + offset
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [west, south + 0.01],
            [west + 0.01, south + 0.01],
            [west + 0.01, south],
            [west, south],
        ]],
    }


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist_para_pruebas():
    # Estas pruebas validan el alcance de la gestión de lotes con gestores
    # @dataris-test.com; se amplía la lista blanca del panel para que el candado
    # por email no las tape. El candado se prueba en test_admin_panel_allowlist.py.
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


def _create_user(
    client: TestClient,
    admin_token: str,
    *,
    email: str,
    company_id: str | None = None,
    admin_role: str = "company_user",
) -> str:
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": "Lotes2026!",
            "first_name": "Usuario",
            "last_name": "Prueba",
            "company_id": company_id,
            "admin_role": admin_role,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["user"]["id"]


def _create_company(client: TestClient, admin_token: str, name: str) -> str:
    response = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(admin_token),
        json={"data": {"name": name, "max_hectares": 10000}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _grant_parcel_permission(admin_user_email: str, *, global_scope: bool) -> None:
    """Marca el permiso directamente en el almacén, como haría el panel."""
    with compat.LOCK:
        db = compat.read_db()
        user = next(u for u in db["users"] if u.get("email") == admin_user_email)
        row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user["id"])
        row[compat.PARCEL_MANAGER_FIELD] = True
        row[compat.PARCEL_MANAGER_ALL_FIELD] = global_scope
        compat.write_db(db)


def _parcels_of(client: TestClient, token: str) -> list[dict]:
    response = client.post("/api/compat/tables/parcels/query", headers=_auth(token), json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- El cliente ya no gestiona sus lotes ----------------------------------


def test_el_cliente_no_puede_crear_lotes(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    manual = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote propio", "geometry": polygon(0.10)},
    )
    assert manual.status_code == 403
    assert "equipo de Dataris" in manual.json()["detail"]

    inserted = client.post(
        "/api/compat/tables/parcels/insert",
        headers=_auth(token),
        json={"data": {"name": "Lote por tabla", "geometry": polygon(0.11)}},
    )
    assert inserted.status_code == 403


def test_el_cliente_no_puede_borrar_sus_lotes(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    user_id = _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"user_id": user_id, "name": "Lote del cliente", "geometry": polygon(0.12)},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]

    removed = client.post(
        "/api/compat/tables/parcels/delete",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}]},
    )
    assert removed.status_code == 403
    assert [p["id"] for p in _parcels_of(client, token)] == [parcel_id]


def test_el_cliente_no_puede_concederse_el_permiso(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    user_id = _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(token),
        json={
            "data": {compat.PARCEL_MANAGER_FIELD: True, compat.PARCEL_MANAGER_ALL_FIELD: True},
            "filters": [{"column": "user_id", "op": "eq", "value": user_id}],
        },
    )
    assert response.status_code == 403

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(token))
    assert context.status_code == 200
    assert context.json()["data"]["allowed"] is False


# --- El equipo de Dataris sí ----------------------------------------------


def test_el_superadmin_carga_lotes_para_un_usuario(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    user_id = _create_user(client, admin_token, email=email)
    client_token = _sign_in(client, email, "Lotes2026!")

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"user_id": user_id, "name": "Lote administrado", "geometry": polygon(0.20)},
    )
    assert created.status_code == 200, created.text
    parcel = created.json()["data"]["parcel"]
    assert parcel["user_id"] == user_id
    assert parcel["area"] > 0

    # El dueño lo ve como propio aunque no lo haya cargado él.
    assert [p["id"] for p in _parcels_of(client, client_token)] == [parcel["id"]]

    listed = client.get(
        "/api/compat/admin/parcels/list",
        headers=_auth(admin_token),
        params={"user_id": user_id},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()["data"]
    assert body["user"]["email"] == email
    assert body["user"]["parcel_count"] == 1
    assert [p["id"] for p in body["parcels"]] == [parcel["id"]]

    removed = client.post(
        "/api/compat/admin/parcels/delete",
        headers=_auth(admin_token),
        json={"user_id": user_id, "ids": [parcel["id"]]},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["count"] == 1
    assert _parcels_of(client, client_token) == []


def test_el_listado_de_usuarios_incluye_a_los_gestionables(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    user_id = _create_user(client, admin_token, email=email)

    response = client.get("/api/compat/admin/parcels/users", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["scope"] == "all"
    assert user_id in {user["id"] for user in data["users"]}


def test_un_comercial_con_permiso_carga_lotes_de_cualquier_empresa(client: TestClient, admin_token: str):
    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=comercial_email)
    comercial_token = _sign_in(client, comercial_email, "Lotes2026!")

    cliente_email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    cliente_id = _create_user(client, admin_token, email=cliente_email)

    # Sin permiso, el comercial no entra.
    denied = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": cliente_id, "name": "Lote comercial", "geometry": polygon(0.30)},
    )
    assert denied.status_code == 403

    _grant_parcel_permission(comercial_email, global_scope=True)

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(comercial_token))
    assert context.json()["data"] == {
        "allowed": True,
        "scope": "all",
        "admin_role": "company_user",
        "company_id": context.json()["data"]["company_id"],
        "company_name": context.json()["data"]["company_name"],
    }

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": cliente_id, "name": "Lote comercial", "geometry": polygon(0.31)},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["parcel"]["user_id"] == cliente_id


def test_un_comercial_sin_alcance_global_solo_ve_su_empresa(client: TestClient, admin_token: str):
    company_a = _create_company(client, admin_token, f"Empresa A {uuid.uuid4().hex[:6]}")
    company_b = _create_company(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")

    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=comercial_email, company_id=company_a)
    comercial_token = _sign_in(client, comercial_email, "Lotes2026!")
    _grant_parcel_permission(comercial_email, global_scope=False)

    propio_id = _create_user(
        client,
        admin_token,
        email=f"propio-{uuid.uuid4().hex[:8]}@dataris-test.com",
        company_id=company_a,
    )
    ajeno_id = _create_user(
        client,
        admin_token,
        email=f"ajeno-{uuid.uuid4().hex[:8]}@dataris-test.com",
        company_id=company_b,
    )

    users = client.get("/api/compat/admin/parcels/users", headers=_auth(comercial_token))
    assert users.status_code == 200, users.text
    visible = {user["id"] for user in users.json()["data"]["users"]}
    assert propio_id in visible
    assert ajeno_id not in visible

    permitido = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": propio_id, "name": "Lote de mi empresa", "geometry": polygon(0.40)},
    )
    assert permitido.status_code == 200, permitido.text

    prohibido = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": ajeno_id, "name": "Lote ajeno", "geometry": polygon(0.41)},
    )
    assert prohibido.status_code == 403


def test_no_se_borran_lotes_de_otro_usuario(client: TestClient, admin_token: str):
    dueno_id = _create_user(client, admin_token, email=f"dueno-{uuid.uuid4().hex[:8]}@dataris-test.com")
    otro_id = _create_user(client, admin_token, email=f"otro-{uuid.uuid4().hex[:8]}@dataris-test.com")

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"user_id": dueno_id, "name": "Lote intacto", "geometry": polygon(0.50)},
    )
    parcel_id = created.json()["data"]["parcel"]["id"]

    response = client.post(
        "/api/compat/admin/parcels/delete",
        headers=_auth(admin_token),
        json={"user_id": otro_id, "ids": [parcel_id]},
    )
    assert response.status_code == 404

    listed = client.get(
        "/api/compat/admin/parcels/list",
        headers=_auth(admin_token),
        params={"user_id": dueno_id},
    )
    assert [p["id"] for p in listed.json()["data"]["parcels"]] == [parcel_id]


def test_el_alcance_global_solo_lo_concede_un_superadmin(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Empresa C {uuid.uuid4().hex[:6]}")
    company_admin_email = f"admin-empresa-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(
        client,
        admin_token,
        email=company_admin_email,
        company_id=company_id,
        admin_role="company_admin",
    )
    company_admin_token = _sign_in(client, company_admin_email, "Lotes2026!")

    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    comercial_id = _create_user(client, admin_token, email=comercial_email, company_id=company_id)

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(company_admin_token),
        json={
            "data": {
                compat.PARCEL_MANAGER_FIELD: True,
                compat.PARCEL_MANAGER_ALL_FIELD: True,
                "admin_role": "superadmin",
            },
            "filters": [{"column": "user_id", "op": "eq", "value": comercial_id}],
        },
    )
    assert response.status_code == 200, response.text

    comercial_token = _sign_in(client, comercial_email, "Lotes2026!")
    context = client.get("/api/compat/admin/parcels/context", headers=_auth(comercial_token))
    data = context.json()["data"]
    assert data["allowed"] is True
    # El permiso se concedió, pero acotado a la empresa y sin ascender el rol.
    assert data["scope"] == "company"
    assert data["admin_role"] == "company_user"
