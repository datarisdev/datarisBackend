"""El proxy WMS evita pintar la escena nublada («rojo plano»).

Medido en producción el 19-20 de agosto de 2026: Graniot sirve siempre la
escena más reciente cuando la petición no lleva ``time`` y su MAXCC no filtra
nada, así que una pasada nublada deja el lote como un raster casi monocolor.
La escena útil suele estar unos días atrás y Graniot sí la sirve con
``time=YYYY-MM-DD`` (Manguito 1: última escena 9 colores 100% rojo; la del
2026-08-10, 157 colores 98% verde).

Aquí se prueba el detector de escena plana, el selector de fecha limpia con su
caché, y el proxy de punta a punta: sin fecha pedida sirve la escena limpia; con
fecha explícita respeta lo que el usuario eligió.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile

import pytest
from PIL import Image

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-escena-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import graniot  # noqa: E402

PARCEL_KEY = "7e157f27-c0e3-43a0-8a12-127b97000001"
GRANIOT_PARCEL_ID = "154937"
LOCAL_ID = "lote-local-uuid-escena"
CLOUDY_DATE = "2026-08-15"
CLEAR_DATE = "2026-08-10"


def signed_key(parcel_key: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"parcel_key": parcel_key}).encode()
    ).rstrip(b"=").decode()
    return f"{payload}:1wwiI7:firma-vigente"


ACCESS_KEY = signed_key(PARCEL_KEY)


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def flat_png() -> bytes:
    """Raster de escena nublada: un rojo casi uniforme (muy pocos colores)."""
    image = Image.new("RGBA", (64, 64), (200, 30, 30, 255))
    for x in range(0, 64, 16):
        for y in range(0, 64, 16):
            image.putpixel((x, y), (180, 25, 25, 255))
    return png_bytes(image)


def clear_png() -> bytes:
    """Raster de escena despejada: degradado con muchos colores."""
    image = Image.new("RGBA", (64, 64))
    for x in range(64):
        for y in range(64):
            image.putpixel((x, y), (x * 3 % 256, 120 + y % 100, 40, 255))
    return png_bytes(image)


FLAT_PNG = flat_png()
CLEAR_PNG = clear_png()


class FakeResponse:
    def __init__(self, content: bytes, content_type: str = "image/png"):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = 200
        self.text = ""


class FakeGraniotClient:
    """Sirve la escena nublada por defecto y la limpia solo con su fecha."""

    binary_calls: list = []
    dates_calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def reset(cls):
        cls.binary_calls = []
        cls.dates_calls = 0

    async def get(self, path, params=None, **kwargs):
        if "/dates/" in str(path):
            FakeGraniotClient.dates_calls += 1
            return {"status": "success", "data": [{"date": CLOUDY_DATE}, {"date": CLEAR_DATE}, {"date": "2026-08-07"}]}
        return {"type": "FeatureCollection", "features": []}

    async def binary_get(self, path, params=None, **kwargs):
        params = params or {}
        FakeGraniotClient.binary_calls.append(dict(params))
        if str(params.get("time") or "") == CLEAR_DATE:
            return FakeResponse(CLEAR_PNG)
        return FakeResponse(FLAT_PNG)


BASE_ROW = {
    "id": LOCAL_ID,
    "user_id": 1,
    "name": "Manguito de prueba",
    "graniot_parcel_id": GRANIOT_PARCEL_ID,
    "graniot_parcel_key": PARCEL_KEY,
    "graniot_wms_access_key": ACCESS_KEY,
    "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={ACCESS_KEY}&layers=",
}


def seed_local_parcel() -> None:
    with graniot.LOCK:
        db = graniot.read_db()
        parcels = graniot.table(db, "parcels")
        parcels[:] = [p for p in parcels if p.get("id") != LOCAL_ID]
        parcels.append(dict(BASE_ROW))
        graniot.write_db(db)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniotClient)
    monkeypatch.setattr(graniot, "_WMS_CACHE_DIR", tmp_path / "wms-cache")
    graniot._RUNTIME_CACHE.clear()
    FakeGraniotClient.reset()
    yield


client = TestClient(app)


# --------------------------------------------------------------------------
# El detector de escena plana
# --------------------------------------------------------------------------

def test_detecta_la_escena_nublada_y_respeta_la_despejada():
    assert graniot._scene_looks_flat(FLAT_PNG) is True
    assert graniot._scene_looks_flat(CLEAR_PNG) is False


def test_ante_bytes_ilegibles_no_descarta_nada():
    assert graniot._scene_looks_flat(b"esto no es un png") is False
    assert graniot._scene_looks_flat(b"") is False
    assert graniot._scene_looks_flat(None) is False


# --------------------------------------------------------------------------
# El selector de fecha limpia
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_elige_la_primera_fecha_despejada():
    chosen = await graniot._choose_clear_scene_date(
        FakeGraniotClient(),
        parcel_token=LOCAL_ID,
        access_key=ACCESS_KEY,
        layer="NDVI",
        graniot_parcel_id=GRANIOT_PARCEL_ID,
        latest_content=FLAT_PNG,
    )
    assert chosen == CLEAR_DATE


@pytest.mark.anyio
async def test_la_eleccion_queda_cacheada_y_no_se_vuelve_a_sondear():
    kwargs = dict(
        parcel_token=LOCAL_ID,
        access_key=ACCESS_KEY,
        layer="NDVI",
        graniot_parcel_id=GRANIOT_PARCEL_ID,
        latest_content=FLAT_PNG,
    )
    await graniot._choose_clear_scene_date(FakeGraniotClient(), **kwargs)
    probes_before = len(FakeGraniotClient.binary_calls)
    chosen = await graniot._choose_clear_scene_date(FakeGraniotClient(), **kwargs)
    assert chosen == CLEAR_DATE
    assert len(FakeGraniotClient.binary_calls) == probes_before


@pytest.mark.anyio
async def test_si_la_ultima_escena_es_util_no_se_elige_nada(monkeypatch):
    chosen = await graniot._choose_clear_scene_date(
        FakeGraniotClient(),
        parcel_token=LOCAL_ID,
        access_key=ACCESS_KEY,
        layer="NDVI",
        graniot_parcel_id=GRANIOT_PARCEL_ID,
        latest_content=CLEAR_PNG,
    )
    assert chosen is None
    assert FakeGraniotClient.dates_calls == 0


# --------------------------------------------------------------------------
# El proxy de punta a punta
# --------------------------------------------------------------------------

def test_sin_fecha_el_proxy_sirve_la_escena_limpia():
    seed_local_parcel()
    resp = client.get(f"/api/graniot/wms-proxy?parcel_id={LOCAL_ID}&layer=NDVI&width=256&height=256")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/")
    assert graniot._scene_looks_flat(resp.content) is False


def test_con_fecha_explicita_se_respeta_aunque_este_nublada():
    """La fecha del calendario es una decisión del usuario: no se toca."""
    seed_local_parcel()
    resp = client.get(
        f"/api/graniot/wms-proxy?parcel_id={LOCAL_ID}&layer=NDVI&width=256&height=256&time={CLOUDY_DATE}"
    )
    assert resp.status_code == 200, resp.text
    assert graniot._scene_looks_flat(resp.content) is True


def test_la_segunda_peticion_usa_la_eleccion_cacheada():
    seed_local_parcel()
    first = client.get(f"/api/graniot/wms-proxy?parcel_id={LOCAL_ID}&layer=NDVI&width=256&height=256")
    assert first.status_code == 200
    dates_before = FakeGraniotClient.dates_calls
    second = client.get(f"/api/graniot/wms-proxy?parcel_id={LOCAL_ID}&layer=NDVI&width=300&height=300")
    assert second.status_code == 200
    assert graniot._scene_looks_flat(second.content) is False
    assert FakeGraniotClient.dates_calls == dates_before
