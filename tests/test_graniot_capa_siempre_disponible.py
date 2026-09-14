"""La capa satelital de la Zona de Análisis: siempre, para cualquier lote, rápido.

Reproduce el fallo visto en producción el 12 sep 2026 (lote «Moreno Norte
B-11» de ASV): la parcela vivía en la cuenta embebida del usuario, la clave
firmada guardada caducó, y la renovación buscaba con la clave de servicio, que
no ve esas parcelas («No Parcel matches the given query»). ``map-layer``
entregaba una imagen con la clave muerta, el proxy probaba 5 variantes y
terminaba en 409 «vuelve a sincronizar desde el panel», cosa que un cliente no
puede hacer. Además tardaba 12 s: descargaba la cuenta entera de Graniot y
reescribía la base dos veces en el camino de la petición.

Cubre:
* la renovación y las fechas usan la cuenta dueña del lote;
* con id conocido se pide solo esa parcela, nunca el listado completo;
* si la parcela ya no existe, ``map-layer`` la recrea al momento;
* el proxy también la recrea (como dueño) en vez de responder 409;
* la petición mínima va primero y hay como mucho 4 variantes;
* las escrituras de la base salen del camino de la petición.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time as _time
from urllib.parse import unquote

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-capa-siempre-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import graniot  # noqa: E402
from app.services.graniot_client import GraniotAPIError  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
LOCAL_ID = "lote-asv-b11"
PARCEL_KEY = "760216b3-d7fa-4d81-8ff4-4c9d91a5157a"
GRANIOT_ID = "160905"
NEW_GRANIOT_ID = "170001"
NEW_PARCEL_KEY = "aaaaaaaa-1111-2222-3333-444444444444"

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

GEOMETRY = {
    "type": "Polygon",
    "coordinates": [[
        [-96.2352, 18.6045], [-96.2352, 18.6055], [-96.2341, 18.6055], [-96.2341, 18.6045], [-96.2352, 18.6045],
    ]],
}


def signed_key(parcel_key: str, stamp: str, signature: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"parcel_key": parcel_key}).encode()).rstrip(b"=").decode()
    return f"{payload}:{stamp}:{signature}"


EXPIRED_KEY = signed_key(PARCEL_KEY, "1x48OA", "firma-del-9-de-septiembre")
FRESH_KEY = signed_key(PARCEL_KEY, "1x5aBc", "firma-recien-emitida")
NEW_KEY = signed_key(NEW_PARCEL_KEY, "1x5aBd", "firma-de-la-parcela-nueva")

IMAGE_TEMPLATE = (
    "SERVICE=wms&WARNINGS=False&MAXCC=100.0"
    "&BBOX=18.6045061,-96.23523861,18.60550147,-96.23410883"
    "&FORMAT=image/png&CRS=EPSG:4326&WIDTH=512&HEIGHT=330&REQUEST=GetMap&VERSION=1.3.0"
)


def parcel_feature(graniot_id: str, parcel_key: str, access_key: str) -> dict:
    return {
        "type": "Feature",
        "id": int(graniot_id),
        "geometry": GEOMETRY,
        "properties": {
            "key": parcel_key,
            "name": "Moreno Norte B-11",
            "wms_url": f"https://app.graniot.com/api/wms/?access_key={access_key}&layers=",
            "image_url": IMAGE_TEMPLATE,
            "parcelresolution_set": [{"resolution": 1, "last_image_date": "2026-09-10"}],
        },
    }


class FakeResponse:
    def __init__(self, content: bytes, content_type: str, status_code: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = "" if content_type.startswith("image/") else content.decode("utf-8", "replace")


class FakeGraniot:
    """Graniot que solo conoce la parcela por su detalle y solo acepta claves vigentes."""

    calls: list = []
    binary_calls: list = []
    instances: list = []
    detail_gone = False
    valid_keys: set = set()

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        FakeGraniot.instances.append(kwargs)

    @classmethod
    def reset(cls, *, detail_gone: bool = False):
        cls.calls = []
        cls.binary_calls = []
        cls.instances = []
        cls.detail_gone = detail_gone
        cls.valid_keys = {FRESH_KEY, NEW_KEY}

    async def get(self, path, params=None, **kwargs):
        FakeGraniot.calls.append(("GET", path))
        if path == "/api/parcels/":
            return {"type": "FeatureCollection", "features": []}
        if path.startswith("/api/parcels/") and "/resolutions/" in path:
            return []
        if path.startswith("/api/parcels/"):
            graniot_id = path.strip("/").split("/")[-1]
            if FakeGraniot.detail_gone or graniot_id not in {GRANIOT_ID, NEW_GRANIOT_ID}:
                raise GraniotAPIError(404, "No Parcel matches the given query.", {"detail": "No Parcel matches the given query."})
            if graniot_id == NEW_GRANIOT_ID:
                return parcel_feature(NEW_GRANIOT_ID, NEW_PARCEL_KEY, NEW_KEY)
            return parcel_feature(GRANIOT_ID, PARCEL_KEY, FRESH_KEY)
        return []

    async def binary_get(self, path, params=None, **kwargs):
        params = dict(params or {})
        FakeGraniot.binary_calls.append(params)
        if str(params.get("access_key") or "") in FakeGraniot.valid_keys:
            return FakeResponse(PNG_1x1, "image/png")
        raise GraniotAPIError(400, "['Invalid access key.']", {"status": ["error"], "message": ["Invalid access key."]})


def _admin_user_id() -> str:
    db = graniot.read_db(force_refresh=True)
    return next(u["id"] for u in db["users"] if u.get("email") == SUPERADMIN["email"])


def base_row(user_id: str, **extra) -> dict:
    row = {
        "id": LOCAL_ID,
        "user_id": user_id,
        "name": "Moreno Norte B-11",
        "geometry": GEOMETRY,
        "graniot_parcel_id": GRANIOT_ID,
        "graniot_parcel_key": PARCEL_KEY,
        "graniot_wms_access_key": EXPIRED_KEY,
        "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={EXPIRED_KEY}&layers=",
        "graniot_image_url": IMAGE_TEMPLATE,
        "graniot_synced_at": "2026-09-09T02:50:18+00:00",
        "graniot_parcels": [{
            "graniot_parcel_id": GRANIOT_ID,
            "graniot_parcel_key": PARCEL_KEY,
            "graniot_wms_access_key": EXPIRED_KEY,
            "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={EXPIRED_KEY}&layers=",
            "graniot_image_url": IMAGE_TEMPLATE,
            "parcelresolution_set": [{"resolution": 1, "last_image_date": None}],
        }],
    }
    row.update(extra)
    return row


def seed(row: dict) -> None:
    with graniot.LOCK:
        db = graniot.read_db()
        parcels = graniot.table(db, "parcels")
        parcels[:] = [p for p in parcels if p.get("id") != row.get("id")]
        parcels.append(row)
        graniot.write_db(db)


def wait_for_background(timeout: float = 5.0) -> None:
    deadline = _time.time() + timeout
    while _time.time() < deadline and any(t.is_alive() for t in graniot._WMS_STORE_THREADS):
        _time.sleep(0.05)


def fake_resync_factory(record: list):
    """Sustituto de sync_local_parcel_to_graniot: crea la «parcela nueva» en la fila."""

    async def fake_sync(user, parcel_id, payload=None, **kwargs):
        record.append({"user": user, "parcel_id": parcel_id, "payload": payload})
        with graniot.LOCK:
            db = graniot.read_db()
            row = next(p for p in graniot.table(db, "parcels") if p.get("id") == parcel_id)
            row.update({
                "graniot_parcel_id": NEW_GRANIOT_ID,
                "graniot_parcel_key": NEW_PARCEL_KEY,
                "graniot_wms_access_key": NEW_KEY,
                "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={NEW_KEY}&layers=",
                "graniot_image_url": IMAGE_TEMPLATE,
                "graniot_parcels": [{
                    "graniot_parcel_id": NEW_GRANIOT_ID,
                    "graniot_parcel_key": NEW_PARCEL_KEY,
                    "graniot_wms_access_key": NEW_KEY,
                    "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={NEW_KEY}&layers=",
                    "graniot_image_url": IMAGE_TEMPLATE,
                    "parcelresolution_set": [{"resolution": 1, "last_image_date": None}],
                }],
                "graniot_synced_at": graniot.now(),
            })
            graniot.write_db(db)
            return {"parcel": dict(row), "graniot": None}

    return fake_sync


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniot)
    monkeypatch.setattr(graniot, "_WMS_CACHE_DIR", tmp_path / "wms-cache")
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_AUTOSYNC_ENABLED", True)
    graniot._RUNTIME_CACHE.clear()
    graniot._WMS_RECOVERY_LOCKS.clear()
    FakeGraniot.reset()
    with graniot.LOCK:
        db = graniot.read_db()
        graniot.table(db, "parcels")[:] = []
        graniot.write_db(db)
    yield
    wait_for_background()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _map_layer(client: TestClient, token: str, **params):
    query = {"include_statistics": "false", "auto_sync": "true", **params}
    return client.get(f"/api/graniot/parcels/{LOCAL_ID}/ndvi/map-layer", params=query, headers=_auth(token))


# --- La cuenta correcta ----------------------------------------------------


def test_la_renovacion_usa_la_cuenta_duena_del_lote(monkeypatch):
    """Un lote sincronizado «en nombre del usuario» se consulta con SU cuenta."""
    row = base_row("user-x", graniot_sync_mode="token", graniot_account_email="dataris-embed+u1@dataris.es")
    seen = {}

    async def fake_target(user, local=None, **kwargs):
        seen["email"] = (user or {}).get("email")
        seen["stored"] = (local or {}).get("graniot_account_email")
        return {"mode": graniot.SYNC_MODE_TOKEN, "access_token": "token-de-la-cuenta-embebida"}

    monkeypatch.setattr(graniot, "_sync_target_for_row", fake_target)
    import asyncio

    made = asyncio.run(graniot._client_for_local_row(row, user={"id": "user-x", "email": "jsalado@innovagro.app"}))
    assert made.kwargs.get("access_token") == "token-de-la-cuenta-embebida"
    assert seen == {"email": "jsalado@innovagro.app", "stored": "dataris-embed+u1@dataris.es"}


def test_una_fila_de_servicio_sigue_con_la_clave_de_servicio():
    import asyncio

    made = asyncio.run(graniot._client_for_local_row(base_row("user-x", graniot_sync_mode="service")))
    assert made.kwargs == {}
    made = asyncio.run(graniot._client_for_local_row(base_row("user-x")))
    assert made.kwargs == {}


# --- map-layer: por id, nunca el listado; recrea si se perdió ---------------


def test_map_layer_renueva_la_clave_por_id_y_no_descarga_la_cuenta(client, token):
    seed(base_row(_admin_user_id()))

    response = _map_layer(client, token)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["available"] is True
    assert FRESH_KEY in unquote(data["overlays"][0]["image_url"]), "la imagen debe salir con la clave recién firmada"
    assert EXPIRED_KEY not in unquote(data["overlays"][0]["image_url"])
    assert data["date"] == "2026-09-10", "las fechas también vienen del refresco por id"
    assert ("GET", f"/api/parcels/{GRANIOT_ID}/") in FakeGraniot.calls
    assert ("GET", "/api/parcels/") not in FakeGraniot.calls, "con id conocido no se recorre la cuenta entera"

    wait_for_background()
    db = graniot.read_db(force_refresh=True)
    row = next(p for p in graniot.table(db, "parcels") if p["id"] == LOCAL_ID)
    assert row["graniot_wms_access_key"] == FRESH_KEY, "la clave renovada queda guardada (en segundo plano)"


def test_map_layer_recrea_la_parcela_si_graniot_ya_no_la_tiene(client, token, monkeypatch):
    seed(base_row(_admin_user_id()))
    FakeGraniot.reset(detail_gone=True)
    resyncs: list = []
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", fake_resync_factory(resyncs))

    response = _map_layer(client, token)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["available"] is True
    assert resyncs and resyncs[0]["parcel_id"] == LOCAL_ID
    assert resyncs[0]["user"]["email"] == SUPERADMIN["email"], "se recrea con el token del dueño"
    assert NEW_KEY in unquote(data["overlays"][0]["image_url"])
    assert ("GET", "/api/parcels/") not in FakeGraniot.calls, "parcela perdida: se recrea, no se busca en el listado"
    assert any("volvió a crear" in w for w in data["warnings"])


def test_map_layer_no_bloquea_la_respuesta_con_las_escrituras(client, token, monkeypatch):
    """Las dos escrituras de la base van en hilos aparte."""
    seed(base_row(_admin_user_id()))
    slow = {"calls": 0}
    original = graniot.write_db

    def slow_write(db):
        slow["calls"] += 1
        _time.sleep(0.4)
        return original(db)

    monkeypatch.setattr(graniot, "write_db", slow_write)
    started = _time.perf_counter()
    response = _map_layer(client, token)
    elapsed = _time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert elapsed < 0.6, f"la respuesta esperó a las escrituras: {elapsed:.2f}s"
    wait_for_background()
    assert slow["calls"] >= 2, "sí se persisten: instantánea y registro del análisis"


# --- proxy: mínima primero, pocas variantes, recrea en vez de 409 -----------


def test_la_peticion_minima_va_primero_y_hay_pocas_variantes():
    variants = graniot._build_wms_param_variants(
        template_params={"BBOX": "1,2,3,4", "Geometry": "POLYGON((0 0,1 1,1 0,0 0))", "access_key": FRESH_KEY},
        access_key=FRESH_KEY,
        layer="NDVI",
        time="2026-09-10",
        width=1024,
        height=1024,
        bbox_latlon="1,2,3,4",
        bbox_lonlat="2,1,4,3",
    )
    first = variants[0]
    assert set(first) == {"access_key", "layers", "response_format", "width", "height", "time"}
    assert graniot.GRANIOT_WMS_MAX_VARIANTS <= 4


def test_el_proxy_sirve_la_imagen_a_la_primera_con_clave_vigente(client):
    seed(base_row(_admin_user_id()))
    response = client.get("/api/graniot/wms-proxy", params={
        "parcel_id": LOCAL_ID, "access_key": FRESH_KEY, "layer": "NDVI", "width": 1024, "height": 1024,
    })
    assert response.status_code == 200, response.text
    assert response.content == PNG_1x1
    assert len(FakeGraniot.binary_calls) == 1, "clave válida = una sola petición a Graniot"
    assert ("GET", f"/api/parcels/{GRANIOT_ID}/") not in FakeGraniot.calls, "sin renovación «por si acaso»"


def test_el_proxy_renueva_la_clave_caducada_con_pocas_peticiones(client):
    seed(base_row(_admin_user_id()))
    response = client.get("/api/graniot/wms-proxy", params={
        "parcel_id": LOCAL_ID, "access_key": EXPIRED_KEY, "layer": "NDVI",
    })
    assert response.status_code == 200, response.text
    assert response.content == PNG_1x1
    keys = [call.get("access_key") for call in FakeGraniot.binary_calls]
    assert keys[-1] == FRESH_KEY
    assert len(FakeGraniot.binary_calls) <= graniot.GRANIOT_WMS_MAX_VARIANTS + 1


def test_el_proxy_recrea_la_parcela_perdida_en_vez_de_409(client, monkeypatch):
    seed(base_row(_admin_user_id()))
    FakeGraniot.reset(detail_gone=True)
    resyncs: list = []
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", fake_resync_factory(resyncs))

    response = client.get("/api/graniot/wms-proxy", params={
        "parcel_id": LOCAL_ID, "access_key": EXPIRED_KEY, "layer": "NDVI",
    })

    assert response.status_code == 200, response.text
    assert response.content == PNG_1x1
    assert resyncs and resyncs[0]["user"]["email"] == SUPERADMIN["email"], "se recrea como el dueño del lote"
    assert FakeGraniot.binary_calls[-1].get("access_key") == NEW_KEY


def test_el_proxy_no_recrea_la_misma_parcela_en_bucle(client, monkeypatch):
    seed(base_row(_admin_user_id()))
    FakeGraniot.reset(detail_gone=True)
    resyncs: list = []

    async def failing_sync(user, parcel_id, payload=None, **kwargs):
        resyncs.append(parcel_id)
        raise RuntimeError("Graniot caído")

    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", failing_sync)
    for _ in range(3):
        response = client.get("/api/graniot/wms-proxy", params={
            "parcel_id": LOCAL_ID, "access_key": EXPIRED_KEY, "layer": "NDVI",
        })
        assert response.status_code == 409
    assert len(resyncs) == 1, "una recreación por lote cada 10 minutos"


# --- cualquier forma --------------------------------------------------------


def test_cualquier_forma_se_convierte_en_poligonos_validos():
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]}
    collection = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "Polygon", "coordinates": [[[10, 10], [10, 11], [11, 11], [11, 10], [10, 10]]]},
            {"type": "MultiPolygon", "coordinates": [[[[20, 20], [20, 21], [21, 21], [21, 20], [20, 20]]]]},
        ],
    }
    repaired = graniot._polygonal_geometry(bowtie)
    assert repaired and repaired["type"] in {"Polygon", "MultiPolygon"}
    from shapely.geometry import shape as shapely_shape
    assert shapely_shape(repaired).is_valid

    flattened = graniot._polygonal_geometry(collection)
    assert flattened and flattened["type"] == "MultiPolygon"
    assert len(flattened["coordinates"]) == 2

    assert graniot._polygonal_geometry({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}) is None

    fc = graniot._feature_collection_from_geometry(collection, "lote-1", "Lote 1")
    assert len(fc["features"]) == 1
    assert fc["features"][0]["geometry"]["type"] == "MultiPolygon"
