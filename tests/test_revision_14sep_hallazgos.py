"""Hallazgos H5, H6, H8 y H10 de la revisión del 14 sep 2026.

* H6: dar de baja una empresa se llevaba solo su fila; sus usuarios, módulos y
  lotes quedaban colgados.
* H5: el alta de empresas podía repetirse (doble clic, otra pestaña) y, con
  varias réplicas, una podía pisar lo que otra acababa de guardar.
* H8: dos polígonos de un mismo archivo con el mismo nombre (o la misma forma)
  se fundían y el archivo perdía un lote sin avisar.
* H10: lo usado se medía con hectáreas escritas a mano por usuario y nada
  impedía cargar lotes por encima del límite de la empresa.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-revision-14sep-")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.routers import compat  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "Revision2026!"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel(monkeypatch_module=None):
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
def admin(client: TestClient) -> str:
    return _sign_in(client, "admin@dataris.local", "admin123456")


def _onboard(client, token, name, email):
    return client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(token),
        json={"company_name": name, "email": email, "password": PASSWORD, "country": "MX", "modules": ["satelite"]},
    )


def _square(offset: float) -> dict:
    w, s = -90.5 + offset, 14.5
    return {"type": "Polygon", "coordinates": [[[w, s], [w, s + 0.01], [w + 0.01, s + 0.01], [w + 0.01, s], [w, s]]]}


# --- H5 -----------------------------------------------------------------------


def test_no_se_crean_dos_empresas_con_el_mismo_nombre(client, admin):
    nombre = f"Limones Cordoba {uuid.uuid4().hex[:5]}"
    primero = _onboard(client, admin, nombre, f"a-{uuid.uuid4().hex[:6]}@cliente.com")
    assert primero.status_code == 200, primero.text
    segundo = _onboard(client, admin, f"  {nombre.upper()} ", f"b-{uuid.uuid4().hex[:6]}@cliente.com")
    assert segundo.status_code == 409
    assert "Ya existe una empresa" in segundo.json()["detail"]


def test_un_guardado_con_estado_viejo_no_pisa_el_de_otra_replica(monkeypatch):
    """Simula dos réplicas: la base ya va por otra revisión que la cacheada."""
    llamadas = []

    def fake_persist(db, expected_revision=None):
        llamadas.append(expected_revision)
        raise compat.StateWriteConflict("otra réplica guardó antes")

    monkeypatch.setattr(compat, "_persist_db", fake_persist)
    db = compat.read_db()
    with pytest.raises(compat.HTTPException) as exc:
        compat.write_db(db)
    assert exc.value.status_code == 409
    assert "Vuelve a intentarlo" in exc.value.detail
    # La caché se descarta: la siguiente lectura trae el estado de la base.
    assert compat.STATE_CACHE is None


@pytest.mark.skipif(not os.getenv("DATARIS_TEST_POSTGRES_DSN"), reason="necesita un Postgres de pruebas")
def test_postgres_rechaza_guardar_sobre_una_revision_vieja(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATARIS_TEST_POSTGRES_DSN"])
    monkeypatch.setenv("DATARIS_COMPAT_PERSISTENCE", "postgres")
    monkeypatch.setattr(compat, "STATE_KEY", f"test-{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(compat, "postgres_dsn", lambda: os.environ["DATARIS_TEST_POSTGRES_DSN"])
    monkeypatch.setattr(compat, "STATE_TABLE_READY", False)

    r1 = compat.write_db_to_postgres({"users": [], "tables": {}})
    assert r1 == 1
    r2 = compat.write_db_to_postgres({"users": [{"id": "a"}], "tables": {}}, r1)
    assert r2 == 2
    # La réplica B leyó la revisión 1 y quiere guardar encima: no puede.
    with pytest.raises(compat.StateWriteConflict):
        compat.write_db_to_postgres({"users": [{"id": "b"}], "tables": {}}, r1)
    assert compat.read_db_from_postgres()["users"] == [{"id": "a"}]
    assert compat.LAST_READ_REVISION == 2


# --- H6 -----------------------------------------------------------------------


def test_dar_de_baja_una_empresa_se_lleva_sus_usuarios_modulos_y_lotes(client, admin):
    nombre = f"Baja {uuid.uuid4().hex[:6]}"
    email = f"titular-{uuid.uuid4().hex[:6]}@cliente.com"
    alta = _onboard(client, admin, nombre, email)
    assert alta.status_code == 200, alta.text
    company_id = alta.json()["data"]["company"]["id"]
    user_id = alta.json()["data"]["user"]["id"]
    lote = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin),
        json={"company_id": company_id, "name": "Lote 1", "geometry": _square(0.3)},
    )
    assert lote.status_code == 200, lote.text

    plan = client.post(f"/api/compat/admin/companies/{company_id}/delete", headers=_auth(admin), json={})
    assert plan.status_code == 200, plan.text
    data = plan.json()["data"]
    assert data["dry_run"] is True and data["users"] == [email] and data["parcels"] == 1 and data["modules"] >= 1
    assert any(c["id"] == company_id for c in compat.table(compat.read_db(force_refresh=True), "companies"))

    hecho = client.post(f"/api/compat/admin/companies/{company_id}/delete", headers=_auth(admin), json={"dry_run": False})
    assert hecho.status_code == 200, hecho.text
    db = compat.read_db(force_refresh=True)
    assert not any(c["id"] == company_id for c in compat.table(db, "companies"))
    assert not any(u["id"] == user_id for u in db["users"])
    for name in ("admin_users", "profiles", "user_roles", "company_modules", "parcels"):
        assert not any(
            r.get("company_id") == company_id or r.get("user_id") == user_id for r in compat.table(db, name)
        ), name
    assert client.post("/api/compat/auth/sign-in", json={"email": email, "password": PASSWORD}).status_code != 200


def test_no_se_da_de_baja_una_empresa_con_cuentas_del_equipo(client, admin):
    db = compat.read_db(force_refresh=True)
    dataris = next(
        a["company_id"] for a in compat.table(db, "admin_users") if a.get("admin_role") == "superadmin" and a.get("company_id")
    )
    response = client.post(f"/api/compat/admin/companies/{dataris}/delete", headers=_auth(admin), json={"dry_run": False})
    assert response.status_code == 409


def test_solo_un_superadmin_da_de_baja_empresas(client, admin):
    nombre = f"Ajena {uuid.uuid4().hex[:6]}"
    email = f"cliente-{uuid.uuid4().hex[:6]}@cliente.com"
    company_id = _onboard(client, admin, nombre, email).json()["data"]["company"]["id"]
    token = _sign_in(client, email, PASSWORD)
    response = client.post(f"/api/compat/admin/companies/{company_id}/delete", headers=_auth(token), json={"dry_run": False})
    assert response.status_code == 403


# --- H8 -----------------------------------------------------------------------

KML_DOS_IGUALES = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Lote 7</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
-90.50,14.50 -90.50,14.51 -90.49,14.51 -90.49,14.50 -90.50,14.50
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>Lote 7</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
-90.40,14.50 -90.40,14.51 -90.39,14.51 -90.39,14.50 -90.40,14.50
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>Lote 8</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
-90.30,14.50 -90.30,14.51 -90.29,14.51 -90.29,14.50 -90.30,14.50
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>"""


