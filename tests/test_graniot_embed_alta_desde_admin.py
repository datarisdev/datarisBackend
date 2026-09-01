"""El cliente que se da de alta en el panel nace con su portal de Graniot.

Hasta ahora el portal solo se creaba para quien YA era responsable de alguna
finca en Graniot. Un cliente recién dado de alta en Dataris no lo es —sus lotes
los carga el equipo después—, así que se quedaba sin cuenta embebida, y sin ella
el destino de sincronización degrada a la cuenta de servicio: el autosync exige
cuenta propia, no sube nada y sus lotes nunca salen de Dataris.

Aquí se cubre la vía nueva: alias propio derivado de su correo, alta de la
cuenta al crear el usuario y al cargarle lotes, y el nombre con el que aparece
su finca dentro de su propio portal.
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
os.environ.setdefault("DATARIS_COMPAT_STORAGE_DIR", tempfile.mkdtemp(prefix="dataris-compat-embed-admin-"))

from app.api.routers import compat  # noqa: E402
from app.api.routers import graniot  # noqa: E402
from app.api.routers.compat import LOCK, read_db, table, write_db  # noqa: E402
from app.services import graniot_embed_accounts as embed_accounts  # noqa: E402

NUEVO = "nuevo-cliente@agricola.test"
CON_FINCAS = "cliente@sumagro.mx"
PLANTILLA = "dataris-embed+{uid}@dataris.es"


def _jwt(*, minutes: int = 60, user_id: int | None = 1635) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"token_type": "access", "exp": int(time.time()) + minutes * 60}
    if user_id is not None:
        payload["id"] = user_id
    return f"{b64({'typ': 'JWT', 'alg': 'HS256'})}.{b64(payload)}.sig"


class FakeGraniot:
    """Graniot en memoria, con el censo de fincas y las cuentas embebidas."""

    company_farms: list[dict] = []
    accounts: list[dict] = []
    managers: dict[int, list[dict]] = {}
    calls: list[dict] = []

    def __init__(self, *, access_token=None, client_id=None):
        self.access_token = access_token
        self.client_id = client_id

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.managers = {}
        cls.accounts = []
        # El cliente nuevo NO aparece: no es responsable de ninguna finca.
        cls.company_farms = [
            {
                "id": 3615,
                "name": "Agropecuaria Salado",
                "area": 12.5,
                "responsibles": [{"id": 1635, "email": CON_FINCAS, "name": "Cliente"}],
            }
        ]

    async def get(self, path, params=None, **kwargs):
        FakeGraniot.calls.append({"method": "GET", "path": path, "params": params})
        if path == "/api/company/farms/":
            return FakeGraniot.company_farms
        if path == "/api/accounts/":
            return FakeGraniot.accounts
        if path.endswith("/managers/"):
            return FakeGraniot.managers.get(int(path.split("/")[3]), [])
        return []

    async def post(self, path, json_body=None, params=None, **kwargs):
        FakeGraniot.calls.append({"method": "POST", "path": path, "json_body": json_body, "params": params})
        if path == "/api/accounts/":
            account = {
                "id": f"acc-{len(FakeGraniot.accounts) + 1}",
                "account_email": (json_body or {}).get("account_email"),
                "embedded_url": f"https://embed.graniot.com/?auth_id={_jwt()}",
                "account_access": _jwt(),
            }
            FakeGraniot.accounts.append(account)
            return account
        if path.endswith("/managers/"):
            farm_id = int(path.split("/")[3])
            entries = FakeGraniot.managers.setdefault(farm_id, [])
            for item in json_body or []:
                account_id = str(item.get("account_id"))
                if not any(entry["account_id"] == account_id for entry in entries):
                    entries.append({"key": f"mgr-{farm_id}", "account_id": account_id})
            return entries
        return {}

    async def delete(self, path, params=None, json_body=None, **kwargs):
        FakeGraniot.calls.append({"method": "DELETE", "path": path})
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


def _cuentas_creadas() -> list[str]:
    return [str(account["account_email"]) for account in FakeGraniot.accounts]


# --- El alias de quien todavía no existe en Graniot -------------------------


def test_cada_cliente_sin_fincas_recibe_un_alias_propio_y_estable():
    primero = graniot._embed_alias_for(None, NUEVO)
    repetido = graniot._embed_alias_for(None, NUEVO.upper())
    otro = graniot._embed_alias_for(None, "otro@agricola.test")

    # Estable: el mismo correo reconstruye siempre el mismo alias, aunque se
    # pierda el registro local del vínculo.
    assert primero == repetido
    # Y propio: dos clientes sin fincas no pueden acabar compartiendo portal.
    assert primero != otro
    assert primero.startswith("dataris-embed+u") and primero.endswith("@dataris.es")
    # No se confunde con el id numérico de un usuario de plataforma.
    assert embed_accounts.platform_user_id_from_alias(primero, PLANTILLA) is None


def test_el_alias_sigue_llevando_el_id_de_plataforma_cuando_lo_hay():
    alias = graniot._embed_alias_for({"user_id": 1635}, CON_FINCAS)

    assert alias == "dataris-embed+1635@dataris.es"
    assert embed_accounts.platform_user_id_from_alias(alias, PLANTILLA) == "1635"


# --- Alta del portal desde el panel ----------------------------------------


def test_el_cliente_sin_fincas_recibe_su_cuenta_embebida():
    resultado = asyncio.run(
        graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO})
    )

    assert resultado["provisioned"] is True
    assert _cuentas_creadas() == [graniot._embed_alias_for(None, NUEVO)]
    link = graniot._find_embed_link(read_db(), NUEVO)
    assert link["account_id"] == "acc-1"
    # No tiene fincas de plataforma todavía: el portal nace vacío y se llenará
    # con los lotes que el equipo le cargue.
    assert link["farm_ids"] == []
    assert link["platform_user_id"] is None


def test_repetir_el_alta_no_crea_una_segunda_cuenta():
    asyncio.run(graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO}))
    asyncio.run(graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO}))

    assert len(FakeGraniot.accounts) == 1


def test_al_cliente_con_fincas_se_le_asignan_ademas_sus_fincas():
    resultado = asyncio.run(
        graniot.ensure_embed_account_for_user({"id": "u-viejo", "email": CON_FINCAS})
    )

    assert resultado["provisioned"] is True
    assert _cuentas_creadas() == ["dataris-embed+1635@dataris.es"]
    assert [entry["account_id"] for entry in FakeGraniot.managers[3615]] == ["acc-1"]


def test_el_portal_recien_creado_es_el_que_ve_ese_usuario():
    asyncio.run(graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO}))

    portal = asyncio.run(graniot._embed_account_for_user({"id": "u-nuevo", "email": NUEVO}))

    assert portal is not None
    assert portal["account_email"] == graniot._embed_alias_for(None, NUEVO)


def test_un_fallo_de_graniot_no_rompe_el_alta_del_usuario():
    class _Caida(FakeGraniot):
        async def get(self, path, params=None, **kwargs):
            raise RuntimeError("Graniot no responde")

    graniot.GraniotClient = _Caida
    try:
        resultado = asyncio.run(
            graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO})
        )
    finally:
        graniot.GraniotClient = FakeGraniot

    assert resultado["provisioned"] is False
    assert resultado["reason"] == "provision_failed"


def test_la_cuenta_de_servicio_no_recibe_alias():
    resultado = asyncio.run(
        graniot.ensure_embed_account_for_user({"id": "u-srv", "email": "servicio@graniot.test"})
    )

    assert resultado["provisioned"] is False
    assert resultado["reason"] == "service_account"
    assert FakeGraniot.accounts == []


def test_el_kill_switch_apaga_el_alta_automatica(monkeypatch):
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_AUTOPROVISION_ENABLED", False)

    resultado = asyncio.run(
        graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO})
    )

    assert resultado["reason"] == "autoprovision_disabled"
    assert FakeGraniot.accounts == []


# --- Los lotes acaban en la cuenta del cliente ------------------------------


def test_el_lote_del_cliente_recien_creado_va_a_su_propia_cuenta():
    asyncio.run(graniot.ensure_embed_account_for_user({"id": "u-nuevo", "email": NUEVO}))

    destino = asyncio.run(graniot._resolve_sync_target(NUEVO, refresh=True))

    # Sin cuenta propia el destino sería "service" y el autosync no subiría nada.
    assert destino["mode"] != graniot.SYNC_MODE_SERVICE
    assert destino["account_email"] == graniot._embed_alias_for(None, NUEVO)


def test_la_finca_por_defecto_toma_el_nombre_del_cliente():
    with LOCK:
        db = read_db()
        table(db, "profiles").append({
            "id": "u-nombre",
            "user_id": "u-nombre",
            "first_name": "Ana",
            "last_name": "Ríos",
            "company_name": "Agrícola del Valle",
        })
        write_db(db)

    nombre = graniot._owner_farm_name({"id": "u-nombre", "email": NUEVO})

    # El portal del cliente muestra ese nombre como sección: "Dataris" no le
    # diría nada dentro de su propia cuenta.
    assert nombre == "Agrícola del Valle"


def test_sin_ficha_el_nombre_cae_en_el_correo():
    assert graniot._owner_farm_name({"id": "u-sin-ficha", "email": NUEVO}) == "nuevo-cliente"


# --- El enganche con el panel de administración ----------------------------


def test_el_panel_encarga_el_alta_del_portal_al_crear_el_usuario():
    encargos: list[tuple[str, str]] = []

    class _Tareas:
        def add_task(self, func, *args, **kwargs):
            encargos.append((getattr(func, "__name__", str(func)), kwargs.get("provisioned_by")))

    compat.schedule_graniot_embed_account(
        _Tareas(), {"id": "u1", "email": NUEVO}, provisioned_by="admin-user-create"
    )

    assert encargos == [("_graniot_ensure_embed_account_task", "admin-user-create")]


def test_la_carga_de_lotes_pide_asegurar_la_cuenta_antes_de_subir():
    encargos: list[tuple] = []

    class _Tareas:
        def add_task(self, func, *args, **kwargs):
            encargos.append(args)

    compat.schedule_graniot_parcel_sync(
        _Tareas(), {"id": "u1", "email": NUEVO}, [{"id": "lote-1"}], ensure_account=True
    )

    assert encargos == [({"id": "u1", "email": NUEVO}, ["lote-1"], True)]


def test_la_carga_desde_el_perfil_no_cambia_de_conducta():
    encargos: list[tuple] = []

    class _Tareas:
        def add_task(self, func, *args, **kwargs):
            encargos.append(args)

    compat.schedule_graniot_parcel_sync(_Tareas(), {"id": "u1", "email": NUEVO}, [{"id": "lote-1"}])

    assert encargos == [({"id": "u1", "email": NUEVO}, ["lote-1"], False)]


def _crear_usuario_por_el_panel(modules: list[str], *, company_id: str | None = None) -> list[str]:
    """Da de alta un usuario como lo hace el panel y devuelve a quién se encargó portal."""
    from fastapi.testclient import TestClient

    from app.main import app

    encargados: list[str] = []
    original = compat.schedule_graniot_embed_account
    compat.schedule_graniot_embed_account = (
        lambda background, user, **kwargs: encargados.append(str((user or {}).get("email") or ""))
    )
    try:
        client = TestClient(app)
        sesion = client.post(
            "/api/compat/auth/sign-in",
            json={"email": "admin@dataris.local", "password": "admin123456"},
        )
        assert sesion.status_code == 200, sesion.text
        token = sesion.json()["data"]["session"]["access_token"]
        alta = client.post(
            "/api/compat/admin/users/manual",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": f"portal-{len(encargados)}-{os.urandom(4).hex()}@agricola.test",
                "password": "Portal2026!",
                "first_name": "Cliente",
                "last_name": "Nuevo",
                "company_id": company_id,
                "modules": modules,
            },
        )
        assert alta.status_code == 200, alta.text
    finally:
        compat.schedule_graniot_embed_account = original
    return encargados


def _empresa_sin_modulos() -> str:
    """Empresa con paquete vacío: sus usuarios no heredan el módulo satelital."""
    with LOCK:
        db = read_db()
        company_id = f"empresa-sin-satelite-{os.urandom(4).hex()}"
        table(db, "companies").append({"id": company_id, "name": "Sin satélite", "max_hectares": 1000})
        write_db(db)
    return company_id


def test_el_cliente_con_modulo_satelital_recibe_encargo_de_portal():
    assert len(_crear_usuario_por_el_panel(["satelite"])) == 1


def test_la_cuenta_sin_modulo_satelital_no_gasta_una_cuenta_de_graniot():
    """Dar de baja una cuenta embebida quema su alias para siempre.

    Por eso el portal se crea para quien va a abrir el mapa, no para cada
    usuario interno que se da de alta en el panel.
    """
    assert _crear_usuario_por_el_panel([], company_id=_empresa_sin_modulos()) == []
