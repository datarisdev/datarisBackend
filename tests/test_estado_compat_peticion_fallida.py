"""Una petición que falla no deja cambios a medias en el estado compartido.

`compat.read_db()` entrega el diccionario cacheado del proceso, no una copia. Si
un endpoint lo modifica y falla antes de su `write_db`, el cambio sobrevive en
memoria y la siguiente escritura de otra petición lo persiste: aparecen filas que
nadie guardó. Aquí se comprueba que la caché se tira cuando una petición no
termina bien, de modo que ese arrastre no ocurra.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
STORAGE_DIR = tempfile.mkdtemp(prefix="dataris-compat-peticion-fallida-")
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = STORAGE_DIR

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
FANTASMA = "EMPRESA QUE NADIE GUARDÓ"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist_para_pruebas():
    previo = os.environ.get("DATARIS_ADMIN_PANEL_EMAILS")
    os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = "admin@dataris.local"
    yield
    if previo is None:
        os.environ.pop("DATARIS_ADMIN_PANEL_EMAILS", None)
    else:
        os.environ["DATARIS_ADMIN_PANEL_EMAILS"] = previo


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _empresas_persistidas() -> list:
    """Lo realmente guardado, releído desde el almacenamiento y no de la caché.

    Ojo: releer también refresca la caché, así que solo se llama al final. Si se
    llamara antes del write legítimo, borraría por su cuenta el cambio colado y
    la prueba pasaría sin comprobar nada.
    """
    return [c.get("name") for c in compat.table(compat.read_db(force_refresh=True), "companies")]


def test_un_cambio_de_una_peticion_fallida_no_lo_persiste_la_siguiente(
    client: TestClient, admin_token: str
):
    # Se reproduce lo que haría un endpoint que modifica el estado y después
    # falla antes de su write_db: el cambio queda en el diccionario cacheado…
    db = compat.read_db()
    compat.table(db, "companies").append({"id": str(uuid.uuid4()), "name": FANTASMA})

    # …y la petición termina en error, que es lo que dispara el descarte de la
    # caché (aquí un login inválido; sirve cualquier respuesta 4xx o 5xx).
    fallida = client.post(
        "/api/compat/auth/sign-in",
        json={"email": SUPERADMIN["email"], "password": "contraseña-que-no-es"},
    )
    assert fallida.status_code >= 400

    # Ahora otra petición, sin relación, escribe algo legítimo.
    legitima = f"Empresa legítima {uuid.uuid4().hex[:6]}"
    respuesta = client.post(
        "/api/compat/tables/companies/insert",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"data": {"name": legitima, "max_hectares": 1}},
    )
    assert respuesta.status_code == 200, respuesta.text

    empresas = _empresas_persistidas()
    assert legitima in empresas
    assert FANTASMA not in empresas, (
        "la escritura legítima arrastró un cambio de una petición que falló"
    )
