"""Alta de clientes: la única vía es el onboarding, y la usa Gestión de Empresas.

La pantalla «Empresas» del panel hacía su propia alta a mano (insert en
`companies` + alta pública + insert en `admin_users`). Eso dejaba clientes a
medio configurar y podía romperse por la mitad, así que ahora su botón «Nueva
Empresa» llama a este endpoint. Lo que se comprueba aquí:

* la empresa guarda el correo y el CIF que teclea el panel, para que su tabla no
  salga con la columna vacía (antes el onboarding no los escribía),
* el administrador nace completo: rol `admin`, perfil ligado a su empresa, país
  y contraseña temporal,
* el paquete de módulos que manda el panel se respeta tal cual, incluso vacío, y
  sin lista se cae al paquete por defecto,
* sin permiso del panel no se da de alta a nadie.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-alta-empresas-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Temporal2026!"


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


def _sign_in(client: TestClient, email: str, password: str):
    return client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    response = _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _onboard(client: TestClient, token: str, **campos):
    payload = {
        "company_name": f"Agrícola {uuid.uuid4().hex[:6]}",
        "email": f"admin-{uuid.uuid4().hex[:8]}@cliente-test.com",
        "password": PASSWORD,
        **campos,
    }
    return client.post("/api/compat/admin/clients/onboard", headers=_auth(token), json=payload), payload


# --- La empresa queda completa para la tabla de Gestión de Empresas ---------


def test_la_empresa_guarda_correo_cif_hectareas_y_estado(client: TestClient, admin_token: str):
    response, enviado = _onboard(
        client,
        admin_token,
        cif="B12345678",
        max_hectares=1500,
        country="MX",
    )
    assert response.status_code == 200, response.text
    company = response.json()["data"]["company"]

    assert company["name"] == enviado["company_name"]
    assert company["cif"] == "B12345678"
    # Sin `company_email` la empresa hereda el correo de su administrador, que
    # es justo lo que la tabla del panel pinta en la columna Email.
    assert company["email"] == enviado["email"]
    assert company["max_hectares"] == 1500
    assert company["is_active"] is True

    listado = client.post("/api/compat/tables/companies/query", headers=_auth(admin_token), json={})
    assert listado.status_code == 200, listado.text
    fila = next(c for c in listado.json()["data"] if c["id"] == company["id"])
    assert fila["email"] == enviado["email"]
    assert fila["cif"] == "B12345678"


def test_el_correo_de_la_empresa_puede_ir_aparte_del_administrador(client: TestClient, admin_token: str):
    response, enviado = _onboard(client, admin_token, company_email="facturacion@cliente-test.com")
    assert response.status_code == 200, response.text
    company = response.json()["data"]["company"]
    assert company["email"] == "facturacion@cliente-test.com"
    assert response.json()["data"]["user"]["email"] == enviado["email"]


def test_la_empresa_puede_nacer_inactiva(client: TestClient, admin_token: str):
    response, _ = _onboard(client, admin_token, is_active=False)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["company"]["is_active"] is False


# --- El administrador del cliente nace completo -----------------------------


def test_el_administrador_nace_con_rol_perfil_pais_y_clave_temporal(client: TestClient, admin_token: str):
    response, enviado = _onboard(
        client,
        admin_token,
        country="GT",
        first_name="María",
        last_name="González",
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    company_id = data["company"]["id"]
    user_id = data["user"]["id"]

    db = compat.read_db()
    roles = [r["role"] for r in compat.table(db, "user_roles") if r["user_id"] == user_id]
    assert roles == ["admin"], "el alta pública dejaba el rol en 'user'"

    perfil = next(p for p in compat.table(db, "profiles") if p["user_id"] == user_id)
    assert perfil["company_id"] == company_id, "el perfil tiene que quedar ligado a su empresa"
    assert perfil["country"] == "GT"
    assert perfil["first_name"] == "María"

    admin_row = next(a for a in compat.table(db, "admin_users") if a["user_id"] == user_id)
    assert admin_row["admin_role"] == "company_admin"
    assert admin_row["company_id"] == company_id

    sesion = _sign_in(client, enviado["email"], PASSWORD)
    assert sesion.status_code == 200, sesion.text
    assert sesion.json()["data"]["user"]["must_change_password"] is True


# --- El paquete de módulos --------------------------------------------------


def test_el_paquete_que_manda_el_panel_se_respeta_tal_cual(client: TestClient, admin_token: str):
    response, _ = _onboard(client, admin_token, modules=["satelite", "telemetria"])
    assert response.status_code == 200, response.text
    assert sorted(response.json()["data"]["modules"]) == ["satelite", "telemetria"]


def test_una_lista_vacia_significa_sin_modulos_no_el_paquete_por_defecto(client: TestClient, admin_token: str):
    response, _ = _onboard(client, admin_token, modules=[])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["modules"] == []


def test_sin_lista_de_modulos_se_aplica_el_paquete_por_defecto(client: TestClient, admin_token: str):
    response, _ = _onboard(client, admin_token)
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]["modules"]) == set(compat.CLIENT_DEFAULT_MODULE_IDS)


# --- El candado sigue puesto ------------------------------------------------


def test_sin_permiso_del_panel_no_se_da_de_alta_a_nadie(client: TestClient, admin_token: str):
    creado, enviado = _onboard(client, admin_token)
    assert creado.status_code == 200, creado.text
    # El administrador del cliente recién creado no está en la lista blanca del
    # panel, así que no puede crear clientes a su vez.
    client.post(
        "/api/compat/auth/change-password",
        headers=_auth(_sign_in(client, enviado["email"], PASSWORD).json()["data"]["session"]["access_token"]),
        json={"new_password": "ClaveDelCliente2026!"},
    )
    token_cliente = _sign_in(client, enviado["email"], "ClaveDelCliente2026!").json()["data"]["session"]["access_token"]

    rechazado, _ = _onboard(client, token_cliente)
    assert rechazado.status_code == 403, rechazado.text
