"""Blindaje del control de roles y alta de clientes (onboarding comercial).

Cubre el fallo reportado: un usuario sin permisos de administrador podía crear
otro usuario administrador. Además verifica que:

* ni un usuario normal ni una petición anónima pueden escribir en las tablas que
  conceden acceso o roles (`admin_users`, `user_roles`, `user_modules`),
* un `company_admin` no puede escalar creando otro administrador por el API
  genérico de tablas (la escalada concreta que existía),
* el auto-registro público NUNCA nace con rol de administrador, y
* un comercial con `can_onboard_clients` puede dar de alta un cliente (empresa +
  su administrador) pero jamás un superadministrador.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-role-sec-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist_para_pruebas():
    # Estas pruebas validan la lógica de roles con actores @dataris-test.com;
    # se amplía la lista blanca del panel para que el candado por email no las
    # tape. El candado en sí se prueba en test_admin_panel_allowlist.py.
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


def _create_company(client: TestClient, admin_token: str, name: str) -> str:
    response = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(admin_token),
        json={"data": {"name": name, "max_hectares": 10000}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _create_user(client, admin_token, *, email, company_id=None, admin_role="company_user") -> str:
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": "Seguridad2026!",
            "first_name": "Usuario",
            "last_name": "Prueba",
            "company_id": company_id,
            "admin_role": admin_role,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["user"]["id"]


def _admin_role_of(user_id: str) -> str | None:
    db = compat.read_db(force_refresh=True)
    row = next((r for r in compat.table(db, "admin_users") if r.get("user_id") == user_id), None)
    return row.get("admin_role") if row else None


# --- La escalada reportada -------------------------------------------------


def test_company_admin_no_puede_crear_otro_admin_por_tablas(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Empresa {uuid.uuid4().hex[:6]}")
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=admin_email, company_id=company_id, admin_role="company_admin")
    admin_tok = _sign_in(client, admin_email, "Seguridad2026!")

    # El company_admin crea un usuario e intenta convertirlo en administrador
    # insertando su fila con admin_role="company_admin" por el API genérico.
    victim_id = str(uuid.uuid4())
    response = client.post(
        "/api/compat/tables/admin_users/insert",
        headers=_auth(admin_tok),
        json={"data": {"user_id": victim_id, "admin_role": "company_admin", "company_id": company_id}},
    )
    assert response.status_code == 200, response.text
    # El rol NO puede haber quedado como administrador: se degrada a company_user.
    assert _admin_role_of(victim_id) == "company_user"


def test_usuario_normal_no_escribe_tablas_de_acceso(client: TestClient, admin_token: str):
    email = f"user-{uuid.uuid4().hex[:8]}@dataris-test.com"
    user_id = _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Seguridad2026!")

    # No puede darse a sí mismo un rol admin.
    r1 = client.post(
        "/api/compat/tables/user_roles/insert",
        headers=_auth(token),
        json={"data": {"user_id": user_id, "role": "admin"}},
    )
    assert r1.status_code == 403, r1.text

    # No puede concederse módulos.
    r2 = client.post(
        "/api/compat/tables/user_modules/insert",
        headers=_auth(token),
        json={"data": {"user_id": user_id, "module_id": "satelite"}},
    )
    assert r2.status_code == 403, r2.text

    # No puede insertarse en admin_users.
    r3 = client.post(
        "/api/compat/tables/admin_users/insert",
        headers=_auth(token),
        json={"data": {"user_id": user_id, "admin_role": "company_admin"}},
    )
    assert r3.status_code == 403, r3.text


def test_anonimo_no_escribe_tablas_de_acceso(client: TestClient):
    r1 = client.post(
        "/api/compat/tables/user_roles/insert",
        json={"data": {"user_id": str(uuid.uuid4()), "role": "admin"}},
    )
    assert r1.status_code == 403, r1.text

    r2 = client.post(
        "/api/compat/tables/admin_users/insert",
        json={"data": {"user_id": str(uuid.uuid4()), "admin_role": "company_admin"}},
    )
    assert r2.status_code == 403, r2.text


def test_signup_publico_no_nace_admin(client: TestClient):
    email = f"selfsignup-{uuid.uuid4().hex[:8]}@dataris-test.com"
    response = client.post(
        "/api/compat/auth/sign-up",
        json={"email": email, "password": "Seguridad2026!", "options": {"data": {"role": "admin"}}},
    )
    assert response.status_code == 200, response.text
    db = compat.read_db(force_refresh=True)
    created = next(u for u in db["users"] if u.get("email") == email)
    role_row = next(r for r in compat.table(db, "user_roles") if r.get("user_id") == created["id"])
    assert role_row.get("role") == "user"


def test_borrar_usuario_exige_superadmin(client: TestClient, admin_token: str):
    email = f"victima-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Seguridad2026!")
    # Un usuario normal no puede borrar a nadie.
    r = client.delete(f"/api/compat/auth/admin/users/{uuid.uuid4()}", headers=_auth(token))
    assert r.status_code == 403, r.text
    # Anónimo tampoco.
    r2 = client.delete(f"/api/compat/auth/admin/users/{uuid.uuid4()}")
    assert r2.status_code in (401, 403), r2.text


# --- Onboarding comercial --------------------------------------------------


def _grant_onboarding(email: str) -> None:
    with compat.LOCK:
        db = compat.read_db()
        user = next(u for u in db["users"] if u.get("email") == email)
        row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user["id"])
        row[compat.CLIENT_ONBOARDER_FIELD] = True
        compat.write_db(db)


def test_comercial_da_de_alta_cliente(client: TestClient, admin_token: str):
    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=comercial_email)
    _grant_onboarding(comercial_email)
    token = _sign_in(client, comercial_email, "Seguridad2026!")

    ctx = client.get("/api/compat/admin/clients/context", headers=_auth(token))
    assert ctx.status_code == 200 and ctx.json()["data"]["allowed"] is True

    client_admin_email = f"cliente-admin-{uuid.uuid4().hex[:8]}@dataris-test.com"
    response = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(token),
        json={
            "company_name": "Agrícola Nueva",
            "max_hectares": 500,
            "email": client_admin_email,
            "password": "ClienteNuevo2026!",
            "first_name": "Cliente",
            "last_name": "Nuevo",
        },
    )
    assert response.status_code == 200, response.text
    new_user_id = response.json()["data"]["user"]["id"]
    # El usuario creado es admin de SU empresa, nunca superadmin.
    assert _admin_role_of(new_user_id) == "company_admin"
    # Y puede iniciar sesión.
    _sign_in(client, client_admin_email, "ClienteNuevo2026!")


def test_usuario_sin_permiso_no_da_de_alta_cliente(client: TestClient, admin_token: str):
    email = f"nopuede-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Seguridad2026!")

    response = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(token),
        json={"company_name": "Intruso", "email": f"x-{uuid.uuid4().hex[:6]}@t.com", "password": "Intruso2026!"},
    )
    assert response.status_code == 403, response.text
