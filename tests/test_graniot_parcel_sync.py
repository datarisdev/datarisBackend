"""Lotes de Dataris subidos/eliminados en la cuenta de Graniot del usuario.

Cubre las dos mitades de la integración por API (sin comercial de Graniot):

* la resolución de la cuenta destino (por coincidencia de email en
  ``/api/accounts/``) y el modo de actuación en su nombre, y
* el flujo real por HTTP: crear un lote lo publica en Graniot y borrarlo lo
  elimina de allí, con un cliente Graniot falso que registra cada llamada.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time

import pytest
from fastapi import HTTPException

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-graniot-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat, graniot  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
def polygon(offset: float = 0.0) -> dict:
    """Polígono propio para cada caso.

    El almacén compat deduplica los lotes de un usuario por geometría, así que
    dos casos con el mismo polígono se pisarían la fila entre ellos.
    """
    west, south = -90.5 + offset, 14.5 + offset
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [west, south + 0.01],
            [west + 0.01, south + 0.01],
            [west + 0.01, south],
            [west, south],
        ]],
    }


POLYGON = polygon()


def _live_jwt(minutes: int = 60, user_id: int | None = 1528) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"token_type": "access", "exp": int(time.time()) + minutes * 60}
    if user_id is not None:
        # Graniot emite el id numérico del usuario en la clave "id".
        payload["id"] = user_id
    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64(payload)}.sig"


def _expired_jwt() -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"token_type": "access", "exp": int(time.time()) - 60}
    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64(payload)}.sig"


def _account(email: str, *, account_id: str = "acc-1528", token: str | None = None, user_id: int | None = 1528) -> dict:
    return {
        "id": account_id,
        "account_email": email,
        "embedded_url": f"https://embed.graniot.com/?auth_id={_live_jwt(user_id=user_id)}",
        "account_access": token if token is not None else _live_jwt(user_id=user_id),
    }


class FakeGraniotClient:
    """Cliente Graniot en memoria que registra cómo se le llamó.

    Todas las instancias comparten ``calls`` para poder afirmar sobre el modo de
    autenticación usado en cada petición (token de la cuenta o client_id).
    """

    calls: list[dict] = []
    accounts: list[dict] = []
    created_parcel_id = 90001
    farms: list[dict] = [{"id": 777, "name": "Dataris", "is_active": True}]
    delete_status: dict[str, int] = {}

    def __init__(self, *, access_token=None, client_id=None):
        self.access_token = access_token
        self.client_id = client_id

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.delete_status = {}

    def _record(self, method, path, **extra):
        FakeGraniotClient.calls.append({
            "method": method,
            "path": path,
            "access_token": self.access_token,
            "client_id": self.client_id,
            **extra,
        })

    async def get(self, path, params=None, **kwargs):
        self._record("GET", path, params=params)
        if path == "/api/accounts/":
            return FakeGraniotClient.accounts
        if path == "/api/farms/":
            return FakeGraniotClient.farms
        return []

    async def post(self, path, json_body=None, params=None, **kwargs):
        self._record("POST", path, json_body=json_body)
        parcels = (json_body or {}).get("parcels") or []
        features = []
        for index, parcel in enumerate(parcels):
            features.append({
                "type": "Feature",
                "id": FakeGraniotClient.created_parcel_id + index,
                "geometry": (parcel.get("geom") or {}).get("geometry"),
                "properties": {
                    "name": parcel.get("name"),
                    "key": f"key-{index}",
                    "wms_url": "https://app.graniot.com/api/wms/?access_key=signed-token&layers=",
                    "image_url": "https://app.graniot.com/api/wms/?BBOX=14.5,-90.5,14.51,-90.49&layers=",
                },
            })
        return {"type": "FeatureCollection", "features": features}

    async def patch(self, path, json_body=None, params=None, **kwargs):
        self._record("PATCH", path, json_body=json_body)
        # Graniot solo acepta la forma documentada: id + parcelGeoJson. La forma
        # de creación (`geom`) responde HTTP 500 en la API real.
        if "geom" in (json_body or {}) or "parcelGeoJson" not in (json_body or {}):
            raise graniot.GraniotAPIError(500, "Server Error (500)")
        return {"id": (json_body or {}).get("id"), "properties": {"key": "key-0"}}

    async def delete(self, path, params=None, **kwargs):
        self._record("DELETE", path, params=params)
        status = FakeGraniotClient.delete_status.get(path)
        if status:
            raise graniot.GraniotAPIError(status, f"HTTP {status}")
        return None

    async def post_form(self, path, data=None, params=None, **kwargs):
        self._record("POST_FORM", path, data=data)
        return FakeGraniotClient.farms[0]


@pytest.fixture(autouse=True)
def _graniot_defaults(monkeypatch):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniotClient)
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_SYNC_PER_USER_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_SYNC_MODE", "auto")
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_SYNC_REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_AUTOSYNC_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_AUTODELETE_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_DEFAULT_FARM_ID", "3615")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "servicio@graniot.test")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", None)
    FakeGraniotClient.accounts = [_account(SUPERADMIN["email"])]
    FakeGraniotClient.reset()
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)
    yield
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)


# --- Resolución de la cuenta destino ---------------------------------------


def test_target_usa_el_token_de_la_cuenta_del_usuario():
    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))

    assert target["mode"] == graniot.SYNC_MODE_TOKEN
    assert target["account_email"] == SUPERADMIN["email"]
    assert target["account_id"] == "acc-1528"
    # client_id identifica al *usuario* de Graniot, no a la cuenta: se toma del
    # user_id numérico del JWT porque el id de /api/accounts/ ("acc-…") hace que
    # Graniot responda HTTP 500.
    assert target["client_id"] == "1528"
    assert target["access_token"]
    # Los datos que llegan al navegador nunca incluyen el token.
    assert "access_token" not in graniot._public_sync_target(target)
    assert graniot._public_sync_target(target)["has_account_token"] is True


def test_target_cae_a_client_id_si_graniot_no_expone_token_vivo():
    FakeGraniotClient.accounts = [_account(SUPERADMIN["email"], token=_expired_jwt())]

    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))

    assert target["mode"] == graniot.SYNC_MODE_CLIENT_ID
    assert target["client_id"] == "1528"
    assert target["access_token"] is None


def test_target_sin_cuenta_en_graniot_no_actua_en_nombre_de_nadie():
    FakeGraniotClient.accounts = [_account("otro@example.com")]

    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))

    assert target["mode"] == graniot.SYNC_MODE_SERVICE
    assert target["reason"] == "no_graniot_account_for_email"


def test_target_respeta_el_kill_switch(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_PARCEL_SYNC_PER_USER_ENABLED", False)

    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))

    assert target["mode"] == graniot.SYNC_MODE_SERVICE
    assert target["reason"] == "per_user_disabled"


def test_target_sobrevive_a_graniot_caido(monkeypatch):
    class _Broken(FakeGraniotClient):
        async def get(self, path, params=None, **kwargs):
            raise RuntimeError("Graniot no responde")

    monkeypatch.setattr(graniot, "GraniotClient", _Broken)

    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))

    assert target["mode"] == graniot.SYNC_MODE_SERVICE
    assert target["reason"] == "accounts_lookup_failed"


def test_target_reutiliza_la_cuenta_guardada_en_el_lote():
    FakeGraniotClient.accounts = [_account("dueño@example.com", account_id="acc-1529", user_id=1529), _account(SUPERADMIN["email"])]

    target = asyncio.run(
        graniot._sync_target_for_row(
            {"id": "u1", "email": SUPERADMIN["email"]},
            {"id": "p1", "graniot_account_email": "dueño@example.com"},
        )
    )

    # El lote se creó en otra cuenta: las actualizaciones y el borrado deben
    # seguir apuntando a ella.
    assert target["account_email"] == "dueño@example.com"
    assert target["account_id"] == "acc-1529"


# --- Cliente impersonado ---------------------------------------------------


def test_cliente_con_token_de_cuenta_no_cae_a_la_api_key(monkeypatch):
    from app.services.graniot_client import GraniotClient

    monkeypatch.setattr("app.services.graniot_client.settings.GRANIOT_API_KEY", "partner-key")
    monkeypatch.setattr("app.services.graniot_client.settings.GRANIOT_CLIENT_ID", "global-client")

    client = GraniotClient(access_token="account-token")
    candidates = client._auth_candidates("application/json")

    assert candidates == [{"Accept": "application/json", "Authorization": "Bearer account-token"}]
    # Con token de cuenta no se envía client_id: la petición ya *es* ese usuario.
    assert client._params({}) == {}
    assert client.acts_on_behalf is True


def test_cliente_con_client_id_lo_envia_como_parametro(monkeypatch):
    from app.services.graniot_client import GraniotClient

    monkeypatch.setattr("app.services.graniot_client.settings.GRANIOT_API_KEY", "partner-key")

    client = GraniotClient(client_id="1528")

    assert client._params({}) == {"client_id": "1528"}


# --- Alta y baja de parcelas ----------------------------------------------


def test_ids_remotos_incluyen_subparcelas_sin_duplicados():
    ids = graniot._remote_parcel_ids({
        "graniot_parcel_id": 900,
        "graniot_parcels": [
            {"graniot_parcel_id": 900},
            {"graniot_parcel_id": 901},
            {"graniot_parcel_id": None},
        ],
    })

    assert ids == ["900", "901"]


def test_borrado_tolera_parcelas_ya_inexistentes():
    FakeGraniotClient.delete_status = {"/api/parcels/901/": 404}

    result = asyncio.run(
        graniot.delete_parcel_from_graniot(
            {"id": "u1", "email": SUPERADMIN["email"]},
            {"id": "p1", "graniot_parcel_id": 900, "graniot_parcels": [{"graniot_parcel_id": 901}]},
            clear_local=False,
        )
    )

    assert result["deleted"] == ["900"]
    assert result["missing"] == ["901"]
    assert result["failed"] == []


def test_borrado_de_lotes_heredados_reintenta_con_la_cuenta_de_servicio():
    # El lote se sincronizó antes del reparto por usuario: vive en la cuenta de
    # la API key, donde el token del usuario no lo encuentra.
    FakeGraniotClient.delete_status = {}

    class _OnlyServiceCanDelete(FakeGraniotClient):
        async def delete(self, path, params=None, **kwargs):
            self._record("DELETE", path, params=params)
            if self.access_token or self.client_id:
                raise graniot.GraniotAPIError(404, "Not found")
            return None

    original = graniot.GraniotClient
    graniot.GraniotClient = _OnlyServiceCanDelete
    try:
        result = asyncio.run(
            graniot.delete_parcel_from_graniot(
                {"id": "u1", "email": SUPERADMIN["email"]},
                {"id": "p1", "graniot_parcel_id": 900},
                clear_local=False,
            )
        )
    finally:
        graniot.GraniotClient = original

    assert result["deleted"] == ["900"]
    assert result["missing"] == []
    attempts = [call for call in FakeGraniotClient.calls if call["method"] == "DELETE"]
    assert len(attempts) == 2, "primero con la cuenta del usuario y luego con la de servicio"
    assert attempts[0]["access_token"] and not attempts[1]["access_token"]


def test_borrado_no_reintenta_cuando_el_lote_sabe_su_cuenta():
    FakeGraniotClient.delete_status = {"/api/parcels/900/": 404}

    result = asyncio.run(
        graniot.delete_parcel_from_graniot(
            {"id": "u1", "email": SUPERADMIN["email"]},
            {"id": "p1", "graniot_parcel_id": 900, "graniot_account_email": SUPERADMIN["email"]},
            clear_local=False,
        )
    )

    assert result["missing"] == ["900"]
    assert len([call for call in FakeGraniotClient.calls if call["method"] == "DELETE"]) == 1


def test_borrado_informa_los_fallos_reales():
    FakeGraniotClient.delete_status = {"/api/parcels/900/": 500}

    result = asyncio.run(
        graniot.delete_parcel_from_graniot(
            {"id": "u1", "email": SUPERADMIN["email"]},
            {"id": "p1", "graniot_parcel_id": 900},
            clear_local=False,
        )
    )

    assert result["deleted"] == []
    assert result["failed"] and result["failed"][0]["status_code"] == 500


def test_sync_automatico_exige_cuenta_propia():
    FakeGraniotClient.accounts = []

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            graniot.sync_local_parcel_to_graniot(
                {"id": "no-existe", "email": "sin-cuenta@example.com"},
                "parcel-inexistente",
                require_account=True,
            )
        )

    # Falla antes de tocar Graniot y sin lote local: lo que importa es que nunca
    # se sube nada a la cuenta de la API key.
    assert exc_info.value.status_code in {404, 409}
    assert not [call for call in FakeGraniotClient.calls if call["method"] == "POST"]


# --- Flujo completo por HTTP ---------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Sin gestor de contexto: el arranque de la app abre Postgres y el sistema
    # de compatibilidad que se prueba aquí no lo necesita.
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_crear_y_borrar_lote_se_refleja_en_graniot(client: TestClient, token: str):
    created = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote Graniot API", "geometry": POLYGON},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]

    # El alta dispara la subida en background (TestClient las ejecuta al
    # terminar la respuesta).
    posts = [call for call in FakeGraniotClient.calls if call["method"] == "POST" and call["path"] == "/api/parcels/"]
    assert len(posts) == 1
    assert posts[0]["access_token"], "el lote debe crearse con el token de la cuenta del usuario"
    assert posts[0]["json_body"]["parcels"][0]["name"] == "Lote Graniot API"
    # La finca global de la API key no se usa cuando se actúa por otra cuenta.
    assert posts[0]["json_body"]["farm"]["id"] != 3615

    stored = client.post(
        "/api/compat/tables/parcels/query",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}], "single": True},
    )
    assert stored.status_code == 200, stored.text
    row = stored.json()["data"]
    assert str(row["graniot_parcel_id"]) == "90001"
    assert row["graniot_account_email"] == SUPERADMIN["email"]
    assert row["graniot_sync_mode"] == graniot.SYNC_MODE_TOKEN
    assert row["graniot_synced_at"]

    FakeGraniotClient.reset()
    removed = client.post(
        "/api/compat/tables/parcels/delete",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}]},
    )
    assert removed.status_code == 200, removed.text

    deletes = [call for call in FakeGraniotClient.calls if call["method"] == "DELETE"]
    assert [call["path"] for call in deletes] == ["/api/parcels/90001/"]
    assert deletes[0]["access_token"], "el borrado debe ir a la cuenta que tiene el lote"


def test_resubir_el_mismo_lote_actualiza_en_vez_de_duplicar(client: TestClient, token: str):
    created = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote reenviado", "geometry": polygon(0.05)},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]
    assert [c for c in FakeGraniotClient.calls if c["method"] == "POST" and c["path"] == "/api/parcels/"]

    FakeGraniotClient.reset()
    # Mismo nombre y misma geometría: compat actualiza la fila existente, así que
    # el lote ya tiene parcelas en Graniot y volver a hacer POST las duplicaría.
    again = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote reenviado", "geometry": polygon(0.05)},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["parcel"]["id"] == parcel_id

    assert not [c for c in FakeGraniotClient.calls if c["method"] == "POST" and c["path"] == "/api/parcels/"]
    patches = [c for c in FakeGraniotClient.calls if c["method"] == "PATCH"]
    assert patches and patches[0]["path"] == "/api/parcels/90001/"


def test_endpoint_de_diagnostico_expone_la_cuenta_destino(client: TestClient, token: str):
    response = client.get("/api/graniot/parcels/sync-target", headers=_auth(token))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mode"] == graniot.SYNC_MODE_TOKEN
    assert data["account_email"] == SUPERADMIN["email"]
    assert data["autosync_enabled"] is True
    assert "access_token" not in data


def test_quitar_de_graniot_mantiene_el_lote_en_dataris(client: TestClient, token: str):
    created = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote solo Dataris", "geometry": polygon(0.1)},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]

    response = client.delete(f"/api/graniot/parcels/sync-local/{parcel_id}", headers=_auth(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["deleted"] == ["90001"]
    assert data["local_cleared"] is True

    stored = client.post(
        "/api/compat/tables/parcels/query",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}], "single": True},
    )
    row = stored.json()["data"]
    assert row["name"] == "Lote solo Dataris"
    assert not row.get("graniot_parcel_id")
    assert not row.get("graniot_synced_at")


def test_sin_user_id_numerico_no_hay_client_id_utilizable():
    # El id de cuenta "acc-<uuid>" no vale como client_id: Graniot responde 500.
    # Sin user_id numérico en el JWT, el token de la cuenta es la única vía.
    FakeGraniotClient.accounts = [_account(SUPERADMIN["email"], user_id=None)]

    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))
    assert target["mode"] == graniot.SYNC_MODE_TOKEN
    assert target["client_id"] is None

    FakeGraniotClient.accounts = [_account(SUPERADMIN["email"], token=_expired_jwt(), user_id=None)]
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)

    without_token = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))
    assert without_token["mode"] == graniot.SYNC_MODE_SERVICE
    assert without_token["reason"] == "account_without_id"


def test_actualizar_usa_el_contrato_documentado_de_patch(client: TestClient, token: str):
    created = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote actualizable", "geometry": polygon(0.15)},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]

    FakeGraniotClient.reset()
    updated = client.post(
        f"/api/graniot/parcels/sync-local/{parcel_id}",
        headers=_auth(token),
        json={"force_update": True},
    )
    assert updated.status_code == 200, updated.text

    patches = [c for c in FakeGraniotClient.calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    body = patches[0]["json_body"]
    assert body["id"] == 90001
    assert "geom" not in body
    feature = body["parcelGeoJson"]["features"][0]
    assert feature["id"] == 90001
    assert feature["geometry"]["type"] == "Polygon"
    # La metadata del PATCH debe ser plana: Graniot la documenta clave/valor.
    assert all(isinstance(value, (str, int, float, bool)) for value in body["metadata"].values())

    stored = client.post(
        "/api/compat/tables/parcels/query",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}], "single": True},
    ).json()["data"]
    # Tras actualizar, el lote conserva su identidad en Graniot.
    assert str(stored["graniot_parcel_id"]) == "90001"
    assert not stored.get("graniot_sync_error")


def test_cambiar_el_numero_de_poligonos_rehace_las_parcelas(client: TestClient, token: str):
    created = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote que cambia", "geometry": polygon(0.2)},
    )
    parcel_id = created.json()["data"]["parcel"]["id"]

    # Dos polígonos donde antes había uno: no se puede emparejar 1 a 1.
    multi = {
        "type": "MultiPolygon",
        "coordinates": [polygon(0.2)["coordinates"], polygon(0.25)["coordinates"]],
    }
    client.post(
        "/api/compat/tables/parcels/update",
        headers=_auth(token),
        json={
            "filters": [{"column": "id", "op": "eq", "value": parcel_id}],
            "data": {"geometry": multi, "geometry_geojson": multi},
        },
    )

    FakeGraniotClient.reset()
    resynced = client.post(
        f"/api/graniot/parcels/sync-local/{parcel_id}",
        headers=_auth(token),
        json={"force_update": True},
    )
    assert resynced.status_code == 200, resynced.text

    methods = [c["method"] for c in FakeGraniotClient.calls if c["path"].startswith("/api/parcels/")]
    assert "DELETE" in methods, "la parcela anterior debe eliminarse"
    assert "POST" in methods, "y volver a crearse con los polígonos nuevos"
    assert "PATCH" not in methods


def test_las_cuentas_se_leen_siguiendo_la_paginacion(monkeypatch):
    """Una respuesta paginada no debe dejar cuentas fuera.

    Un usuario cuya cuenta viva en la segunda página se trataría como "sin
    cuenta en Graniot" y sus lotes no se subirían a ninguna parte.
    """
    pages = {
        "/api/accounts/": {
            "count": 4,
            "next": "https://app.graniot.com/api/accounts/?page=2",
            "results": [_account("uno@example.com", account_id="acc-1"), _account("dos@example.com", account_id="acc-2")],
        },
        "https://app.graniot.com/api/accounts/?page=2": {
            "count": 4,
            "next": None,
            "results": [_account("tres@example.com", account_id="acc-3"), _account(SUPERADMIN["email"], account_id="acc-4")],
        },
    }

    class _Paginated(FakeGraniotClient):
        async def get(self, path, params=None, **kwargs):
            self._record("GET", path, params=params)
            if path in pages:
                return pages[path]
            return await super().get(path, params=params, **kwargs)

    monkeypatch.setattr(graniot, "GraniotClient", _Paginated)

    accounts = asyncio.run(graniot._fetch_all_accounts(_Paginated(), operation="test"))
    assert [a["account_email"] for a in accounts] == [
        "uno@example.com", "dos@example.com", "tres@example.com", SUPERADMIN["email"],
    ]

    # Y la resolución encuentra al usuario aunque esté en la última página.
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)
    target = asyncio.run(graniot._resolve_sync_target(SUPERADMIN["email"]))
    assert target["mode"] == graniot.SYNC_MODE_TOKEN
    assert target["account_id"] == "acc-4"


def test_listado_de_cuentas_es_solo_para_admin_y_no_filtra_tokens(client: TestClient, token: str):
    response = client.get("/api/graniot/accounts", headers=_auth(token))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["count"] == 1
    account = data["accounts"][0]
    assert account["account_email"] == SUPERADMIN["email"]
    assert account["dataris_user"] is True
    assert "account_access" not in account and "embedded_url" not in account
    assert isinstance(data["dataris_users_without_account"], list)

    anonymous = client.get("/api/graniot/accounts")
    assert anonymous.status_code == 401
