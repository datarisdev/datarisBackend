"""Cuadrar el mapa de un cliente con los lotes que tiene en Dataris.

El síntoma reportado: hay usuarios con lotes cargados en Dataris cuyo módulo
Satélite enseña otra cosa. Ocurre cuando el lote se cargó antes de que esa
persona tuviera cuenta embebida propia: sin cuenta, el destino de
sincronización degradaba a la de servicio y el lote nunca salió de Dataris (o
acabó en una cuenta que no es la suya). Listado y mapa dejan de contar lo mismo.

Aquí se cubre el diagnóstico (quién está descuadrado y por qué) y el cuadre
(crear su portal y subirle los lotes que faltan), incluido el caso delicado:
un lote que ya vive en otra cuenta solo se mueve si se pide expresamente.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ.setdefault("DATARIS_COMPAT_STORAGE_DIR", tempfile.mkdtemp(prefix="dataris-compat-cuadre-"))

from app.api.routers import graniot  # noqa: E402
from app.api.routers.compat import LOCK, read_db, table, write_db  # noqa: E402

CON_LOTES = "cliente-con-lotes@agricola.test"
PLANTILLA = "dataris-embed+{uid}@dataris.es"


def _jwt(minutes: int = 60) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"token_type": "access", "exp": int(time.time()) + minutes * 60, "id": 4242}
    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64(payload)}.sig"


class FakeGraniot:
    """Graniot en memoria: sin censo de fincas, como el cliente recién creado."""

    accounts: list[dict] = []

    def __init__(self, *, access_token=None, client_id=None):
        self.access_token = access_token
        self.client_id = client_id

    @classmethod
    def reset(cls):
        cls.accounts = []

    async def get(self, path, params=None, **kwargs):
        if path == "/api/company/farms/":
            return []
        if path == "/api/accounts/":
            return FakeGraniot.accounts
        return []

    async def post(self, path, json_body=None, params=None, **kwargs):
        if path == "/api/accounts/":
            account = {
                "id": f"acc-{len(FakeGraniot.accounts) + 1}",
                "account_email": (json_body or {}).get("account_email"),
                "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
                "account_access": _jwt(),
            }
            FakeGraniot.accounts.append(account)
            return account
        return {}

    async def delete(self, path, params=None, json_body=None, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniot)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PER_USER_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_AUTOPROVISION_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ALIAS_TEMPLATE", PLANTILLA)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "servicio@graniot.test")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", None)
    FakeGraniot.reset()
    _limpiar()
    yield
    _limpiar()


def _limpiar():
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)
    graniot._cache_delete_prefix(graniot._COMPANY_FARMS_CACHE_KEY)
    with LOCK:
        db = read_db()
        table(db, graniot.EMBED_LINKS_TABLE).clear()
        table(db, "parcels")[:] = [
            row for row in table(db, "parcels") if not str(row.get("id", "")).startswith("cuadre-")
        ]
        db["users"] = [u for u in db.get("users") or [] if u.get("email") != CON_LOTES]
        write_db(db)


def _sembrar_usuario_con_lotes(*, sincronizados: int = 0, en_otra_cuenta: int = 0, pendientes: int = 1) -> dict:
    """Un usuario de Dataris con lotes en los tres estados posibles."""
    user = {"id": "cuadre-user", "email": CON_LOTES, "is_active": True}
    with LOCK:
        db = read_db()
        db.setdefault("users", []).append(user)
        filas = table(db, "parcels")
        indice = 0
        for _ in range(sincronizados):
            indice += 1
            filas.append({
                "id": f"cuadre-lote-{indice}",
                "user_id": user["id"],
                "name": f"Lote {indice}",
                "graniot_parcel_id": 900 + indice,
                "graniot_account_email": graniot._embed_alias_for(None, CON_LOTES),
            })
        for _ in range(en_otra_cuenta):
            indice += 1
            filas.append({
                "id": f"cuadre-lote-{indice}",
                "user_id": user["id"],
                "name": f"Lote {indice}",
                "graniot_parcel_id": 900 + indice,
                "graniot_account_email": "servicio@graniot.test",
            })
        for _ in range(pendientes):
            indice += 1
            filas.append({"id": f"cuadre-lote-{indice}", "user_id": user["id"], "name": f"Lote {indice}"})
        write_db(db)
    return user


def _estado(user: dict, account: dict | None) -> dict:
    parcels = [row for row in table(read_db(), "parcels") if row.get("user_id") == user["id"]]
    return graniot._parcel_status(parcels, account)


# --- Diagnóstico ------------------------------------------------------------


def test_un_lote_sin_subir_cuenta_como_descuadre():
    user = _sembrar_usuario_con_lotes(sincronizados=1, pendientes=2)

    estado = _estado(user, {"account_email": graniot._embed_alias_for(None, CON_LOTES)})

    assert estado["local_parcels"] == 3
    assert estado["synced_parcels"] == 1
    assert estado["parcels_pending_upload"] == 2
    assert estado["parcels_out_of_place"] == 2


def test_un_lote_subido_a_otra_cuenta_tambien_descuadra():
    """Está en Graniot, sí, pero en un portal que el cliente no ve."""
    user = _sembrar_usuario_con_lotes(sincronizados=1, en_otra_cuenta=2, pendientes=0)

    estado = _estado(user, {"account_email": graniot._embed_alias_for(None, CON_LOTES)})

    assert estado["parcels_in_other_account"] == 2
    assert estado["parcels_pending_upload"] == 0
    assert estado["parcels_out_of_place"] == 2


def test_sin_cuenta_propia_no_se_acusa_a_los_lotes_de_estar_en_otro_sitio():
    """Sin portal todavía, lo que falta es la cuenta, no mover lotes."""
    user = _sembrar_usuario_con_lotes(sincronizados=0, en_otra_cuenta=2, pendientes=0)

    estado = _estado(user, None)

    assert estado["parcels_in_other_account"] == 0
    assert estado["synced_parcels"] == 2


# --- El cuadre --------------------------------------------------------------


class _Reconciliador:
    """Llama al endpoint saltándose el candado del panel, ya cubierto aparte."""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(graniot, "require_admin_context", lambda *a, **k: {"admin": {}})

    def __call__(self, **payload):
        return asyncio.run(graniot.reconcile_embed_portals(payload=payload))["data"]


@pytest.fixture
def reconciliar(monkeypatch):
    return _Reconciliador(monkeypatch)


def test_el_ensayo_informa_sin_tocar_nada(reconciliar):
    _sembrar_usuario_con_lotes(pendientes=3)

    informe = reconciliar(user_email=CON_LOTES)

    assert informe["dry_run"] is True
    fila = next(u for u in informe["users"] if u["user_email"] == CON_LOTES)
    assert fila["parcels_pending_upload"] == 3
    # Nada creado en Graniot: el ensayo solo mira.
    assert FakeGraniot.accounts == []
    assert graniot._find_embed_link(read_db(), CON_LOTES) is None


def test_el_cuadre_crea_el_portal_y_sube_los_lotes_que_faltaban(reconciliar, monkeypatch):
    _sembrar_usuario_con_lotes(pendientes=2)
    subidos: list[str] = []

    async def _sync(user, parcel_id, payload=None, **kwargs):
        subidos.append(parcel_id)
        return {"ok": True}

    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", _sync)

    informe = reconciliar(user_email=CON_LOTES, dry_run=False)

    fila = informe["users"][0]
    assert len(fila["uploaded"]) == 2
    assert sorted(subidos) == ["cuadre-lote-1", "cuadre-lote-2"]
    # Y el portal al que van esos lotes ya existe.
    assert [a["account_email"] for a in FakeGraniot.accounts] == [graniot._embed_alias_for(None, CON_LOTES)]


def test_los_lotes_de_otra_cuenta_no_se_mueven_sin_pedirlo(reconciliar, monkeypatch):
    _sembrar_usuario_con_lotes(en_otra_cuenta=2, pendientes=0)
    movidos: list[str] = []

    async def _delete(user, local, **kwargs):
        movidos.append(str(local.get("id")))
        return {}

    monkeypatch.setattr(graniot, "delete_parcel_from_graniot", _delete)
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", lambda *a, **k: _noop())

    informe = reconciliar(user_email=CON_LOTES, dry_run=False)

    fila = informe["users"][0]
    assert fila["skipped_in_other_account"] == 2
    assert fila["moved"] == []
    assert movidos == []


async def _noop():
    return {}


def test_con_move_misplaced_el_lote_se_retira_antes_de_recrearlo(reconciliar, monkeypatch):
    _sembrar_usuario_con_lotes(en_otra_cuenta=1, pendientes=0)
    orden: list[str] = []

    async def _delete(user, local, **kwargs):
        orden.append(f"delete:{local.get('id')}")
        # Dejarlo en las dos cuentas duplicaría el lote en los mapas.
        assert kwargs.get("clear_local") is True
        return {}

    async def _sync(user, parcel_id, payload=None, **kwargs):
        orden.append(f"sync:{parcel_id}")
        return {}

    monkeypatch.setattr(graniot, "delete_parcel_from_graniot", _delete)
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", _sync)

    informe = reconciliar(user_email=CON_LOTES, dry_run=False, move_misplaced=True)

    assert orden == ["delete:cuadre-lote-1", "sync:cuadre-lote-1"]
    assert informe["users"][0]["moved"] == ["cuadre-lote-1"]


def test_el_trabajo_se_parte_en_pasadas(reconciliar, monkeypatch):
    """Un cliente con cientos de lotes no cabe en una sola llamada."""
    _sembrar_usuario_con_lotes(pendientes=5)

    async def _sync(user, parcel_id, payload=None, **kwargs):
        return {}

    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", _sync)

    informe = reconciliar(user_email=CON_LOTES, dry_run=False, max_parcels_per_user=2)

    fila = informe["users"][0]
    assert len(fila["uploaded"]) == 2
    assert fila["remaining"] == 3


def test_un_lote_que_falla_no_detiene_a_los_demas(reconciliar, monkeypatch):
    _sembrar_usuario_con_lotes(pendientes=3)

    async def _sync(user, parcel_id, payload=None, **kwargs):
        if parcel_id == "cuadre-lote-2":
            raise RuntimeError("Graniot rechazó la geometría")
        return {}

    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", _sync)

    fila = reconciliar(user_email=CON_LOTES, dry_run=False)["users"][0]

    assert sorted(fila["uploaded"]) == ["cuadre-lote-1", "cuadre-lote-3"]
    assert len(fila["failed"]) == 1


def test_solo_entran_los_usuarios_con_lotes(reconciliar):
    _sembrar_usuario_con_lotes(pendientes=1)

    informe = reconciliar()

    assert all(fila["local_parcels"] > 0 for fila in informe["users"])
    assert CON_LOTES in {fila["user_email"] for fila in informe["users"]}
