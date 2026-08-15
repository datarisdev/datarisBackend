"""Reglas de alerta de los reportes del SIG: validación, reemplazo y aislamiento.

El cliente guarda qué respuestas de sus formularios disparan alerta y con qué
prioridad; el frontend evalúa las reglas sobre los report-records. Aquí se
prueba el contrato HTTP: el PUT reemplaza el conjunto completo de la empresa,
valida condiciones y prioridades, y ninguna empresa ve reglas ajenas.

Se ejecuta con un almacén compat vacío para partir del `default_db()` sembrado,
igual que el resto de smoke tests del sistema de compatibilidad.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-alert-rules-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers.compat import LOCK, read_db, table, write_db  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}

RULES_URL = "/api/compat/sig-agricola/report-alert-rules"


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Sin el gestor de contexto a propósito: entrar en él dispara el arranque de
    # la aplicación, que abre una conexión SQLAlchemy a Postgres. El sistema de
    # compatibilidad que se prueba aquí no la usa (ver
    # [[project-dataris-arquitectura-usuarios]]).
    return TestClient(app)


@pytest.fixture(scope="module")
def superadmin_token(client: TestClient) -> str:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _regla(**overrides) -> dict:
    base = {
        "template_key": "visita-campo",
        "field": "plaga_detectada",
        "condition": "contains",
        "value": "sí",
        "priority": "alta",
        "label": "Plaga reportada",
    }
    base.update(overrides)
    return base


def test_sin_reglas_el_listado_llega_vacio(client, superadmin_token):
    response = client.get(RULES_URL, headers=_auth(superadmin_token))
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


def test_guardar_y_listar_ordena_por_prioridad(client, superadmin_token):
    payload = {
        "rules": [
            _regla(field="observaciones", condition="not_empty", value="", priority="baja"),
            _regla(),
            _regla(field="severidad", condition="gt", value="3", priority="media"),
        ]
    }
    response = client.put(RULES_URL, headers=_auth(superadmin_token), json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 3

    listing = client.get(RULES_URL, headers=_auth(superadmin_token))
    assert listing.status_code == 200
    priorities = [row["priority"] for row in listing.json()["data"]]
    assert priorities == ["alta", "media", "baja"]


def test_prioridad_desconocida_es_rechazada(client, superadmin_token):
    response = client.put(
        RULES_URL,
        headers=_auth(superadmin_token),
        json={"rules": [_regla(priority="urgentisima")]},
    )
    assert response.status_code == 400
    assert "prioridad" in response.json()["detail"].lower()


def test_condicion_numerica_exige_numero(client, superadmin_token):
    response = client.put(
        RULES_URL,
        headers=_auth(superadmin_token),
        json={"rules": [_regla(condition="gt", value="mucho")]},
    )
    assert response.status_code == 400
    assert "numérico" in response.json()["detail"]


def test_regla_sin_campo_es_rechazada(client, superadmin_token):
    response = client.put(
        RULES_URL,
        headers=_auth(superadmin_token),
        json={"rules": [_regla(field="")]},
    )
    assert response.status_code == 400
    assert "field" in response.json()["detail"]


def test_el_put_reemplaza_el_conjunto_y_conserva_created_at(client, superadmin_token):
    primera = client.put(
        RULES_URL,
        headers=_auth(superadmin_token),
        json={"rules": [_regla(), _regla(field="otra_cosa", condition="not_empty", value="", priority="baja")]},
    )
    assert primera.status_code == 200
    guardadas = primera.json()["data"]
    conservada = guardadas[0]

    segunda = client.put(
        RULES_URL,
        headers=_auth(superadmin_token),
        json={"rules": [dict(_regla(priority="media"), id=conservada["id"])]},
    )
    assert segunda.status_code == 200
    restantes = segunda.json()["data"]
    assert len(restantes) == 1
    assert restantes[0]["id"] == conservada["id"]
    assert restantes[0]["created_at"] == conservada["created_at"]
    assert restantes[0]["priority"] == "media"


def test_las_reglas_de_otra_empresa_no_se_ven_ni_se_pisan(client, superadmin_token):
    ajena = {
        "id": "regla-ajena",
        "user_id": "usuario-ajeno",
        "company_id": "otra-empresa-cualquiera",
        "template_key": "",
        "field": "campo",
        "condition": "not_empty",
        "value": "",
        "priority": "alta",
        "label": "",
        "enabled": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    with LOCK:
        db = read_db()
        table(db, "sig_report_alert_rules").append(dict(ajena))
        write_db(db)

    listing = client.get(RULES_URL, headers=_auth(superadmin_token))
    assert all(row["id"] != "regla-ajena" for row in listing.json()["data"])

    client.put(RULES_URL, headers=_auth(superadmin_token), json={"rules": []})
    with LOCK:
        db = read_db()
        sobrevive = any(row.get("id") == "regla-ajena" for row in table(db, "sig_report_alert_rules"))
    assert sobrevive
