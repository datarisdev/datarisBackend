"""Panel de módulos: catálogo fijo y accesos por empresa y por usuario.

Prueba de punta a punta la vía que usa /admin/modules: el catálogo del producto
no se puede inventar ni borrar desde el panel, el paquete de la empresa se fija
en un sitio, y los overrides por usuario se guardan de forma que el propio
cálculo de /me/access los respeta (era justo lo que fallaba: se guardaba en un
sitio y se leía de otro).
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-module-access-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Modulos2026!"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist():
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


@pytest.fixture(scope="module")
def cliente(client: TestClient, admin_token: str) -> dict:
    """Empresa con Satélite y Telemetría, y un usuario suyo."""
    response = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(admin_token),
        json={
            "company_name": f"Agro {uuid.uuid4().hex[:6]}",
            "email": f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com",
            "password": PASSWORD,
            "modules": ["satelite", "telemetria"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return {"company_id": data["company"]["id"], "user_id": data["user"]["id"], "email": data["user"]["email"]}


def _module_ids(client: TestClient, token: str) -> list:
    response = client.get("/api/me/access", headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["data"]["moduleIds"]


# --- Catálogo ---------------------------------------------------------------


def test_el_catalogo_dice_donde_se_ve_cada_modulo(client: TestClient, admin_token: str):
    response = client.get("/api/compat/admin/module-access/catalog", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    modules = {m["id"]: m for m in response.json()["data"]["modules"]}

    assert modules["dashboard"]["is_system"] is True
    assert modules["satelite"]["surface"] == "menu"
    # Mapeo existe y tiene ruta, pero no tiene entrada propia en el menú: el
    # panel debe decirlo para que activarlo y "no verlo" no parezca un fallo.
    assert modules["mapeo"]["surface"] == "embedded"
    assert modules["mapeo"]["surface_hint"]
    assert modules["digiforms"]["category"] == "extension"
    assert modules["ml-training"]["is_internal"] is True


def test_no_se_pueden_inventar_ni_borrar_modulos(client: TestClient, admin_token: str):
    creado = client.post(
        "/api/compat/tables/platform_modules/insert",
        headers=_auth(admin_token),
        json={"data": {"name": "modulo_fantasma", "is_active": True}},
    )
    assert creado.status_code == 400, creado.text

    borrado = client.post(
        "/api/compat/tables/platform_modules/delete",
        headers=_auth(admin_token),
        json={"filters": [{"column": "id", "op": "eq", "value": "satelite"}]},
    )
    assert borrado.status_code == 400, borrado.text


def test_el_dashboard_no_se_puede_desactivar(client: TestClient, admin_token: str):
    response = client.patch(
        "/api/compat/admin/module-access/catalog/dashboard",
        headers=_auth(admin_token),
        json={"is_active": False},
    )
    assert response.status_code == 400, response.text


# --- Accesos por usuario ----------------------------------------------------


def test_el_detalle_distingue_heredado_de_decision_propia(client: TestClient, admin_token: str, cliente: dict):
    response = client.get(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(admin_token),
    )
    assert response.status_code == 200, response.text
    modules = {m["id"]: m for m in response.json()["data"]["modules"]}

    assert modules["satelite"]["company_enabled"] is True
    assert modules["satelite"]["effective"] is True
    assert modules["personal"]["company_enabled"] is False
    assert modules["personal"]["effective"] is False


def test_quitar_un_modulo_por_usuario_surte_efecto(client: TestClient, admin_token: str, cliente: dict):
    token = _sign_in(client, cliente["email"], PASSWORD)
    assert "telemetria" in _module_ids(client, token)

    response = client.put(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"telemetria": False}},
    )
    assert response.status_code == 200, response.text

    ids = _module_ids(client, token)
    assert "telemetria" not in ids
    assert "satelite" in ids


def test_apagar_todo_no_devuelve_el_paquete_de_la_empresa(client: TestClient, admin_token: str, cliente: dict):
    token = _sign_in(client, cliente["email"], PASSWORD)
    response = client.put(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"satelite": False, "telemetria": False}},
    )
    assert response.status_code == 200, response.text
    assert _module_ids(client, token) == ["dashboard"]


def test_devolver_el_modulo_a_heredado(client: TestClient, admin_token: str, cliente: dict):
    token = _sign_in(client, cliente["email"], PASSWORD)
    response = client.put(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"satelite": None, "telemetria": None}},
    )
    assert response.status_code == 200, response.text
    ids = _module_ids(client, token)
    assert {"satelite", "telemetria"} <= set(ids)


def test_no_se_concede_por_usuario_lo_que_la_empresa_no_tiene(client: TestClient, admin_token: str, cliente: dict):
    response = client.put(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"personal": True}},
    )
    assert response.status_code == 400, response.text
    assert "paquete de la empresa" in response.json()["detail"]


# --- Paquete de la empresa --------------------------------------------------


def test_cambiar_el_paquete_de_la_empresa_alcanza_a_sus_usuarios(client: TestClient, admin_token: str, cliente: dict):
    token = _sign_in(client, cliente["email"], PASSWORD)
    response = client.put(
        f"/api/compat/admin/module-access/companies/{cliente['company_id']}",
        headers=_auth(admin_token),
        json={"modules": {"personal": True}},
    )
    assert response.status_code == 200, response.text
    assert "personal" in _module_ids(client, token)

    apagado = client.put(
        f"/api/compat/admin/module-access/companies/{cliente['company_id']}",
        headers=_auth(admin_token),
        json={"modules": {"personal": False}},
    )
    assert apagado.status_code == 200, apagado.text
    assert "personal" not in _module_ids(client, token)


def test_los_usuarios_listados_incluyen_a_los_que_no_son_admin(client: TestClient, admin_token: str):
    email = f"registro-{uuid.uuid4().hex[:8]}@dataris-test.com"
    alta = client.post("/api/compat/auth/sign-up", json={"email": email, "password": PASSWORD})
    assert alta.status_code == 200, alta.text

    response = client.get(
        "/api/compat/admin/module-access/users",
        headers=_auth(admin_token),
        params={"search": email},
    )
    assert response.status_code == 200, response.text
    users = response.json()["data"]["users"]
    assert [u["email"] for u in users] == [email]
    assert users[0]["admin_role"] is None


def test_un_usuario_normal_no_puede_tocar_los_accesos(client: TestClient, admin_token: str, cliente: dict):
    email = f"curioso-{uuid.uuid4().hex[:8]}@dataris-test.com"
    client.post("/api/compat/auth/sign-up", json={"email": email, "password": PASSWORD})
    token = _sign_in(client, email, PASSWORD)

    response = client.put(
        f"/api/compat/admin/module-access/users/{cliente['user_id']}",
        headers=_auth(token),
        json={"overrides": {"satelite": True}},
    )
    assert response.status_code == 403, response.text


# --- El caso real reportado: un usuario dado de alta desde el panel ----------


@pytest.fixture(scope="module")
def usuario_de_alta_manual(client: TestClient, admin_token: str) -> dict:
    """Empresa con paquete amplio y un usuario suyo creado por el alta manual.

    Es la vía por la que están creados los usuarios reales (dgarcia y compañía),
    y la que dejaba filas positivas en `user_modules` para todo lo concedido: el
    escenario donde antes el panel se pintaba mal y las desactivaciones no
    llegaban a la plataforma.
    """
    empresa = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(admin_token),
        json={
            "company_name": f"Summagro {uuid.uuid4().hex[:6]}",
            "email": f"admin-{uuid.uuid4().hex[:8]}@dataris-test.com",
            "password": PASSWORD,
            "modules": ["satelite", "telemetria", "personal", "sig-agricola"],
        },
    )
    assert empresa.status_code == 200, empresa.text
    company_id = empresa.json()["data"]["company"]["id"]

    email = f"dgarcia-{uuid.uuid4().hex[:8]}@dataris-test.com"
    alta = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": PASSWORD,
            "first_name": "Diego",
            "last_name": "García",
            "admin_role": "company_user",
            "company_id": company_id,
            "modules": ["satelite", "telemetria", "personal", "sig-agricola"],
        },
    )
    assert alta.status_code == 200, alta.text
    return {
        "company_id": company_id,
        "user_id": alta.json()["data"]["user"]["id"],
        "email": email,
    }


def test_el_panel_pinta_lo_que_el_usuario_ve_de_verdad(client: TestClient, admin_token: str, usuario_de_alta_manual: dict):
    token = _sign_in(client, usuario_de_alta_manual["email"], PASSWORD)
    vistos = set(_module_ids(client, token))

    response = client.get(
        f"/api/compat/admin/module-access/users/{usuario_de_alta_manual['user_id']}",
        headers=_auth(admin_token),
    )
    assert response.status_code == 200, response.text
    modules = response.json()["data"]["modules"]

    encendidos = {m["id"] for m in modules if m["effective"]}
    apagados = {m["id"] for m in modules if not m["effective"] and m["assignable"]}
    assert encendidos <= vistos, "el panel enseña encendido algo que el usuario no tiene"
    assert not (apagados & vistos), "el panel enseña apagado algo que el usuario sí tiene"


def test_apagar_por_usuario_le_quita_el_modulo_de_verdad(client: TestClient, admin_token: str, usuario_de_alta_manual: dict):
    token = _sign_in(client, usuario_de_alta_manual["email"], PASSWORD)
    assert {"telemetria", "personal"} <= set(_module_ids(client, token))

    response = client.put(
        f"/api/compat/admin/module-access/users/{usuario_de_alta_manual['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"telemetria": False, "personal": False}},
    )
    assert response.status_code == 200, response.text

    ids = set(_module_ids(client, token))
    assert "telemetria" not in ids and "personal" not in ids
    assert {"satelite", "sig-agricola"} <= ids

    # Y devolverlos también surte efecto, sin tocar el resto.
    vuelta = client.put(
        f"/api/compat/admin/module-access/users/{usuario_de_alta_manual['user_id']}",
        headers=_auth(admin_token),
        json={"overrides": {"telemetria": True, "personal": None}},
    )
    assert vuelta.status_code == 200, vuelta.text
    assert {"satelite", "telemetria", "personal", "sig-agricola"} <= set(_module_ids(client, token))


def test_quitar_el_modulo_a_la_empresa_alcanza_al_usuario_con_filas_propias(
    client: TestClient, admin_token: str, usuario_de_alta_manual: dict
):
    """Aunque el usuario tenga filas propias, el paquete de la empresa es el techo."""
    token = _sign_in(client, usuario_de_alta_manual["email"], PASSWORD)
    assert "sig-agricola" in _module_ids(client, token)

    apagado = client.put(
        f"/api/compat/admin/module-access/companies/{usuario_de_alta_manual['company_id']}",
        headers=_auth(admin_token),
        json={"modules": {"sig-agricola": False}},
    )
    assert apagado.status_code == 200, apagado.text
    assert "sig-agricola" not in _module_ids(client, token)

    encendido = client.put(
        f"/api/compat/admin/module-access/companies/{usuario_de_alta_manual['company_id']}",
        headers=_auth(admin_token),
        json={"modules": {"sig-agricola": True}},
    )
    assert encendido.status_code == 200, encendido.text
    assert "sig-agricola" in _module_ids(client, token)
