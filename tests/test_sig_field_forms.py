"""SIG › Formularios de campo (Bitácoras Sustenta): acceso solo admin y demo.

- La vista y sus tablas son solo para administradores: un usuario operativo
  recibe 403 en el endpoint y nada por el API genérico de tablas.
- La demo se siembra sobre las parcelas reales de Guatemala de la cuenta, con
  todos los puntos dentro del lindero salvo tres, y se puede borrar sin tocar
  datos reales.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-field-forms-")

from fastapi.testclient import TestClient  # noqa: E402
from shapely.geometry import Point, shape  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers.compat import LOCK, read_db, table, write_db  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
URL = "/api/compat/sig-agricola/field-forms"


def _square(lng: float, lat: float, size: float = 0.004) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[lng, lat], [lng + size, lat], [lng + size, lat + size], [lng, lat + size], [lng, lat]]],
    }


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def admin(client: TestClient) -> dict:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return {"token": data["session"]["access_token"], "id": data["user"]["id"]}


@pytest.fixture(scope="module")
def parcelas(admin: dict) -> list[dict]:
    """Ocho parcelas en Escuintla (Guatemala) y una en Sinaloa (México)."""
    rows = []
    with LOCK:
        db = read_db()
        for i in range(8):
            rows.append({
                "id": f"gt-{i}",
                "user_id": admin["id"],
                "name": f"Lote GT {i}",
                "area": 17.5,
                "geometry_geojson": _square(-90.80 + i * 0.01, 14.10),
            })
        rows.append({
            "id": "mx-0",
            "user_id": admin["id"],
            "name": "Lote Sinaloa",
            "area": 20,
            "geometry_geojson": _square(-108.9, 25.6),
        })
        table(db, "parcels").extend(rows)
        write_db(db)
    return rows


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_demo_solo_usa_parcelas_de_guatemala(client: TestClient, admin: dict, parcelas: list[dict]) -> None:
    response = client.post(f"{URL}/demo", json={"action": "seed"}, headers=_auth(admin["token"]))
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    ids = {p["id"] for p in result["parcelas"]}
    assert ids == {f"gt-{i}" for i in range(8)}
    assert 80 <= result["registros"] <= 100
    assert result["fuera_de_lindero"] == 3

    data = client.get(URL, headers=_auth(admin["token"])).json()["data"]
    assert data["forms"][0]["count"] == result["registros"]
    geoms = [shape(p["geometry_geojson"]) for p in parcelas if p["id"].startswith("gt-")]
    fuera = [r for r in data["records"] if not any(g.contains(Point(r["lng"], r["lat"])) for g in geoms)]
    assert len(fuera) == 3
    # La superficie sale de la parcela, no se inventa; la CURP nunca viaja.
    assert all(p["superficie_ha"] == 17.5 for p in data["profiles"])
    assert all("curp" not in p and p["productor"]["curp_registrada"] for p in data["profiles"])
    # El catálogo trae los % de N de referencia.
    n_pct = {p["nombre"]: p["n_pct"] for p in data["products"]}
    assert n_pct["Amoniaco anhidro"] == 82.0 and n_pct["Urea"] == 46.0 and n_pct["MAP 11-52-00"] == 11.0


def test_demo_es_idempotente_y_se_borra(client: TestClient, admin: dict, parcelas: list[dict]) -> None:
    first = client.post(f"{URL}/demo", json={"action": "seed"}, headers=_auth(admin["token"])).json()["data"]
    second = client.post(f"{URL}/demo", json={"action": "seed"}, headers=_auth(admin["token"])).json()["data"]
    assert first["registros"] == second["registros"]
    total = client.get(URL, headers=_auth(admin["token"])).json()["data"]["forms"][0]["count"]
    assert total == second["registros"]

    with LOCK:
        db = read_db()
        table(db, "sig_field_form_records").append({"id": "real-1", "user_id": admin["id"], "form_key": "bitacoras-sustenta"})
        write_db(db)
    cleared = client.post(f"{URL}/demo", json={"action": "clear"}, headers=_auth(admin["token"]))
    assert cleared.status_code == 200
    records = client.get(URL, headers=_auth(admin["token"])).json()["data"]["records"]
    assert [r["id"] for r in records] == ["real-1"]


def test_usuario_operativo_no_ve_la_vista(client: TestClient, admin: dict) -> None:
    created = client.post(
        "/api/compat/admin/users/manual",
        json={
            "email": "operativo-sustenta@example.com",
            "password": "operativo123",
            "admin_role": "company_user",
            "require_password_change": False,
        },
        headers=_auth(admin["token"]),
    )
    assert created.status_code == 200, created.text
    login = client.post("/api/compat/auth/sign-in", json={"email": "operativo-sustenta@example.com", "password": "operativo123"})
    token = login.json()["data"]["session"]["access_token"]

    assert client.get(URL, headers=_auth(token)).status_code == 403
    assert client.post(f"{URL}/demo", json={"action": "clear"}, headers=_auth(token)).status_code == 403
    generic = client.post("/api/compat/tables/sig_field_form_records/query", json={}, headers=_auth(token))
    assert generic.json()["data"] == []
    write = client.post("/api/compat/tables/sig_field_form_records/insert", json={"data": {"form_key": "x"}}, headers=_auth(token))
    assert write.status_code == 403


def test_catalogo_de_productos_editable(client: TestClient, admin: dict) -> None:
    products = [{"nombre": "Urea", "n_pct": 46}, {"nombre": "Nitrato de calcio", "n_pct": 15.5, "precio": 9.1}]
    saved = client.put(f"{URL}/products", json={"products": products}, headers=_auth(admin["token"]))
    assert saved.status_code == 200, saved.text
    catalog = client.get(URL, headers=_auth(admin["token"])).json()["data"]["products"]
    assert {p["nombre"] for p in catalog} == {"Urea", "Nitrato de calcio"}
    bad = client.put(f"{URL}/products", json={"products": [{"nombre": "X", "n_pct": 150}]}, headers=_auth(admin["token"]))
    assert bad.status_code == 400
