"""El proxy WMS con un lote dividido en varias subparcelas de Graniot.

Reproduce lo visto en producción el 20 de agosto de 2026 con un lote de 13
subparcelas: el mapa pidió las 13 imágenes a la vez, ninguna llegó y Azure
reinició el contenedor porque ``/health`` dejó de responder. Tres causas:

* la fila padre guarda el ``graniot_parcel_id`` de su PRIMERA subparcela, y el
  proxy emparejaba por ese id: las 13 imágenes recibían la plantilla, la clave
  y por tanto la imagen de la subparcela 1;
* cada imagen pedía a Graniot los metadatos y reescribía la base local entera
  (``read_db``/``write_db`` síncronos en el event loop): 13 a la vez congelaban
  el proceso, y
* ese guardado pisaba los campos del padre con la subparcela que llegara última.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-wmssub-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import graniot  # noqa: E402
from app.services.graniot_client import GraniotAPIError  # noqa: E402

LOCAL_ID = "lote-dividido-uuid-0001"
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def signed_key(parcel_key: str, stamp: str, signature: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"parcel_key": parcel_key}).encode()).rstrip(b"=").decode()
    return f"{payload}:{stamp}:{signature}"


# Dos subparcelas del mismo lote. La fila padre apunta a la PRIMERA.
SUB_A = {"id": "154633", "key": "aaaaaaaa-0000-4000-8000-00000000000a"}
SUB_B = {"id": "154634", "key": "bbbbbbbb-0000-4000-8000-00000000000b"}
EXPIRED = {s["id"]: signed_key(s["key"], "1wrfRj", f"vieja-{s['id']}") for s in (SUB_A, SUB_B)}
FRESH = {s["id"]: signed_key(s["key"], "1wwiI7", f"nueva-{s['id']}") for s in (SUB_A, SUB_B)}

IMAGE_TEMPLATE = (
    "SERVICE=wms&WARNINGS=False&MAXCC=100.0"
    "&BBOX=19.35,-96.43,19.36,-96.42&FORMAT=image/png&CRS=EPSG:4326"
    "&WIDTH=512&HEIGHT=330&REQUEST=GetMap&VERSION=1.3.0"
)


def wms_url(access_key: str) -> str:
    return f"https://app.graniot.com/api/wms/?access_key={access_key}&layers="


def subparcel_row(sub: dict, access_key: str) -> dict:
    return {
        "graniot_parcel_id": sub["id"],
        "graniot_parcel_key": sub["key"],
        "graniot_wms_access_key": access_key,
        "graniot_wms_url": wms_url(access_key),
        "graniot_image_url": IMAGE_TEMPLATE,
    }


def parent_row() -> dict:
    return {
        "id": LOCAL_ID,
        "user_id": 1,
        "name": "Lote dividido",
        "graniot_parcel_id": SUB_A["id"],
        "graniot_parcel_key": SUB_A["key"],
        "graniot_wms_access_key": EXPIRED[SUB_A["id"]],
        "graniot_wms_url": wms_url(EXPIRED[SUB_A["id"]]),
        "graniot_image_url": IMAGE_TEMPLATE,
        "graniot_parcels": [subparcel_row(SUB_A, EXPIRED[SUB_A["id"]]), subparcel_row(SUB_B, EXPIRED[SUB_B["id"]])],
    }


class FakeResponse:
    def __init__(self, content: bytes, content_type: str, status_code: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = ""


class FakeGraniotClient:
    """Graniot con dos parcelas; solo acepta la clave recién firmada de cada una."""

    get_calls: list = []
    binary_calls: list = []
    get_delay: float = 0.0

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def reset(cls):
        cls.get_calls = []
        cls.binary_calls = []
        cls.get_delay = 0.0

    async def get(self, path, params=None, **kwargs):
        FakeGraniotClient.get_calls.append(path)
        if FakeGraniotClient.get_delay:
            await asyncio.sleep(FakeGraniotClient.get_delay)
        for sub in (SUB_A, SUB_B):
            if path == f"/api/parcels/{sub['id']}/":
                return {
                    "id": int(sub["id"]),
                    "properties": {"key": sub["key"], "name": sub["id"], "wms_url": wms_url(FRESH[sub["id"]]), "image_url": IMAGE_TEMPLATE},
                }
        return {"type": "FeatureCollection", "features": []}

    async def binary_get(self, path, params=None, **kwargs):
        params = params or {}
        FakeGraniotClient.binary_calls.append(dict(params))
        key = str(params.get("access_key") or "")
        for sub_id, fresh in FRESH.items():
            if key == fresh:
                return FakeResponse(PNG_1x1 + sub_id.encode(), "image/png")
        raise GraniotAPIError(400, "['Invalid access key.']", {"status": ["error"], "message": ["Invalid access key."]})


def seed(row: dict) -> None:
    with graniot.LOCK:
        db = graniot.read_db()
        parcels = graniot.table(db, "parcels")
        parcels[:] = [p for p in parcels if p.get("id") != row.get("id")]
        parcels.append(row)
        graniot.write_db(db)


def stored_row() -> dict:
    db = graniot.read_db()
    return next(p for p in graniot.table(db, "parcels") if p["id"] == LOCAL_ID)


def wait_for_stores(timeout: float = 5.0) -> None:
    """El guardado de la clave renovada va en segundo plano; se espera a que termine."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline and any(t.is_alive() for t in graniot._WMS_STORE_THREADS):
        _time.sleep(0.05)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniotClient)
    monkeypatch.setattr(graniot, "_WMS_CACHE_DIR", tmp_path / "wms-cache")
    graniot._RUNTIME_CACHE.clear()
    graniot._WMS_RECOVERY_LOCKS.clear()
    FakeGraniotClient.reset()
    with graniot.LOCK:
        db = graniot.read_db()
        graniot.table(db, "parcels")[:] = []
        graniot.write_db(db)
    yield


