"""La carga de lotes pasó del perfil del cliente al panel de administración.

Cubre las dos mitades del cambio:

* el cliente ya no puede dar de alta ni borrar sus lotes (ni por los endpoints
  de carga ni por el API genérico de tablas), y
* las cuentas del panel de Dataris (las de la lista blanca) sí pueden hacerlo en
  nombre de cualquier usuario de cualquier empresa, sin ningún permiso por fila.
  Los permisos por fila antiguos (`can_manage_parcels`, `can_manage_all_parcels`)
  ya no abren nada por sí solos, tampoco las rutas de Graniot.
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
# Las cuentas del panel son las @dataris-test.com (ver la lista blanca de abajo);
# los clientes, de un dominio que la lista no cubre.
CLIENT_DOMAIN = "cliente-final.com"


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
    # Las cuentas @dataris-test.com hacen de equipo de Dataris: se amplía la
    # lista blanca del panel para ellas. El candado en sí se prueba en
    # test_admin_panel_allowlist.py.
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
    """Marca los permisos antiguos directamente en el almacén."""
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


def _client_email(prefix: str = "cliente") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@{CLIENT_DOMAIN}"


# --- El cliente ya no gestiona sus lotes ----------------------------------


def test_el_cliente_no_puede_crear_lotes(client: TestClient, admin_token: str):
    email = _client_email()
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
    email = _client_email()
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
    email = _client_email()
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
    email = _client_email()
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
    user_id = _create_user(client, admin_token, email=_client_email())

    response = client.get("/api/compat/admin/parcels/users", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["scope"] == "all"
    assert user_id in {user["id"] for user in data["users"]}


def test_una_cuenta_del_panel_gestiona_lotes_de_cualquier_empresa(client: TestClient, admin_token: str):
    # Sin ningún permiso marcado y sin ser administradora: basta con estar en la
    # lista blanca del panel.
    company_a = _create_company(client, admin_token, f"Empresa A {uuid.uuid4().hex[:6]}")
    company_b = _create_company(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")

    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=comercial_email, company_id=company_a)
    comercial_token = _sign_in(client, comercial_email, "Lotes2026!")

    ajeno_id = _create_user(client, admin_token, email=_client_email("ajeno"), company_id=company_b)

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(comercial_token))
    data = context.json()["data"]
    assert data["allowed"] is True
    assert data["scope"] == "all"
    assert data["admin_role"] == "company_user"

    users = client.get("/api/compat/admin/parcels/users", headers=_auth(comercial_token))
    assert users.status_code == 200, users.text
    assert ajeno_id in {user["id"] for user in users.json()["data"]["users"]}

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": ajeno_id, "name": "Lote de otra empresa", "geometry": polygon(0.31)},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["parcel"]["user_id"] == ajeno_id


def test_los_permisos_por_fila_ya_no_abren_la_gestion_de_lotes(client: TestClient, admin_token: str):
    # Fuera de la lista blanca, los permisos antiguos no sirven por ninguna vía.
    # Incluye las rutas de Graniot «en nombre de otro usuario», que antes solo
    # miraban el permiso de la fila (hallazgo C-001).
    comercial_email = _client_email("comercial")
    _create_user(client, admin_token, email=comercial_email)
    _grant_parcel_permission(comercial_email, global_scope=True)
    token = _sign_in(client, comercial_email, "Lotes2026!")

    cliente_id = _create_user(client, admin_token, email=_client_email())

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(token))
    assert context.json()["data"]["allowed"] is False

    users = client.get("/api/compat/admin/parcels/users", headers=_auth(token))
    assert users.status_code == 403

    manual = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(token),
        json={"user_id": cliente_id, "name": "Lote ajeno", "geometry": polygon(0.40)},
    )
    assert manual.status_code == 403

    target = client.get(
        "/api/graniot/parcels/sync-target",
        headers=_auth(token),
        params={"user_id": cliente_id},
    )
    assert target.status_code == 403, target.text

    unsync = client.delete(
        f"/api/graniot/parcels/sync-local/{uuid.uuid4()}",
        headers=_auth(token),
        params={"user_id": cliente_id},
    )
    assert unsync.status_code == 403, unsync.text


def test_no_se_borran_lotes_de_otro_usuario(client: TestClient, admin_token: str):
    dueno_id = _create_user(client, admin_token, email=_client_email("dueno"))
    otro_id = _create_user(client, admin_token, email=_client_email("otro"))

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


def test_un_admin_de_empresa_no_asciende_a_nadie(client: TestClient, admin_token: str):
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

    comercial_id = _create_user(client, admin_token, email=_client_email("comercial"), company_id=company_id)

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

    db = compat.read_db(force_refresh=True)
    row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == comercial_id)
    # El rol no sube y los permisos marcados no le abren la gestión de lotes.
    assert row["admin_role"] == "company_user"
    assert compat.can_manage_parcels(db, comercial_id) is False


def test_el_alta_sin_lista_de_modulos_hereda_el_paquete_de_la_empresa(client: TestClient, admin_token: str):
    # Antes, un alta sin `modules` bloqueaba de forma explícita todo el paquete
    # de la empresa y el usuario nacía sin módulos.
    company_id = _create_company(client, admin_token, f"Empresa D {uuid.uuid4().hex[:6]}")
    paquete = client.put(
        f"/api/compat/admin/module-access/companies/{company_id}",
        headers=_auth(admin_token),
        json={"modules": {"satelite": True, "telemetria": True}},
    )
    assert paquete.status_code == 200, paquete.text

    hereda_id = _create_user(client, admin_token, email=_client_email("hereda"), company_id=company_id)
    detalle = client.get(f"/api/compat/admin/module-access/users/{hereda_id}", headers=_auth(admin_token))
    modulos = {m["id"]: m for m in detalle.json()["data"]["modules"]}
    for module_id in ("satelite", "telemetria"):
        assert modulos[module_id]["override"] is None
        assert modulos[module_id]["effective"] is True

    # Con lista explícita se respeta: lo que no se marca queda bloqueado.
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": _client_email("elige"),
            "password": "Lotes2026!",
            "company_id": company_id,
            "admin_role": "company_user",
            "modules": ["satelite"],
        },
    )
    assert response.status_code == 200, response.text
    elige_id = response.json()["data"]["user"]["id"]
    detalle = client.get(f"/api/compat/admin/module-access/users/{elige_id}", headers=_auth(admin_token))
    modulos = {m["id"]: m for m in detalle.json()["data"]["modules"]}
    assert modulos["satelite"]["effective"] is True
    assert modulos["telemetria"]["effective"] is False
