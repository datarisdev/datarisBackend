"""Candado del panel de administración: lista blanca de cuentas.

El panel /admin queda restringido a las cuentas autorizadas de Dataris
(por defecto admin@dataris.local, admin@dataris.es y gmateo@dataris.es,
ajustable con DATARIS_ADMIN_PANEL_EMAILS). Cualquier otra cuenta —aunque
tenga fila activa en `admin_users` con rol de administrador o permisos de
comercial— NO puede entrar al panel ni usar sus endpoints:

* `GET /admin/panel-access` (la fuente de verdad del frontend) responde
  allowed=False,
* los endpoints administrativos (/admin/users/manual, /admin/clients/onboard,
  /admin/parcels/*, extensiones) devuelven 403,
* las escrituras a las tablas privilegiadas (admin_users, companies,
  user_modules…) devuelven 403.

Los roles de esas filas siguen valiendo para el acceso a módulos de la app:
aquí solo se cierra el panel.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-panel-allowlist-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Panel2026!seguro"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _lista_blanca_por_defecto():
    # Se fija explícitamente la lista por defecto para que estas pruebas no
    # dependan de lo que otros módulos de test dejaron en el entorno.
    previo = os.environ.get("DATARIS_ADMIN_PANEL_EMAILS")
    os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = compat.DEFAULT_ADMIN_PANEL_EMAILS
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


def _create_user(client, admin_token, *, email, admin_role="company_user", company_id=None) -> str:
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": PASSWORD,
            "first_name": "Prueba",
            "last_name": "Panel",
            "admin_role": admin_role,
            "company_id": company_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["user"]["id"]


def _set_admin_flag(email: str, field: str, value) -> None:
    with compat.LOCK:
        db = compat.read_db()
        user = next(u for u in db["users"] if u.get("email") == email)
        row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user["id"])
        row[field] = value
        compat.write_db(db)


def test_cuenta_autorizada_entra_al_panel(client: TestClient, admin_token: str):
    response = client.get("/api/compat/admin/panel-access", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["allowed"] is True


def test_anonimo_y_usuario_normal_no_entran(client: TestClient, admin_token: str):
    anon = client.get("/api/compat/admin/panel-access")
    assert anon.status_code == 200 and anon.json()["data"]["allowed"] is False

    email = f"normal-{uuid.uuid4().hex[:8]}@cliente-final.com"
    _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, PASSWORD)
    response = client.get("/api/compat/admin/panel-access", headers=_auth(token))
    assert response.status_code == 200 and response.json()["data"]["allowed"] is False


def test_company_admin_no_listado_queda_fuera_del_panel(client: TestClient, admin_token: str):
    email = f"admin-cliente-{uuid.uuid4().hex[:8]}@cliente-final.com"
    _create_user(client, admin_token, email=email, admin_role="company_admin")
    _set_admin_flag(email, "admin_role", "company_admin")
    token = _sign_in(client, email, PASSWORD)

    acceso = client.get("/api/compat/admin/panel-access", headers=_auth(token))
    assert acceso.status_code == 200 and acceso.json()["data"]["allowed"] is False

    # Endpoints administrativos cerrados.
    manual = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(token),
        json={"email": f"x-{uuid.uuid4().hex[:6]}@t.com", "password": PASSWORD},
    )
    assert manual.status_code == 403, manual.text

    borrar = client.delete(f"/api/compat/auth/admin/users/{uuid.uuid4()}", headers=_auth(token))
    assert borrar.status_code == 403, borrar.text

    lotes = client.get("/api/compat/admin/parcels/users", headers=_auth(token))
    assert lotes.status_code == 403, lotes.text

    contexto_lotes = client.get("/api/compat/admin/parcels/context", headers=_auth(token))
    assert contexto_lotes.status_code == 200 and contexto_lotes.json()["data"]["allowed"] is False

    # Escrituras a tablas privilegiadas cerradas.
    fila_admin = client.post(
        "/api/compat/tables/admin_users/insert",
        headers=_auth(token),
        json={"data": {"user_id": str(uuid.uuid4()), "admin_role": "company_admin"}},
    )
    assert fila_admin.status_code == 403, fila_admin.text

    empresa = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(token),
        json={"data": {"name": "Intrusa"}},
    )
    assert empresa.status_code == 403, empresa.text

    modulos = client.post(
        "/api/compat/tables/user_modules/insert",
        headers=_auth(token),
        json={"data": {"user_id": str(uuid.uuid4()), "module_id": "satelite"}},
    )
    assert modulos.status_code == 403, modulos.text

    # Revisión de extensiones cerrada.
    revisar = client.post(
        f"/api/compat/extensions/requests/{uuid.uuid4()}/reject",
        headers=_auth(token),
        json={},
    )
    assert revisar.status_code == 403, revisar.text


def test_comercial_no_listado_no_da_de_alta_clientes(client: TestClient, admin_token: str):
    email = f"comercial-{uuid.uuid4().hex[:8]}@cliente-final.com"
    _create_user(client, admin_token, email=email)
    _set_admin_flag(email, compat.CLIENT_ONBOARDER_FIELD, True)
    token = _sign_in(client, email, PASSWORD)

    contexto = client.get("/api/compat/admin/clients/context", headers=_auth(token))
    assert contexto.status_code == 200 and contexto.json()["data"]["allowed"] is False

    alta = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(token),
        json={"company_name": "Fuera", "email": f"y-{uuid.uuid4().hex[:6]}@t.com", "password": PASSWORD},
    )
    assert alta.status_code == 403, alta.text


def test_la_variable_de_entorno_amplia_la_lista(client: TestClient, admin_token: str):
    email = f"listado-{uuid.uuid4().hex[:8]}@cliente-final.com"
    _create_user(client, admin_token, email=email, admin_role="company_admin")
    _set_admin_flag(email, "admin_role", "company_admin")
    token = _sign_in(client, email, PASSWORD)

    denegado = client.get("/api/compat/admin/panel-access", headers=_auth(token))
    assert denegado.json()["data"]["allowed"] is False

    os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = f"{compat.DEFAULT_ADMIN_PANEL_EMAILS},{email}"
    permitido = client.get("/api/compat/admin/panel-access", headers=_auth(token))
    assert permitido.json()["data"]["allowed"] is True