def _upload(client, admin, company_id, name="Finca Norte"):
    return client.post(
        "/api/compat/admin/parcels/upload",
        headers=_auth(admin),
        data={"name": name, "company_id": company_id},
        files={"file": ("lotes.kml", KML_DOS_IGUALES.encode(), "application/vnd.google-earth.kml+xml")},
    )


def test_un_archivo_no_pierde_polígonos_con_el_mismo_nombre(client, admin):
    alta = _onboard(client, admin, f"Carga {uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}@cliente.com")
    company_id = alta.json()["data"]["company"]["id"]

    primera = _upload(client, admin, company_id)
    assert primera.status_code == 200, primera.text
    assert primera.json()["data"]["summary"] == {"created": 2, "updated": 0, "renamed": 1}
    listado = client.get("/api/compat/admin/parcels/company", headers=_auth(admin), params={"company_id": company_id})
    nombres = sorted(p["name"] for p in listado.json()["data"]["parcels"])
    assert nombres == ["Lote 7", "Lote 7 (2)", "Lote 8"]

    # Volver a subir el mismo archivo actualiza los mismos 3 lotes, sin crear más.
    segunda = _upload(client, admin, company_id)
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["data"]["summary"]["created"] == 0
    listado = client.get("/api/compat/admin/parcels/company", headers=_auth(admin), params={"company_id": company_id})
    assert len(listado.json()["data"]["parcels"]) == 3


