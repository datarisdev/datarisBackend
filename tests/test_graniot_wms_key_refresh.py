"""El proxy WMS ante una access_key de Graniot caducada.

Reproduce el fallo observado en producción el 18 de agosto de 2026: siete
peticiones a ``/api/graniot/wms-proxy`` devolvieron 502 porque Graniot rechazó
los 32 intentos con ``Invalid access key.``. La clave venía firmada el 5 de
agosto —trece días antes— y el proxy nunca llegó a renovarla.

La causa era doble:

* el proxy solo sabía localizar el lote por su id local, pero el módulo Satélite
  manda el id de la parcela **en Graniot**. Sin fila local no hay plantilla ni
  manera de renovar la clave, así que se reintentaba 32 veces con la misma clave
  muerta, y
* agotadas las variantes no se pedía una clave nueva: se devolvía un 502 opaco.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-wmskey-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import graniot  # noqa: E402
from app.services.graniot_client import GraniotAPIError  # noqa: E402

PARCEL_KEY = "3e3cca18-e739-4a6a-81ac-5b814a901b01"
GRANIOT_PARCEL_ID = "154467"
LOCAL_ID = "lote-local-uuid-0001"

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def signed_key(parcel_key: str, stamp: str, signature: str) -> str:
    """Token con la forma real de Graniot: base64({parcel_key}):fecha:firma."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"parcel_key": parcel_key}).encode()
    ).rstrip(b"=").decode()
    return f"{payload}:{stamp}:{signature}"


EXPIRED_KEY = signed_key(PARCEL_KEY, "1wrfRj", "firma-del-5-de-agosto")
FRESH_KEY = signed_key(PARCEL_KEY, "1wwiI7", "firma-recien-emitida")

IMAGE_TEMPLATE = (
    "SERVICE=wms&WARNINGS=False&MAXCC=100.0"
    "&BBOX=19.51079320027754,-96.38929889939419,19.51150255799982,-96.38884623883314"
    "&FORMAT=image/png&CRS=EPSG:4326&WIDTH=512&HEIGHT=330&REQUEST=GetMap&VERSION=1.3.0"
)


def parcel_feature(access_key: str) -> dict:
    return {
        "id": int(GRANIOT_PARCEL_ID),
        "properties": {
            "key": PARCEL_KEY,
            "name": "Lote de prueba",
            "wms_url": f"https://app.graniot.com/api/wms/?access_key={access_key}&layers=",
            "image_url": IMAGE_TEMPLATE,
        },
    }


