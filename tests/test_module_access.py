"""Acceso a módulos: la empresa manda y el usuario sobrescribe módulo a módulo.

Cubre los fallos que se reportaron del panel de módulos:

* apagar TODOS los módulos de un usuario le devolvía el paquete entero de su
  empresa (las filas se borraban y el cálculo caía al fallback),
* aprobar una extensión creaba la primera fila del usuario y con eso le quitaba
  de golpe todo lo que heredaba de su empresa, y
* las filas escritas por `admin_user_id` y las escritas por `user_id` no se leían
  juntas, así que el panel pintaba apagado lo que el usuario sí tenía.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import me_access

COMPANY_ID = "company-1"
USER_ID = "user-heredero"
RESTRINGIDO_ID = "user-restringido"
EXTENSION_ID = "user-extension"
ADMIN_ROW_ID = "admin-row-1"
ADMIN_USER_ID = "user-con-fila-admin"


def _db():
    return {
        "users": [],
        "tables": {
            "platform_modules": [
                {"id": "dashboard", "name": "Dashboard", "is_active": True},
                {"id": "satelite", "name": "Monitoreo Satelital", "is_active": True},
                {"id": "telemetria", "name": "Telemetría", "is_active": True},
                {"id": "personal", "name": "Personal de Campo", "is_active": True},
                {"id": "digiforms", "name": "DigiformsApp", "is_active": True, "is_extension": True},
            ],
            "admin_users": [
                {"id": "admin-heredero", "user_id": USER_ID, "admin_role": "company_user", "company_id": COMPANY_ID, "is_active": True},
                {"id": "admin-restringido", "user_id": RESTRINGIDO_ID, "admin_role": "company_user", "company_id": COMPANY_ID, "is_active": True},
                {"id": "admin-extension", "user_id": EXTENSION_ID, "admin_role": "company_user", "company_id": COMPANY_ID, "is_active": True},
                {"id": ADMIN_ROW_ID, "user_id": ADMIN_USER_ID, "admin_role": "company_user", "company_id": COMPANY_ID, "is_active": True},
            ],
            "company_modules": [
                {"company_id": COMPANY_ID, "module_id": "satelite", "is_enabled": True},
                {"company_id": COMPANY_ID, "module_id": "telemetria", "is_enabled": True},
                {"company_id": COMPANY_ID, "module_id": "personal", "is_enabled": True},
            ],
            "user_modules": [
                # Restringido: le quitaron Telemetría de forma explícita.
                {"user_id": RESTRINGIDO_ID, "module_id": "telemetria", "is_enabled": False},
                # Extensión aprobada: única fila del usuario.
                {"user_id": EXTENSION_ID, "module_id": "digiforms", "is_enabled": True},
                # Fila escrita solo con admin_user_id (así las guardaba el panel).
                {"admin_user_id": ADMIN_ROW_ID, "module_id": "satelite", "is_enabled": False},
            ],
            "extension_requests": [],
        },
    }


@pytest.fixture()
def client(monkeypatch):
    fake_db = _db()
    monkeypatch.setattr(me_access, "read_db", lambda *a, **k: fake_db)
    monkeypatch.setattr(me_access, "write_db", lambda db: None)
    monkeypatch.setattr(me_access, "is_commercial_demo_user", lambda user: False)
    app = FastAPI()
    app.include_router(me_access.router, prefix="/api")
    return TestClient(app)


def _access(client, monkeypatch, user_id):
    monkeypatch.setattr(me_access, "bearer_user", lambda authorization: {"id": user_id, "email": f"{user_id}@example.com"})
    response = client.get("/api/me/access", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_usuario_sin_filas_hereda_el_paquete_de_su_empresa(client, monkeypatch):
    ids = _access(client, monkeypatch, USER_ID)["moduleIds"]
    assert {"dashboard", "satelite", "telemetria", "personal"} <= set(ids)
    assert "digiforms" not in ids


def test_override_negativo_le_quita_el_modulo_aunque_la_empresa_lo_tenga(client, monkeypatch):
    ids = _access(client, monkeypatch, RESTRINGIDO_ID)["moduleIds"]
    assert "telemetria" not in ids
    # Y conserva el resto del paquete de la empresa.
    assert {"satelite", "personal"} <= set(ids)


def test_una_extension_aprobada_no_borra_lo_heredado(client, monkeypatch):
    ids = _access(client, monkeypatch, EXTENSION_ID)["moduleIds"]
    assert "digiforms" in ids
    assert {"satelite", "telemetria", "personal"} <= set(ids)


def test_las_filas_por_admin_user_id_tambien_cuentan(client, monkeypatch):
    ids = _access(client, monkeypatch, ADMIN_USER_ID)["moduleIds"]
    assert "satelite" not in ids
    assert {"telemetria", "personal"} <= set(ids)


def test_apagar_todo_deja_al_usuario_solo_con_el_dashboard(client, monkeypatch):
    # El panel guarda un `false` explícito por módulo, no borra las filas: es lo
    # que impide que el cálculo vuelva a heredar el paquete completo.
    db = me_access.read_db()
    db["tables"]["user_modules"].extend([
        {"user_id": USER_ID, "module_id": module_id, "is_enabled": False}
        for module_id in ("satelite", "telemetria", "personal")
    ])
    ids = _access(client, monkeypatch, USER_ID)["moduleIds"]
    assert ids == ["dashboard"]


def test_modulo_desactivado_en_la_plataforma_no_llega_a_nadie(client, monkeypatch):
    db = me_access.read_db()
    for row in db["tables"]["platform_modules"]:
        if row["id"] == "personal":
            row["is_active"] = False
    ids = _access(client, monkeypatch, USER_ID)["moduleIds"]
    assert "personal" not in ids
