"""Contraseña temporal: cambio obligatorio en el primer acceso.

Las cuentas que crea un administrador nacen con una contraseña que él teclea y
entrega por correo o por WhatsApp. Hasta ahora esa contraseña se quedaba puesta
para siempre. Lo que se comprueba aquí:

* la cuenta recién creada viene marcada (`must_change_password`) en el alta
  manual y en el onboarding de clientes, y la marca viaja en el sign-in,
* con la marca puesta NO se pide la contraseña actual (el primer acceso no debe
  obligar a teclear dos veces la temporal),
* cambiarla retira la marca, la nueva sirve para entrar y la temporal deja de
  servir,
* un cambio voluntario (sin marca) SÍ exige la contraseña actual y la rechaza si
  no es correcta,
* no se acepta repetir la contraseña que ya se tenía ni uná más corta del mínimo,
* un administrador puede asignar una temporal a una cuenta ajena y eso la vuelve
  a marcar, pero no a cuentas de otra empresa.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-password-temporal-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
TEMPORAL = "Temporal2026!"
NUEVA = "MiPropiaClave2026!"


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


def _crear_empresa(client, admin_token, nombre: str) -> str:
    response = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(admin_token),
        json={"data": {"name": nombre, "max_hectares": 10000}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _crear_usuario(client, admin_token, *, email=None, company_id=None, admin_role="company_user", **extra):
    email = email or f"temporal-{uuid.uuid4().hex[:8]}@dataris-test.com"
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": TEMPORAL,
            "first_name": "Prueba",
            "last_name": "Temporal",
            "admin_role": admin_role,
            "company_id": company_id,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return email, response.json()["data"]


# --- La cuenta nace marcada -------------------------------------------------


def test_alta_manual_marca_la_cuenta_y_el_sign_in_lo_dice(client: TestClient, admin_token: str):
    email, data = _crear_usuario(client, admin_token)
    assert data["user"]["must_change_password"] is True
    assert data["require_password_change"] is True

    sesion = _sign_in(client, email, TEMPORAL)
    assert sesion.status_code == 200, sesion.text
    assert sesion.json()["data"]["user"]["must_change_password"] is True


def test_el_administrador_puede_no_exigir_el_cambio(client: TestClient, admin_token: str):
    email, data = _crear_usuario(client, admin_token, require_password_change=False)
    assert data["user"]["must_change_password"] is False
    assert _sign_in(client, email, TEMPORAL).json()["data"]["user"]["must_change_password"] is False


def test_onboarding_de_cliente_tambien_marca_al_administrador(client: TestClient, admin_token: str):
    email = f"cliente-{uuid.uuid4().hex[:8]}@dataris-test.com"
    response = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(admin_token),
        json={
            "company_name": f"Cliente {uuid.uuid4().hex[:6]}",
            "email": email,
            "password": TEMPORAL,
            "first_name": "Cliente",
            "last_name": "Nuevo",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user"]["must_change_password"] is True


# --- El cambio en el primer acceso -----------------------------------------


def test_el_primer_acceso_cambia_sin_pedir_la_actual_y_retira_la_marca(client: TestClient, admin_token: str):
    email, _ = _crear_usuario(client, admin_token)
    token = _sign_in(client, email, TEMPORAL).json()["data"]["session"]["access_token"]

    response = client.post(
        "/api/compat/auth/change-password",
        headers=_auth(token),
        json={"new_password": NUEVA},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user"]["must_change_password"] is False

    # La nueva entra; la temporal ya no.
    con_nueva = _sign_in(client, email, NUEVA)
    assert con_nueva.status_code == 200, con_nueva.text
    assert con_nueva.json()["data"]["user"]["must_change_password"] is False
    assert _sign_in(client, email, TEMPORAL).status_code == 401


def test_la_nueva_no_puede_ser_la_misma_ni_mas_corta_del_minimo(client: TestClient, admin_token: str):
    email, _ = _crear_usuario(client, admin_token)
    token = _sign_in(client, email, TEMPORAL).json()["data"]["session"]["access_token"]

    corta = client.post("/api/compat/auth/change-password", headers=_auth(token), json={"new_password": "corta1"})
    assert corta.status_code == 400

    igual = client.post("/api/compat/auth/change-password", headers=_auth(token), json={"new_password": TEMPORAL})
    assert igual.status_code == 400

    # Sigue marcada: ninguno de los dos intentos la dio por buena.
    assert _sign_in(client, email, TEMPORAL).json()["data"]["user"]["must_change_password"] is True


def test_sin_sesion_no_se_cambia_ninguna_contrasena(client: TestClient):
    response = client.post("/api/compat/auth/change-password", json={"new_password": NUEVA})
    assert response.status_code == 401


# --- El cambio voluntario, ya sin marca ------------------------------------


def test_el_cambio_voluntario_exige_la_contrasena_actual(client: TestClient, admin_token: str):
    email, _ = _crear_usuario(client, admin_token, require_password_change=False)
    token = _sign_in(client, email, TEMPORAL).json()["data"]["session"]["access_token"]

    sin_actual = client.post("/api/compat/auth/change-password", headers=_auth(token), json={"new_password": NUEVA})
    assert sin_actual.status_code == 400

    equivocada = client.post(
        "/api/compat/auth/change-password",
        headers=_auth(token),
        json={"current_password": "loQueSea2026!", "new_password": NUEVA},
    )
    assert equivocada.status_code == 400

    correcta = client.post(
        "/api/compat/auth/change-password",
        headers=_auth(token),
        json={"current_password": TEMPORAL, "new_password": NUEVA},
    )
    assert correcta.status_code == 200, correcta.text
    assert _sign_in(client, email, NUEVA).status_code == 200


def test_cambiar_la_contrasena_por_update_user_tambien_retira_la_marca(client: TestClient, admin_token: str):
    email, _ = _crear_usuario(client, admin_token)
    token = _sign_in(client, email, TEMPORAL).json()["data"]["session"]["access_token"]

    response = client.post("/api/compat/auth/update-user", headers=_auth(token), json={"password": NUEVA})
    assert response.status_code == 200, response.text
    assert _sign_in(client, email, NUEVA).json()["data"]["user"]["must_change_password"] is False


# --- Temporal asignada por un administrador --------------------------------


def test_un_admin_asigna_temporal_a_una_cuenta_ajena_y_vuelve_a_marcarla(client: TestClient, admin_token: str):
    email, data = _crear_usuario(client, admin_token, require_password_change=False)
    user_id = data["user"]["id"]

    response = client.post(
        f"/api/compat/admin/users/{user_id}/password",
        headers=_auth(admin_token),
        json={},
    )
    assert response.status_code == 200, response.text
    asignada = response.json()["data"]["password"]
    assert len(asignada) >= compat.MIN_PASSWORD_LENGTH

    sesion = _sign_in(client, email, asignada)
    assert sesion.status_code == 200, sesion.text
    assert sesion.json()["data"]["user"]["must_change_password"] is True
    assert _sign_in(client, email, TEMPORAL).status_code == 401


def test_un_admin_de_empresa_no_toca_la_contrasena_de_otra_empresa(client: TestClient, admin_token: str):
    empresa_a = _crear_empresa(client, admin_token, f"Empresa A {uuid.uuid4().hex[:6]}")
    empresa_b = _crear_empresa(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")

    admin_a_email, _ = _crear_usuario(client, admin_token, company_id=empresa_a, admin_role="company_admin")
    _, ajena = _crear_usuario(client, admin_token, company_id=empresa_b)

    token_a = _sign_in(client, admin_a_email, TEMPORAL).json()["data"]["session"]["access_token"]
    response = client.post(
        f"/api/compat/admin/users/{ajena['user']['id']}/password",
        headers=_auth(token_a),
        json={},
    )
    assert response.status_code == 403, response.text
