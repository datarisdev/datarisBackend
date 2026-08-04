"""El portal satelital propio de cada usuario, incluso sin cuenta de API.

Graniot separa a los usuarios de plataforma (dueños de las fincas, invisibles
para ``/api/accounts/``) de los usuarios de mapa embebido (los únicos que el
iframe acepta). Antes, emparejar por correo contra ``/api/accounts/`` solo
encontraba a las cuentas dadas de alta como API y el resto de clientes acababa
mirando el portal de la cuenta de servicio, con fincas ajenas.

Aquí se cubre la vía que Graniot confirma como única: censar a los dueños de
finca en ``/api/company/farms/``, dar de alta su cuenta embebida y asignarle sus
fincas.
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
os.environ.setdefault("DATARIS_COMPAT_STORAGE_DIR", tempfile.mkdtemp(prefix="dataris-compat-embed-"))

from app.api.routers import graniot  # noqa: E402
from app.api.routers.compat import LOCK, read_db, table, write_db  # noqa: E402
from app.services import graniot_embed_accounts as embed_accounts  # noqa: E402
from app.services.graniot_client import GraniotAPIError  # noqa: E402

CLIENTE = "cliente@sumagro.mx"
ALIAS = "dataris-embed+1635@dataris.es"


def _jwt(*, minutes: int = 60, user_id: int | None = 1635) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"token_type": "access", "exp": int(time.time()) + minutes * 60}
    if user_id is not None:
        payload["id"] = user_id
    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64(payload)}.sig"


def _expired_jwt() -> str:
    return _jwt(minutes=-5)


def _company_farm(farm_id: int, name: str, responsibles: list[dict]) -> dict:
    return {"id": farm_id, "name": name, "responsibles": responsibles, "area": 12.5}


def _account_ids(body) -> list[str]:
    """Cuentas de un cuerpo de gestores, con la forma que exige Graniot.

    El propio error de la API lo dicta: ``The request has to have a list like
    [{"account_id": "manager1"}, {"account_id": "manager2"}]``.
    """
    if not isinstance(body, list) or not all(
        isinstance(item, dict) and item.get("account_id") for item in body
    ):
        raise GraniotAPIError(
            400,
            'The request has to have a list like [{"account_id": "manager1"}]',
        )
    return [str(item["account_id"]) for item in body]


class FakeGraniot:
    """Graniot en memoria: cuentas embebidas, censo de fincas y vínculos."""

    company_farms: list[dict] = []
    accounts: list[dict] = []
    managers: dict[int, list[dict]] = {}
    calls: list[dict] = []
    link_failures: dict[int, Exception] = {}
    create_error: Exception | None = None

    def __init__(self, *, access_token=None, client_id=None):
        self.access_token = access_token
        self.client_id = client_id

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.managers = {}
        cls.link_failures = {}
        cls.create_error = None
        cls.company_farms = [
            _company_farm(3615, "Agropecuaria Salado", [
                {"id": 1635, "email": CLIENTE, "name": "Cliente"},
                {"id": 1387, "email": "servicio@dataris.es", "name": "Servicio"},
            ]),
            _company_farm(3616, "El Retiro", [{"id": 1635, "email": CLIENTE, "name": "Cliente"}]),
        ]
        cls.accounts = []

    async def get(self, path, params=None, **kwargs):
        FakeGraniot.calls.append({"method": "GET", "path": path, "params": params})
        if path == "/api/company/farms/":
            return FakeGraniot.company_farms
        if path == "/api/accounts/":
            return FakeGraniot.accounts
        if path.endswith("/managers/"):
            farm_id = int(path.split("/")[3])
            return FakeGraniot.managers.get(farm_id, [])
        return []

    async def post(self, path, json_body=None, params=None, **kwargs):
        FakeGraniot.calls.append({"method": "POST", "path": path, "json_body": json_body, "params": params})
        if path == "/api/accounts/":
            if FakeGraniot.create_error:
                raise FakeGraniot.create_error
            account = {
                "id": "acc-nueva",
                "account_email": (json_body or {}).get("account_email"),
                "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
                "account_access": _jwt(),
            }
            FakeGraniot.accounts.append(account)
            return account
        if path.endswith("/managers/"):
            farm_id = int(path.split("/")[3])
            failure = FakeGraniot.link_failures.get(farm_id)
            if failure:
                raise failure
            for account_id in _account_ids(json_body):
                # Graniot suma gestores sin tocar a los que ya estaban, y
                # repetir el alta no duplica nada.
                entries = FakeGraniot.managers.setdefault(farm_id, [])
                if not any(entry["account_id"] == account_id for entry in entries):
                    entries.append({"key": f"mgr-{farm_id}-{len(entries)}", "account_id": account_id})
            # Responde con la lista completa de gestores de la finca.
            return FakeGraniot.managers.get(farm_id, [])
        return {}

    async def delete(self, path, params=None, json_body=None, **kwargs):
        FakeGraniot.calls.append({"method": "DELETE", "path": path, "params": params, "json_body": json_body})
        if path.endswith("/managers/"):
            farm_id = int(path.split("/")[3])
            fuera = set(_account_ids(json_body))
            FakeGraniot.managers[farm_id] = [
                entry for entry in FakeGraniot.managers.get(farm_id, [])
                if entry["account_id"] not in fuera
            ]
        return None


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setattr(graniot, "GraniotClient", FakeGraniot)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PER_USER_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_AUTOPROVISION_ENABLED", True)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ALIAS_TEMPLATE", "dataris-embed+{uid}@dataris.es")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_ACCOUNT_EMAIL", "servicio@graniot.test")
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_USERNAME", None)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_FARM_SYNC_TTL_SECONDS", 6 * 3600)
    FakeGraniot.reset()
    _limpiar_estado()
    yield
    _limpiar_estado()


def _limpiar_estado():
    graniot._cache_delete_prefix(graniot._EMBED_ACCOUNTS_CACHE_KEY)
    graniot._cache_delete_prefix(graniot._COMPANY_FARMS_CACHE_KEY)
    with LOCK:
        db = read_db()
        table(db, graniot.EMBED_LINKS_TABLE).clear()
        write_db(db)


def _link_guardado(email: str = CLIENTE) -> dict | None:
    return graniot._find_embed_link(read_db(), email)


# --- Censo de dueños de finca ----------------------------------------------


def test_el_censo_identifica_a_los_usuarios_de_plataforma_con_su_id_numerico():
    index = embed_accounts.index_platform_users(FakeGraniot.company_farms)

    assert set(index) == {CLIENTE, "servicio@dataris.es"}
    # El id numérico es justo lo que Graniot acepta como client_id.
    assert index[CLIENTE]["user_id"] == 1635
    assert [farm["id"] for farm in index[CLIENTE]["farms"]] == [3615, 3616]
    assert len(index["servicio@dataris.es"]["farms"]) == 1


def test_el_censo_ignora_fincas_sin_responsables():
    index = embed_accounts.index_platform_users([_company_farm(1, "Huérfana", [])])

    assert index == {}


def test_el_alias_lleva_dentro_el_id_para_reconstruir_el_vinculo():
    alias = embed_accounts.embed_alias(CLIENTE, 1635, "dataris-embed+{uid}@dataris.es")

    assert alias == ALIAS
    assert embed_accounts.platform_user_id_from_alias(alias, "dataris-embed+{uid}@dataris.es") == "1635"
    assert embed_accounts.platform_user_id_from_alias(CLIENTE, "dataris-embed+{uid}@dataris.es") is None


# --- Alta del portal --------------------------------------------------------


def test_el_usuario_sin_cuenta_de_api_recibe_su_propio_portal_con_sus_fincas():
    portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert portal is not None
    assert portal["account_email"] == ALIAS
    assert portal["embedded_url"].startswith("https://embed.graniot.com/?auth_id=")
    # Las dos fincas del usuario de plataforma quedan asignadas a su cuenta.
    assert sorted(FakeGraniot.managers) == [3615, 3616]
    assert all(
        entry["account_id"] == "acc-nueva"
        for entries in FakeGraniot.managers.values()
        for entry in entries
    )
    link = _link_guardado()
    assert link["account_id"] == "acc-nueva"
    assert link["platform_user_id"] == 1635
    assert link["farm_ids"] == ["3615", "3616"]


def test_asignar_una_finca_no_desaloja_a_sus_gestores_anteriores():
    # Graniot recibe la lista de gestores de la finca: mandar solo la cuenta
    # nueva dejaría sin acceso a los que ya la tenían.
    FakeGraniot.managers = {3615: [{"key": "mgr-previo", "account_id": "acc-de-otro"}]}

    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    gestores = [entry["account_id"] for entry in FakeGraniot.managers[3615]]
    assert "acc-de-otro" in gestores
    assert "acc-nueva" in gestores


def test_retirar_una_finca_se_pide_con_la_misma_lista_del_alta():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    quitada = asyncio.run(embed_accounts.unlink_farm_from_account(FakeGraniot(), 3615, "acc-nueva"))

    assert quitada is True
    assert [entry["account_id"] for entry in FakeGraniot.managers[3615]] == []
    baja = next(c for c in FakeGraniot.calls if c["method"] == "DELETE" and c["path"].endswith("/managers/"))
    assert baja["json_body"] == [{"account_id": "acc-nueva"}]


def test_un_alta_que_graniot_dice_aceptar_pero_no_asigna_cuenta_como_fallo():
    class _MienteAlAsignar(FakeGraniot):
        async def post(self, path, json_body=None, params=None, **kwargs):
            if path.endswith("/managers/"):
                # 2xx pero sin la cuenta entre los gestores: la finca no se vería.
                return []
            return await FakeGraniot.post(self, path, json_body=json_body, params=params, **kwargs)

    graniot.GraniotClient = _MienteAlAsignar
    try:
        result = asyncio.run(graniot._provision_embed_account(CLIENTE))
    finally:
        graniot.GraniotClient = FakeGraniot

    assert result["farms"]["linked"] == []
    assert len(result["farms"]["errors"]) == 2
    assert _link_guardado()["farms_synced_at"] is None


def test_quien_no_tiene_fincas_en_graniot_no_estrena_portal_vacio():
    portal = asyncio.run(graniot._embed_account_for_user({"id": "u2", "email": "nadie@example.com"}))

    # Sin fincas no hay nada que enseñar: se deja el fallback de siempre.
    assert portal is None
    assert FakeGraniot.accounts == []
    assert _link_guardado("nadie@example.com") is None


def test_no_se_crea_nada_dos_veces():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    altas = [c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"] == "/api/accounts/"]
    vinculos = [c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"].endswith("/managers/")]

    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert len([c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"] == "/api/accounts/"]) == len(altas)
    assert len([c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"].endswith("/managers/")]) == len(vinculos)


def test_una_finca_que_falla_no_deja_al_usuario_sin_las_demas():
    FakeGraniot.link_failures = {3615: GraniotAPIError(403, "sin permiso")}

    result = asyncio.run(graniot._provision_embed_account(CLIENTE))

    assert result["provisioned"] is True
    assert result["farms"]["linked"] == [3616]
    assert result["farms"]["errors"][0]["farm_id"] == 3615
    # Queda constancia de que la sincronización no está completa.
    assert _link_guardado()["farms_synced_at"] is None
    assert _link_guardado()["last_error"]


def test_el_kill_switch_de_aprovisionamiento_deja_el_comportamiento_anterior(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_AUTOPROVISION_ENABLED", False)

    portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert portal is None
    assert FakeGraniot.accounts == []


def test_graniot_caido_no_rompe_el_mapa(monkeypatch):
    class _Roto(FakeGraniot):
        async def get(self, path, params=None, **kwargs):
            raise RuntimeError("Graniot no responde")

    monkeypatch.setattr(graniot, "GraniotClient", _Roto)

    assert asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE})) is None


def test_si_la_cuenta_ya_existia_en_graniot_se_reutiliza():
    # Graniot ya tenía el alias dado de alta, pero nuestra caché no lo veía.
    FakeGraniot.create_error = GraniotAPIError(400, "An account with this email already exists.")
    FakeGraniot.accounts = [{
        "id": "acc-previa",
        "account_email": ALIAS,
        "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
        "account_access": _jwt(),
    }]

    portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert portal["account_email"] == ALIAS
    assert _link_guardado()["account_id"] == "acc-previa"


def test_un_alias_desactivado_no_deja_al_usuario_sin_portal():
    # Dar de baja una cuenta embebida en Graniot no la borra: la desactiva, y a
    # partir de ahí ese correo ni se lista ni se puede volver a dar de alta.
    desactivado = GraniotAPIError(
        400,
        "An account with this email already exists but is deactivated. "
        "It can be reactivated or permanently deleted from the Graniot app.",
    )
    intentos: list[str] = []

    class _AliasQuemado(FakeGraniot):
        async def post(self, path, json_body=None, params=None, **kwargs):
            if path == "/api/accounts/":
                alias = (json_body or {}).get("account_email")
                intentos.append(alias)
                if alias == ALIAS:
                    raise desactivado
            return await FakeGraniot.post(self, path, json_body=json_body, params=params, **kwargs)

    graniot.GraniotClient = _AliasQuemado
    try:
        portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    finally:
        graniot.GraniotClient = FakeGraniot

    assert intentos == [ALIAS, "dataris-embed+1635-1@dataris.es"]
    assert portal["account_email"] == "dataris-embed+1635-1@dataris.es"
    # El alias alternativo sigue diciendo de quién es.
    assert embed_accounts.platform_user_id_from_alias(
        portal["account_email"], "dataris-embed+{uid}@dataris.es"
    ) == "1635"


def test_si_todos_los_alias_estan_quemados_se_cae_al_portal_de_siempre():
    class _TodoQuemado(FakeGraniot):
        async def post(self, path, json_body=None, params=None, **kwargs):
            if path == "/api/accounts/":
                raise GraniotAPIError(400, "An account with this email already exists.")
            return await FakeGraniot.post(self, path, json_body=json_body, params=params, **kwargs)

    graniot.GraniotClient = _TodoQuemado
    try:
        portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    finally:
        graniot.GraniotClient = FakeGraniot

    assert portal is None
    # Queda registrado el motivo para que se pueda arreglar desde Graniot.
    assert "already exists" in _link_guardado()["last_error"]


def test_la_cuenta_dada_de_alta_a_mano_con_el_correo_del_usuario_tiene_prioridad():
    FakeGraniot.accounts = [{
        "id": "acc-manual",
        "account_email": CLIENTE,
        "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
        "account_access": _jwt(),
    }]

    portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert portal["account_email"] == CLIENTE
    # No se da de alta ninguna cuenta nueva por encima de la que ya existía.
    assert [c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"] == "/api/accounts/"] == []


def test_el_vinculo_guardado_manda_aunque_el_correo_no_coincida():
    graniot._save_embed_link(CLIENTE, {
        "account_id": "acc-vinculada",
        "account_email": "otro-alias@dataris.es",
        "platform_user_id": 1635,
        "farm_ids": ["3615", "3616"],
        "farms_synced_at": graniot.now(),
    })
    FakeGraniot.accounts = [{
        "id": "acc-vinculada",
        "account_email": "otro-alias@dataris.es",
        "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
        "account_access": _jwt(),
    }]

    portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    assert portal["account_email"] == "otro-alias@dataris.es"


def test_un_token_vencido_en_cache_se_relee_antes_de_rendirse():
    # Graniot renueva el auth_id en cada lectura: lo vencido suele ser la caché.
    vencida = {
        "id": "acc-nueva",
        "account_email": ALIAS,
        "embedded_url": f"https://embed.graniot.com/?auth_id={_expired_jwt()}",
        "account_access": _expired_jwt(),
    }
    FakeGraniot.accounts = [vencida]
    graniot._save_embed_link(CLIENTE, {
        "account_id": "acc-nueva",
        "account_email": ALIAS,
        "platform_user_id": 1635,
        "farm_ids": ["3615", "3616"],
        "farms_synced_at": graniot.now(),
    })
    graniot._cache_set(graniot._EMBED_ACCOUNTS_CACHE_KEY, [vencida], 60)

    def _relectura(path, params=None, **kwargs):
        FakeGraniot.accounts = [{**vencida, "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}"}]
        return FakeGraniot.accounts

    class _Renovando(FakeGraniot):
        async def get(self, path, params=None, **kwargs):
            if path == "/api/accounts/":
                return _relectura(path)
            return await FakeGraniot.get(self, path, params=params, **kwargs)

    graniot.GraniotClient = _Renovando
    try:
        portal = asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    finally:
        graniot.GraniotClient = FakeGraniot

    assert portal is not None
    assert not graniot._embed_token_looks_expired(graniot._embed_url_auth_token(portal["embedded_url"]))


def test_dos_pestanas_a_la_vez_no_dan_de_alta_dos_cuentas():
    async def _dos_a_la_vez():
        return await asyncio.gather(
            graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}),
            graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}),
        )

    portales = asyncio.run(_dos_a_la_vez())

    altas = [c for c in FakeGraniot.calls if c["method"] == "POST" and c["path"] == "/api/accounts/"]
    assert len(altas) == 1
    assert all(portal is not None for portal in portales)


def test_un_alta_lenta_no_deja_el_mapa_esperando(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PROVISION_TIMEOUT_SECONDS", 0.01)

    class _Lenta(FakeGraniot):
        async def post(self, path, json_body=None, params=None, **kwargs):
            if path == "/api/accounts/":
                await asyncio.sleep(0.05)
            return await FakeGraniot.post(self, path, json_body=json_body, params=params, **kwargs)

    monkeypatch.setattr(graniot, "GraniotClient", _Lenta)

    async def _abrir_y_esperar_al_alta():
        portal = await graniot._embed_account_for_user({"id": "u1", "email": CLIENTE})
        pendientes = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pendientes:
            await asyncio.gather(*pendientes)
        return portal

    portal = asyncio.run(_abrir_y_esperar_al_alta())

    # Esta carga usa el portal de siempre, pero el alta termina por su cuenta.
    assert portal is None
    assert _link_guardado()["account_id"] == "acc-nueva"


# --- Fincas que aparecen después -------------------------------------------


def test_una_finca_nueva_reconcilia_el_portal_en_segundo_plano():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    FakeGraniot.company_farms.append(
        _company_farm(3617, "La Ceiba", [{"id": 1635, "email": CLIENTE, "name": "Cliente"}])
    )
    graniot._cache_delete_prefix(graniot._COMPANY_FARMS_CACHE_KEY)
    # Simula que el vínculo lleva más del TTL sin revisarse.
    graniot._save_embed_link(CLIENTE, {"farms_synced_at": "2020-01-01T00:00:00+00:00"})

    async def _abrir_mapa():
        portal = await graniot._embed_account_for_user({"id": "u1", "email": CLIENTE})
        # La reconciliación va en segundo plano: se le da su turno al bucle.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pendientes = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pendientes:
            await asyncio.gather(*pendientes)
        return portal

    portal = asyncio.run(_abrir_mapa())

    assert portal is not None
    assert 3617 in FakeGraniot.managers
    assert _link_guardado()["farm_ids"] == ["3615", "3616", "3617"]


def test_subir_un_lote_a_una_finca_desconocida_marca_el_portal_para_revisar():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    assert _link_guardado()["farms_synced_at"]

    graniot._mark_embed_farms_pending(CLIENTE, 9999)

    assert _link_guardado()["farms_synced_at"] is None
    assert graniot._embed_farm_sync_is_stale(_link_guardado()) is True


def test_una_finca_ya_conocida_no_provoca_trabajo_extra():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))
    sincronizado = _link_guardado()["farms_synced_at"]

    graniot._mark_embed_farms_pending(CLIENTE, 3615)

    assert _link_guardado()["farms_synced_at"] == sincronizado


# --- Subida de lotes --------------------------------------------------------


def test_los_lotes_de_un_usuario_de_plataforma_van_a_su_cuenta_por_client_id():
    target = asyncio.run(graniot._resolve_sync_target(CLIENTE))

    # Sin cuenta embebida todavía, pero Graniot acepta actuar por su id numérico.
    assert target["mode"] == graniot.SYNC_MODE_CLIENT_ID
    assert target["client_id"] == "1635"
    assert target["platform_user_id"] == "1635"
    assert target["reason"] == "platform_user"


def test_con_portal_propio_los_lotes_van_dentro_de_esa_cuenta():
    asyncio.run(graniot._embed_account_for_user({"id": "u1", "email": CLIENTE}))

    target = asyncio.run(graniot._resolve_sync_target(CLIENTE))

    assert target["mode"] == graniot.SYNC_MODE_TOKEN
    assert target["account_email"] == ALIAS
    assert target["access_token"]


def test_sin_fincas_ni_cuenta_no_se_actua_en_nombre_de_nadie():
    target = asyncio.run(graniot._resolve_sync_target("nadie@example.com"))

    assert target["mode"] == graniot.SYNC_MODE_SERVICE
    assert target["reason"] == "no_graniot_account_for_email"
