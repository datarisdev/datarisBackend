"""Graniot y Monitoreo Satelital son un único módulo.

Eran dos filas del catálogo para la misma capacidad —las capas satelitales de
los lotes—, así que el cliente veía dos cosas donde solo había una y quien
repartía accesos tenía que adivinar cuál activar. Aquí se comprueba que quedó
una sola: `graniot` resuelve a `satelite`, ya no se puede solicitar como
extensión y los accesos antiguos siguen valiendo tras la migración.

El archivo sustituye a `test_extension_requests_graniot.py`, que cubría a
Graniot como extensión propia; lo que sigue vivo de aquello (una solicitud sin
módulo se rechaza, y la solicitud respeta el módulo elegido) se mantiene abajo
con DigiformsApp, que es la única extensión que queda.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-graniot-unificado-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402
from app.services import module_catalog  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client: TestClient, email: str, password: str) -> str:
    r = client.post("/api/compat/auth/sign-in", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["session"]["access_token"]


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    return _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])


def _new_user(client, admin_token, email):
    r = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={"email": email, "password": "Extension2026!", "first_name": "Ext", "last_name": "User"},
    )
    assert r.status_code == 200, r.text


# --- El catálogo ---------------------------------------------------------


def test_graniot_resuelve_a_monitoreo_satelital():
    assert module_catalog.canonical_module_id("graniot") == "satelite"
    assert "graniot" not in module_catalog.SPECS_BY_ID
    assert [spec.id for spec in module_catalog.extension_specs()] == ["digiforms"]


def test_graniot_ya_no_es_una_fila_del_catalogo(client: TestClient):
    db = compat.read_db(force_refresh=True)
    assert not [m for m in compat.table(db, "platform_modules") if m.get("id") == "graniot"]


def test_graniot_ya_no_se_puede_solicitar_como_extension(client: TestClient, admin_token: str):
    email = f"cliente-graniot-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _new_user(client, admin_token, email)
    token = _sign_in(client, email, "Extension2026!")

    r = client.post(
        "/api/compat/extensions/requests",
        headers=_auth(token),
        json={"extension_id": "graniot", "contact_notes": "Quiero activar Graniot"},
    )
    assert r.status_code == 400, r.text


# --- Lo que sigue vivo del circuito de extensiones -----------------------


def test_la_solicitud_respeta_el_modulo_elegido(client: TestClient, admin_token: str):
    email = f"cliente-digiforms-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _new_user(client, admin_token, email)
    token = _sign_in(client, email, "Extension2026!")

    r = client.post(
        "/api/compat/extensions/requests",
        headers=_auth(token),
        json={"extension_id": "digiforms", "contact_notes": "Quiero formularios"},
    )
    assert r.status_code == 200, r.text
    request = r.json()["data"]["request"]
    assert request["extension_id"] == "digiforms"
    assert request["extension_name"] == "DigiformsApp"


def test_solicitud_sin_extension_id_se_rechaza(client: TestClient, admin_token: str):
    email = f"cliente-vacio-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _new_user(client, admin_token, email)
    token = _sign_in(client, email, "Extension2026!")

    r = client.post(
        "/api/compat/extensions/requests",
        headers=_auth(token),
        json={"contact_notes": "sin módulo"},
    )
    assert r.status_code == 400, r.text


# --- La migración de datos ----------------------------------------------


def _db_con_graniot():
    return {
        "migrations": {},
        "tables": {
            "platform_modules": [
                {"id": "satelite", "name": "Monitoreo Satelital"},
                {"id": "graniot", "name": "Graniot"},
            ],
            "admin_users": [
                {"id": "admin-1", "user_id": "user-con-graniot", "company_id": "empresa-1", "is_active": True},
            ],
            "company_modules": [
                {"id": "cm-1", "company_id": "empresa-graniot", "module_id": "graniot", "is_enabled": True},
                {"id": "cm-2", "company_id": "empresa-ambos", "module_id": "graniot", "is_enabled": True},
                {"id": "cm-3", "company_id": "empresa-ambos", "module_id": "satelite", "is_enabled": True},
            ],
            "user_modules": [
                {"id": "um-1", "user_id": "user-con-graniot", "module_id": "graniot", "is_enabled": True},
                {"id": "um-2", "user_id": "user-sin-graniot", "module_id": "graniot", "is_enabled": False},
                {"id": "um-3", "user_id": "user-sin-graniot", "module_id": "satelite", "is_enabled": True},
            ],
            "extension_requests": [
                {
                    "id": "req-1",
                    "extension_id": "graniot",
                    "status": "approved",
                    "company_id": "empresa-solo-solicitud",
                    "requested_by_user_id": "user-solicitante",
                },
            ],
        },
    }


def _modules_of(rows, key, value):
    return {row["module_id"] for row in rows if row.get(key) == value}


def test_la_migracion_convierte_graniot_en_satelite():
    db = _db_con_graniot()
    compat.merge_graniot_into_satelite(db, "2026-08-20T00:00:00+00:00")

    company_rows = compat.table(db, "company_modules")
    user_rows = compat.table(db, "user_modules")

    # La empresa que solo tenía Graniot pasa a tener Monitoreo Satelital...
    assert _modules_of(company_rows, "company_id", "empresa-graniot") == {"satelite"}
    # ...la que ya tenía los dos no acaba con la fila duplicada...
    assert [r for r in company_rows if r.get("company_id") == "empresa-ambos"] == [
        r for r in company_rows if r.get("company_id") == "empresa-ambos" and r["module_id"] == "satelite"
    ]
    # ...y la solicitud aprobada deja el acceso escrito en el paquete.
    assert _modules_of(company_rows, "company_id", "empresa-solo-solicitud") == {"satelite"}

    assert _modules_of(user_rows, "user_id", "user-con-graniot") == {"satelite"}
    # Nadie pierde nada: el `false` de Graniot no se convierte en un `false` de
    # Monitoreo Satelital, que sí tenía.
    assert [r for r in user_rows if r.get("user_id") == "user-sin-graniot"] == [
        {"id": "um-3", "user_id": "user-sin-graniot", "module_id": "satelite", "is_enabled": True}
    ]

    assert not [m for m in compat.table(db, "platform_modules") if m.get("id") == "graniot"]


def test_la_migracion_es_idempotente_y_limpia_lo_que_reaparezca():
    """Se ejecuta en cada normalización, así que repetirla no puede cambiar nada.

    Y si algo vuelve a escribir el id viejo —durante un despliegue, la revisión
    anterior sigue sembrando `graniot` unos minutos—, la siguiente pasada lo
    recoge en lugar de dejarlo ahí para siempre.
    """
    db = _db_con_graniot()
    compat.merge_graniot_into_satelite(db, "2026-08-20T00:00:00+00:00")
    antes = json.dumps(db["tables"], sort_keys=True)
    compat.merge_graniot_into_satelite(db, "2026-08-21T00:00:00+00:00")
    assert json.dumps(db["tables"], sort_keys=True) == antes

    compat.table(db, "platform_modules").append({"id": "graniot", "name": "Graniot"})
    compat.table(db, "company_modules").append(
        {"id": "cm-9", "company_id": "empresa-nueva", "module_id": "graniot", "is_enabled": True}
    )
    compat.merge_graniot_into_satelite(db, "2026-08-22T00:00:00+00:00")
    assert not [m for m in compat.table(db, "platform_modules") if m.get("id") == "graniot"]
    assert _modules_of(compat.table(db, "company_modules"), "company_id", "empresa-nueva") == {"satelite"}