class FakeResponse:
    def __init__(self, content: bytes, content_type: str, status_code: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = "" if content_type.startswith("image/") else content.decode("utf-8", "replace")


class FakeGraniotClient:
    """Graniot que solo acepta la clave recién firmada."""

    recovery_features: list = []
    binary_calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def reset(cls, *, recovery: bool = True):
        cls.recovery_features = [parcel_feature(FRESH_KEY)] if recovery else []
        cls.binary_calls = []

    async def get(self, path, params=None, **kwargs):
        return {"type": "FeatureCollection", "features": list(FakeGraniotClient.recovery_features)}

    async def binary_get(self, path, params=None, **kwargs):
        params = params or {}
        FakeGraniotClient.binary_calls.append(dict(params))
        if str(params.get("access_key") or "") == FRESH_KEY:
            return FakeResponse(PNG_1x1, "image/png")
        raise GraniotAPIError(
            400,
            "['Invalid access key.']",
            {"status": ["error"], "message": ["Invalid access key."]},
        )


def seed_local_parcel(row: dict) -> None:
    with graniot.LOCK:
        db = graniot.read_db()
        parcels = graniot.table(db, "parcels")
        parcels[:] = [p for p in parcels if p.get("id") != row.get("id")]
        parcels.append(row)
        graniot.write_db(db)


BASE_ROW = {
    "id": LOCAL_ID,
    "user_id": 1,
    "name": "Lote de prueba",
    "graniot_parcel_id": GRANIOT_PARCEL_ID,
    "graniot_parcel_key": PARCEL_KEY,
    "graniot_wms_access_key": EXPIRED_KEY,
    "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key={EXPIRED_KEY}&layers=",
    "graniot_image_url": IMAGE_TEMPLATE,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniotClient)
    # El proxy cachea el PNG en disco y en memoria; sin aislarlo, un caso
    # serviría la imagen que dejó el anterior y nunca llegaría a Graniot.
    monkeypatch.setattr(graniot, "_WMS_CACHE_DIR", tmp_path / "wms-cache")
    graniot._RUNTIME_CACHE.clear()
    FakeGraniotClient.reset()
    with graniot.LOCK:
        db = graniot.read_db()
        graniot.table(db, "parcels")[:] = []
        graniot.write_db(db)
    yield


# --------------------------------------------------------------------------
# Localizar el lote: el id que llega no siempre es el local
# --------------------------------------------------------------------------

def test_encuentra_el_lote_por_su_id_local():
    seed_local_parcel(dict(BASE_ROW))
    found = graniot._find_local_parcel_for_wms(LOCAL_ID, EXPIRED_KEY)
    assert found and found["id"] == LOCAL_ID


def test_encuentra_el_lote_por_el_id_de_graniot():
    """El caso que rompía en producción: llega 154467, no el UUID local."""
    seed_local_parcel(dict(BASE_ROW))
    found = graniot._find_local_parcel_for_wms(GRANIOT_PARCEL_ID, EXPIRED_KEY)
    assert found and found["id"] == LOCAL_ID


def test_encuentra_el_lote_por_el_parcel_key_de_la_clave_firmada():
    """Sin ningún id utilizable, el token firmado lleva dentro el parcel_key."""
    row = dict(BASE_ROW)
    row.pop("graniot_parcel_id")
    seed_local_parcel(row)
    found = graniot._find_local_parcel_for_wms(None, EXPIRED_KEY)
    assert found and found["id"] == LOCAL_ID


def test_encuentra_el_lote_por_una_de_sus_subparcelas():
    row = dict(BASE_ROW)
    row.pop("graniot_parcel_id")
    row.pop("graniot_parcel_key")
    row["graniot_parcels"] = [
        {"graniot_parcel_id": "999001", "graniot_parcel_key": "aaaaaaaa-0000-0000-0000-000000000001"},
        {"graniot_parcel_id": GRANIOT_PARCEL_ID, "graniot_parcel_key": PARCEL_KEY},
    ]
    seed_local_parcel(row)
    found = graniot._find_local_parcel_for_wms(GRANIOT_PARCEL_ID, None)
    assert found and found["id"] == LOCAL_ID


def test_no_inventa_un_lote_cuando_no_hay_coincidencia():
    seed_local_parcel(dict(BASE_ROW))
    assert graniot._find_local_parcel_for_wms("777777", signed_key(
        "11111111-2222-3333-4444-555555555555", "1wwiI7", "otra")) is None
    assert graniot._find_local_parcel_for_wms(None, None) is None


# --------------------------------------------------------------------------
# El proxy renueva la clave en vez de devolver 502
# --------------------------------------------------------------------------

def test_una_clave_caducada_se_renueva_y_la_imagen_llega():
    seed_local_parcel(dict(BASE_ROW))
    client = TestClient(app)
    response = client.get("/api/graniot/wms-proxy", params={
        "parcel_id": GRANIOT_PARCEL_ID,
        "access_key": EXPIRED_KEY,
        "layer": "NDVI",
        "width": 768,
        "height": 768,
    })

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")
    assert response.content == PNG_1x1

    usadas = [call.get("access_key") for call in FakeGraniotClient.binary_calls]
    assert FRESH_KEY in usadas, "el proxy debe reintentar con la clave recién firmada"


def test_la_clave_renovada_queda_guardada_para_la_proxima_vez():
    seed_local_parcel(dict(BASE_ROW))
    client = TestClient(app)
    client.get("/api/graniot/wms-proxy", params={
        "parcel_id": GRANIOT_PARCEL_ID,
        "access_key": EXPIRED_KEY,
        "layer": "NDVI",
    })

    db = graniot.read_db()
    row = next(p for p in graniot.table(db, "parcels") if p["id"] == LOCAL_ID)
    assert row["graniot_wms_access_key"] == FRESH_KEY
    assert EXPIRED_KEY not in str(row.get("graniot_wms_url") or "")


def test_si_graniot_ya_no_conoce_la_parcela_se_pide_resincronizar():
    """Sin clave nueva no es un fallo de red: el lote perdió su parcela."""
    seed_local_parcel(dict(BASE_ROW))
    FakeGraniotClient.reset(recovery=False)
    client = TestClient(app)
    response = client.get("/api/graniot/wms-proxy", params={
        "parcel_id": GRANIOT_PARCEL_ID,
        "access_key": EXPIRED_KEY,
        "layer": "NDVI",
    })

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["requires_resync"] is True
    assert "sincronizar" in detail["message"].lower()
