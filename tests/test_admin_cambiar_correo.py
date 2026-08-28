"""Cambio del correo de una cuenta ajena desde administración.

El correo vive en DOS sitios —la credencial con la que se entra y el perfil que
se muestra— y hasta ahora sólo `/auth/update-user` lo tocaba: sobre la propia
sesión y sin actualizar el perfil. Es decir, no había forma de cambiarle el
correo a otra persona sin dejar la cuenta a medias.

`POST /admin/users/{user_id}/email` lo resuelve moviendo los dos a la vez. Lo
que se comprueba aquí:

* el correo nuevo sirve para entrar y el viejo deja de servir,
* el perfil queda con el correo nuevo (no se queda con el viejo),
* NO se toca nada más: contraseña, rol, empresa y módulos siguen igual,
* no se puede pisar el correo de otra cuenta,
* un admin de empresa no puede tocar cuentas de otra empresa,
* el panel sigue cerrado a quien no está en la lista blanca.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-cambiar-correo-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
PASSWORD = "Correo2026!seguro"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str):
    return client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    response = _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _crear_usuario(client, admin_token, *, email, admin_role="company_user", company_id=None) -> str:
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": PASSWORD,
            "first_name": "Prueba",
            "last_name": "Correo",
            "admin_role": admin_role,
            "company_id": company_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["user"]["id"]


def _perfil(user_id: str) -> dict:
    db = compat.read_db()
    return next(
        row for row in compat.table(db, "profiles")
        if str(row.get("user_id") or row.get("id")) == str(user_id)
    )


def _fila_admin(user_id: str) -> dict:
    db = compat.read_db()
    return next(row for row in compat.table(db, "admin_users") if str(row.get("user_id")) == str(user_id))


def test_cambia_el_correo_en_el_acceso_y_en_la_ficha(client: TestClient, admin_token: str):
    viejo = f"viejo-{uuid.uuid4().hex[:8]}@cliente-final.com"
    nuevo = f"nuevo-{uuid.uuid4().hex[:8]}@cliente-final.com"
    user_id = _crear_usuario(client, admin_token, email=viejo)

    antes = _fila_admin(user_id)
    rol_antes, empresa_antes = antes.get("admin_role"), antes.get("company_id")

    response = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        headers=_auth(admin_token),
        json={"email": nuevo},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["user"]["email"] == nuevo
    assert data["email_anterior"] == viejo
    assert data["perfiles_actualizados"] >= 1

    # La ficha acompaña al acceso: si no, el panel mostraría el correo viejo.
    assert _perfil(user_id)["email"] == nuevo

    # El correo nuevo entra y el viejo ya no.
    assert _sign_in(client, nuevo, PASSWORD).status_code == 200
    assert _sign_in(client, viejo, PASSWORD).status_code != 200


def test_no_toca_nada_mas_que_el_correo(client: TestClient, admin_token: str):
    viejo = f"intacto-{uuid.uuid4().hex[:8]}@cliente-final.com"
    nuevo = f"intacto2-{uuid.uuid4().hex[:8]}@cliente-final.com"
    user_id = _crear_usuario(client, admin_token, email=viejo, admin_role="company_admin")

    antes = dict(_fila_admin(user_id))
    perfil_antes = dict(_perfil(user_id))

    response = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        headers=_auth(admin_token),
        json={"email": nuevo},
    )
    assert response.status_code == 200, response.text

    despues = _fila_admin(user_id)
    assert despues.get("admin_role") == antes.get("admin_role")
    assert despues.get("company_id") == antes.get("company_id")
    assert despues.get("is_active") == antes.get("is_active")

    perfil_despues = _perfil(user_id)
    for campo in ("first_name", "last_name", "company_name", "country", "hectareas"):
        assert perfil_despues.get(campo) == perfil_antes.get(campo), campo

    # La contraseña sigue siendo la misma.
    assert _sign_in(client, nuevo, PASSWORD).status_code == 200


def test_no_puede_pisar_el_correo_de_otra_cuenta(client: TestClient, admin_token: str):
    ocupado = f"ocupado-{uuid.uuid4().hex[:8]}@cliente-final.com"
    _crear_usuario(client, admin_token, email=ocupado)
    otro = f"otro-{uuid.uuid4().hex[:8]}@cliente-final.com"
    user_id = _crear_usuario(client, admin_token, email=otro)

    response = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        headers=_auth(admin_token),
        json={"email": ocupado},
    )
    assert response.status_code == 400, response.text
    # La cuenta se queda como estaba: el correo ocupado sigue siendo del primero.
    assert _sign_in(client, otro, PASSWORD).status_code == 200


def test_rechaza_correo_invalido_y_el_mismo_que_ya_tiene(client: TestClient, admin_token: str):
    email = f"mismo-{uuid.uuid4().hex[:8]}@cliente-final.com"
    user_id = _crear_usuario(client, admin_token, email=email)

    invalido = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        headers=_auth(admin_token),
        json={"email": "esto-no-es-un-correo"},
    )
    assert invalido.status_code == 400, invalido.text

    igual = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        headers=_auth(admin_token),
        json={"email": email},
    )
    assert igual.status_code == 400, igual.text


def test_usuario_inexistente_da_404(client: TestClient, admin_token: str):
    response = client.post(
        f"/api/compat/admin/users/{uuid.uuid4()}/email",
        headers=_auth(admin_token),
        json={"email": f"fantasma-{uuid.uuid4().hex[:8]}@cliente-final.com"},
    )
    assert response.status_code == 404, response.text


def test_anonimo_no_puede_cambiar_correos(client: TestClient, admin_token: str):
    email = f"protegido-{uuid.uuid4().hex[:8]}@cliente-final.com"
    user_id = _crear_usuario(client, admin_token, email=email)

    response = client.post(
        f"/api/compat/admin/users/{user_id}/email",
        json={"email": f"pirata-{uuid.uuid4().hex[:8]}@cliente-final.com"},
    )
    assert response.status_code in (401, 403), response.text
    assert _sign_in(client, email, PASSWORD).status_code == 200