def proxy_params(sub: dict) -> dict:
    return {"parcel_id": LOCAL_ID, "access_key": EXPIRED[sub["id"]], "layer": "NDVI", "time": "2026-08-15"}


# --------------------------------------------------------------------------
# Cada subparcela es ella misma, no la primera del lote
# --------------------------------------------------------------------------

def test_la_plantilla_es_la_de_la_subparcela_pedida():
    row = parent_row()
    template = graniot._wms_template_from_local(row, access_key=EXPIRED[SUB_B["id"]], graniot_parcel_id=SUB_A["id"])
    assert EXPIRED[SUB_B["id"]] in template, "con el id del padre seguía ganando la subparcela 1"


def test_el_id_de_graniot_es_el_de_la_subparcela_de_la_clave():
    row = parent_row()
    assert graniot._graniot_parcel_id_for_wms_request(row, EXPIRED[SUB_B["id"]]) == SUB_B["id"]
    assert graniot._graniot_parcel_id_for_wms_request(row, EXPIRED[SUB_A["id"]]) == SUB_A["id"]
    # Sin clave, el del padre.
    assert graniot._graniot_parcel_id_for_wms_request(row, None) == SUB_A["id"]


def test_la_segunda_subparcela_recibe_su_propia_imagen():
    seed(parent_row())
    client = TestClient(app)
    response = client.get("/api/graniot/wms-proxy", params=proxy_params(SUB_B))

    assert response.status_code == 200, response.text
    assert response.content.endswith(SUB_B["id"].encode()), "se sirvió la imagen de otra subparcela"
    assert f"/api/parcels/{SUB_B['id']}/" in FakeGraniotClient.get_calls
    assert f"/api/parcels/{SUB_A['id']}/" not in FakeGraniotClient.get_calls


def test_la_clave_nueva_se_guarda_en_la_subparcela_y_no_pisa_al_padre():
    seed(parent_row())
    client = TestClient(app)
    assert client.get("/api/graniot/wms-proxy", params=proxy_params(SUB_B)).status_code == 200

    wait_for_stores()
    row = stored_row()
    sub_b = next(s for s in row["graniot_parcels"] if s["graniot_parcel_id"] == SUB_B["id"])
    sub_a = next(s for s in row["graniot_parcels"] if s["graniot_parcel_id"] == SUB_A["id"])
    assert sub_b["graniot_wms_access_key"] == FRESH[SUB_B["id"]]
    assert sub_a["graniot_wms_access_key"] == EXPIRED[SUB_A["id"]]
    assert row["graniot_parcel_id"] == SUB_A["id"], "el padre cambió de parcela"
    assert row["graniot_wms_access_key"] == EXPIRED[SUB_A["id"]]


# --------------------------------------------------------------------------
# Muchas imágenes a la vez: una sola consulta a Graniot y una sola escritura
# --------------------------------------------------------------------------

def test_trece_peticiones_concurrentes_consultan_graniot_una_vez_por_subparcela():
    seed(parent_row())
    FakeGraniotClient.get_delay = 0.05
    writes = []
    original_write = graniot.write_db

    def counting_write(db):
        writes.append(1)
        return original_write(db)

    graniot.write_db = counting_write
    try:
        async def run():
            return await asyncio.gather(*[
                graniot._wms_proxy_impl(
                    parcel_id=LOCAL_ID,
                    access_key=EXPIRED[SUB_B["id"]],
                    layer="NDVI",
                    time="2026-08-15",
                    width=512,
                    height=512,
                    south=19.35 + i * 0.001, west=-96.43, north=19.36 + i * 0.001, east=-96.42,
                )
                for i in range(13)
            ])
        responses = asyncio.run(run())
    finally:
        graniot.write_db = original_write

    assert all(r.status_code == 200 for r in responses)
    recoveries = [p for p in FakeGraniotClient.get_calls if p == f"/api/parcels/{SUB_B['id']}/"]
    assert len(recoveries) == 1, f"Graniot recibió {len(recoveries)} consultas para la misma subparcela"
    assert len(writes) == 1, f"la base se reescribió {len(writes)} veces"


def test_la_recuperacion_se_recuerda_entre_peticiones():
    seed(parent_row())
    client = TestClient(app)
    assert client.get("/api/graniot/wms-proxy", params=proxy_params(SUB_B)).status_code == 200
    primera = len(FakeGraniotClient.get_calls)
    # Otra petición de la misma subparcela (distinto bbox → distinta caché de imagen).
    params = dict(proxy_params(SUB_B), south=1.0, west=2.0, north=3.0, east=4.0)
    assert client.get("/api/graniot/wms-proxy", params=params).status_code == 200
    assert len(FakeGraniotClient.get_calls) == primera, "volvió a consultar a Graniot con la clave en memoria"