# --- H10 ----------------------------------------------------------------------


def _limited_company(client, admin, limit):
    alta = client.post(
        "/api/compat/admin/clients/onboard",
        headers=_auth(admin),
        json={
            "company_name": f"Limite {uuid.uuid4().hex[:6]}",
            "email": f"l-{uuid.uuid4().hex[:6]}@cliente.com",
            "password": PASSWORD,
            "country": "MX",
            "max_hectares": limit,
            "modules": ["satelite"],
        },
    )
    assert alta.status_code == 200, alta.text
    return alta.json()["data"]["company"]["id"]


def _manual(client, admin, company_id, name, offset):
    return client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin),
        json={"company_id": company_id, "name": name, "geometry": _square(offset)},
    )


def test_lo_usado_es_el_area_real_de_los_lotes(client, admin):
    company_id = _limited_company(client, admin, 1000)
    lote = _manual(client, admin, company_id, "Uno", 0.10)
    assert lote.status_code == 200, lote.text
    area = lote.json()["data"]["parcel"]["area"]
    fila = client.post(
        "/api/compat/tables/companies/query",
        headers=_auth(admin),
        json={"filters": [{"column": "id", "op": "eq", "value": company_id}]},
    ).json()["data"][0]
    assert fila["used_hectares"] == pytest.approx(area, abs=0.01)


def test_una_carga_que_pasa_del_limite_se_rechaza_y_no_se_guarda(client, admin):
    # Cada cuadrado de 0,01° mide ~119 ha: con 150 ha cabe uno y no dos.
    company_id = _limited_company(client, admin, 150)
    assert _manual(client, admin, company_id, "Cabe", 0.20).status_code == 200
    rechazo = _manual(client, admin, company_id, "No cabe", 0.22)
    assert rechazo.status_code == 400
    assert "límite de hectáreas" in rechazo.json()["detail"]
    listado = client.get("/api/compat/admin/parcels/company", headers=_auth(admin), params={"company_id": company_id})
    assert [p["name"] for p in listado.json()["data"]["parcels"]] == ["Cabe"]
    assert listado.json()["data"]["company"]["over_limit"] is False
    # Tampoco por archivo.
    archivo = client.post(
        "/api/compat/admin/parcels/upload",
        headers=_auth(admin),
        data={"name": "Finca", "company_id": company_id},
        files={"file": ("lotes.kml", KML_DOS_IGUALES.encode(), "application/vnd.google-earth.kml+xml")},
    )
    assert archivo.status_code == 400
    listado = client.get("/api/compat/admin/parcels/company", headers=_auth(admin), params={"company_id": company_id})
    assert len(listado.json()["data"]["parcels"]) == 1


def test_sin_limite_definido_no_se_bloquea(client, admin):
    company_id = _limited_company(client, admin, 0)
    assert _manual(client, admin, company_id, "A", 0.30).status_code == 200
    assert _manual(client, admin, company_id, "B", 0.32).status_code == 200


def test_una_empresa_ya_excedida_puede_actualizar_sin_crecer(client, admin):
    company_id = _limited_company(client, admin, 150)
    assert _manual(client, admin, company_id, "Uno", 0.40).status_code == 200
    with compat.LOCK:
        db = compat.read_db()
        next(c for c in compat.table(db, "companies") if c["id"] == company_id)["max_hectares"] = 10
        compat.write_db(db)
    # Re-subir el mismo lote no aumenta lo usado: se permite.
    assert _manual(client, admin, company_id, "Uno", 0.40).status_code == 200
    assert _manual(client, admin, company_id, "Dos", 0.42).status_code == 400


def test_las_hectareas_por_usuario_ya_no_limitan_el_alta(client, admin):
    company_id = _limited_company(client, admin, 1)
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin),
        json={
            "email": f"u-{uuid.uuid4().hex[:6]}@cliente.com",
            "password": PASSWORD,
            "company_id": company_id,
            "assigned_hectares": 5000,
        },
    )
    assert response.status_code == 200, response.text
