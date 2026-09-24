"""La carga de lotes pasó del perfil del cliente al panel de administración.

Cubre las dos mitades del cambio:

* el cliente ya no puede dar de alta ni borrar sus lotes (ni por los endpoints
  de carga ni por el API genérico de tablas), y
* las cuentas del panel de Dataris (las de la lista blanca) sí pueden hacerlo
  para cualquier empresa, sin ningún permiso por fila. Los lotes son de la
  empresa: los ve todo su equipo.
  Los permisos por fila antiguos (`can_manage_parcels`, `can_manage_all_parcels`)
  ya no abren nada por sí solos, tampoco las rutas de Graniot.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-admin-parcels-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}
# Las cuentas del panel son las @dataris-test.com (ver la lista blanca de abajo);
# los clientes, de un dominio que la lista no cubre.
CLIENT_DOMAIN = "cliente-final.com"


def polygon(offset: float = 0.0) -> dict:
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


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _panel_allowlist_para_pruebas():
    # Las cuentas @dataris-test.com hacen de equipo de Dataris: se amplía la
    # lista blanca del panel para ellas. El candado en sí se prueba en
    # test_admin_panel_allowlist.py.
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
def admin_token(client: TestClient) -> str:
    return _sign_in(client, SUPERADMIN["email"], SUPERADMIN["password"])


def _create_user(
    client: TestClient,
    admin_token: str,
    *,
    email: str,
    company_id: str | None = None,
    admin_role: str = "company_user",
) -> str:
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": email,
            "password": "Lotes2026!",
            "first_name": "Usuario",
            "last_name": "Prueba",
            "company_id": company_id,
            "admin_role": admin_role,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["user"]["id"]


def _create_company(client: TestClient, admin_token: str, name: str) -> str:
    response = client.post(
        "/api/compat/tables/companies/insert",
        headers=_auth(admin_token),
        json={"data": {"name": name, "max_hectares": 10000}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _grant_parcel_permission(admin_user_email: str, *, global_scope: bool) -> None:
    """Marca los permisos antiguos directamente en el almacén."""
    with compat.LOCK:
        db = compat.read_db()
        user = next(u for u in db["users"] if u.get("email") == admin_user_email)
        row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == user["id"])
        row[compat.PARCEL_MANAGER_FIELD] = True
        row[compat.PARCEL_MANAGER_ALL_FIELD] = global_scope
        compat.write_db(db)


def _parcels_of(client: TestClient, token: str) -> list[dict]:
    response = client.post("/api/compat/tables/parcels/query", headers=_auth(token), json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _client_email(prefix: str = "cliente") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@{CLIENT_DOMAIN}"


# --- El cliente ya no gestiona sus lotes ----------------------------------


def test_el_cliente_no_puede_crear_lotes(client: TestClient, admin_token: str):
    email = _client_email()
    _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    manual = client.post(
        "/api/compat/parcels/create-manual",
        headers=_auth(token),
        json={"name": "Lote propio", "geometry": polygon(0.10)},
    )
    assert manual.status_code == 403
    assert "equipo de Dataris" in manual.json()["detail"]

    inserted = client.post(
        "/api/compat/tables/parcels/insert",
        headers=_auth(token),
        json={"data": {"name": "Lote por tabla", "geometry": polygon(0.11)}},
    )
    assert inserted.status_code == 403


def test_el_cliente_no_puede_borrar_sus_lotes(client: TestClient, admin_token: str):
    email = _client_email()
    user_id = _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"user_id": user_id, "name": "Lote del cliente", "geometry": polygon(0.12)},
    )
    assert created.status_code == 200, created.text
    parcel_id = created.json()["data"]["parcel"]["id"]

    removed = client.post(
        "/api/compat/tables/parcels/delete",
        headers=_auth(token),
        json={"filters": [{"column": "id", "op": "eq", "value": parcel_id}]},
    )
    assert removed.status_code == 403
    assert [p["id"] for p in _parcels_of(client, token)] == [parcel_id]


def test_el_cliente_no_puede_concederse_el_permiso(client: TestClient, admin_token: str):
    email = _client_email()
    user_id = _create_user(client, admin_token, email=email)
    token = _sign_in(client, email, "Lotes2026!")

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(token),
        json={
            "data": {compat.PARCEL_MANAGER_FIELD: True, compat.PARCEL_MANAGER_ALL_FIELD: True},
            "filters": [{"column": "user_id", "op": "eq", "value": user_id}],
        },
    )
    assert response.status_code == 403

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(token))
    assert context.status_code == 200
    assert context.json()["data"]["allowed"] is False


# --- El equipo de Dataris sí ----------------------------------------------


def test_el_superadmin_carga_lotes_para_una_empresa(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Empresa Lotes {uuid.uuid4().hex[:6]}")
    titular_email = _client_email("titular")
    titular_id = _create_user(client, admin_token, email=titular_email, company_id=company_id, admin_role="company_admin")
    colega_email = _client_email("colega")
    _create_user(client, admin_token, email=colega_email, company_id=company_id)
    ajeno_email = _client_email("ajeno")
    _create_user(client, admin_token, email=ajeno_email, company_id=_create_company(client, admin_token, f"Otra {uuid.uuid4().hex[:6]}"))

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"company_id": company_id, "name": "Lote administrado", "geometry": polygon(0.20)},
    )
    assert created.status_code == 200, created.text
    parcel = created.json()["data"]["parcel"]
    assert parcel["company_id"] == company_id
    # A nombre del titular: es la cuenta con la que la empresa vive en Graniot.
    assert parcel["user_id"] == titular_id
    assert parcel["area"] > 0

    # Lo ve toda la empresa, aunque ninguno lo haya cargado, y nadie de fuera.
    for email in (titular_email, colega_email):
        assert [p["id"] for p in _parcels_of(client, _sign_in(client, email, "Lotes2026!"))] == [parcel["id"]]
    assert _parcels_of(client, _sign_in(client, ajeno_email, "Lotes2026!")) == []

    listed = client.get(
        "/api/compat/admin/parcels/company",
        headers=_auth(admin_token),
        params={"company_id": company_id},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()["data"]
    assert body["company"]["parcel_count"] == 1
    assert body["company"]["member_count"] == 2
    assert body["company"]["owner"]["id"] == titular_id
    assert [p["id"] for p in body["parcels"]] == [parcel["id"]]

    companies = client.get("/api/compat/admin/parcels/companies", headers=_auth(admin_token))
    assert companies.status_code == 200, companies.text
    resumen = next(c for c in companies.json()["data"]["companies"] if c["id"] == company_id)
    assert resumen["parcel_count"] == 1 and resumen["total_area"] > 0

    removed = client.post(
        "/api/compat/admin/parcels/delete",
        headers=_auth(admin_token),
        json={"company_id": company_id, "ids": [parcel["id"]]},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["count"] == 1
    assert _parcels_of(client, _sign_in(client, colega_email, "Lotes2026!")) == []


def test_cargar_indicando_un_usuario_lo_guarda_en_su_empresa(client: TestClient, admin_token: str):
    # Compatibilidad con el panel anterior: si solo llega `user_id`, el lote
    # va a la empresa de ese usuario.
    company_id = _create_company(client, admin_token, f"Empresa U {uuid.uuid4().hex[:6]}")
    user_id = _create_user(client, admin_token, email=_client_email(), company_id=company_id)

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"user_id": user_id, "name": "Lote por usuario", "geometry": polygon(0.21)},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["parcel"]["company_id"] == company_id


def test_una_empresa_sin_usuarios_no_admite_lotes(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Vacia {uuid.uuid4().hex[:6]}")
    response = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"company_id": company_id, "name": "Sin dueño", "geometry": polygon(0.22)},
    )
    assert response.status_code == 409
    assert "ningún usuario activo" in response.json()["detail"]


def test_los_lotes_anteriores_siguen_siendo_de_su_usuario(client: TestClient, admin_token: str):
    # Los lotes cargados antes del cambio no llevan company_id: hasta migrarlos,
    # solo los ve su usuario y el panel los cuenta aparte.
    company_id = _create_company(client, admin_token, f"Empresa L {uuid.uuid4().hex[:6]}")
    dueno_email = _client_email("dueno")
    dueno_id = _create_user(client, admin_token, email=dueno_email, company_id=company_id)
    colega_email = _client_email("colega")
    _create_user(client, admin_token, email=colega_email, company_id=company_id)

    legacy_id = str(uuid.uuid4())
    with compat.LOCK:
        db = compat.read_db()
        compat.table(db, "parcels").append(
            compat.normalize_record_geometries(
                "parcels",
                {"id": legacy_id, "user_id": dueno_id, "name": "Lote viejo", "geometry": polygon(0.23), "created_at": compat.now()},
            )
        )
        compat.write_db(db)

    assert [p["id"] for p in _parcels_of(client, _sign_in(client, dueno_email, "Lotes2026!"))] == [legacy_id]
    assert _parcels_of(client, _sign_in(client, colega_email, "Lotes2026!")) == []

    listed = client.get("/api/compat/admin/parcels/list", headers=_auth(admin_token), params={"user_id": dueno_id})
    assert [p["id"] for p in listed.json()["data"]["parcels"]] == [legacy_id]

    detalle = client.get("/api/compat/admin/parcels/company", headers=_auth(admin_token), params={"company_id": company_id})
    empresa = detalle.json()["data"]["company"]
    assert empresa["parcel_count"] == 0
    assert empresa["legacy_parcel_count"] == 1
    assert empresa["legacy_user_count"] == 1


def test_borrar_un_usuario_no_se_lleva_los_lotes_de_la_empresa(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")
    titular_id = _create_user(client, admin_token, email=_client_email("titular"), company_id=company_id, admin_role="company_admin")
    colega_email = _client_email("colega")
    colega_id = _create_user(client, admin_token, email=colega_email, company_id=company_id)

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"company_id": company_id, "name": "Lote compartido", "geometry": polygon(0.24)},
    )
    parcel_id = created.json()["data"]["parcel"]["id"]

    removed = client.delete(f"/api/compat/auth/admin/users/{titular_id}", headers=_auth(admin_token))
    assert removed.status_code == 200, removed.text
    assert [p["id"] for p in _parcels_of(client, _sign_in(client, colega_email, "Lotes2026!"))] == [parcel_id]
    # El lote pasa al nuevo titular, para seguir teniendo cuenta en Graniot.
    db = compat.read_db(force_refresh=True)
    row = next(r for r in compat.table(db, "parcels") if r.get("id") == parcel_id)
    assert row["user_id"] == colega_id


def test_el_listado_de_usuarios_incluye_a_los_gestionables(client: TestClient, admin_token: str):
    user_id = _create_user(client, admin_token, email=_client_email())

    response = client.get("/api/compat/admin/parcels/users", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["scope"] == "all"
    assert user_id in {user["id"] for user in data["users"]}


def test_una_cuenta_del_panel_gestiona_lotes_de_cualquier_empresa(client: TestClient, admin_token: str):
    # Sin ningún permiso marcado y sin ser administradora: basta con estar en la
    # lista blanca del panel.
    company_a = _create_company(client, admin_token, f"Empresa A {uuid.uuid4().hex[:6]}")
    company_b = _create_company(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")

    comercial_email = f"comercial-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(client, admin_token, email=comercial_email, company_id=company_a)
    comercial_token = _sign_in(client, comercial_email, "Lotes2026!")

    ajeno_id = _create_user(client, admin_token, email=_client_email("ajeno"), company_id=company_b)

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(comercial_token))
    data = context.json()["data"]
    assert data["allowed"] is True
    assert data["scope"] == "all"
    assert data["admin_role"] == "company_user"

    users = client.get("/api/compat/admin/parcels/users", headers=_auth(comercial_token))
    assert users.status_code == 200, users.text
    assert ajeno_id in {user["id"] for user in users.json()["data"]["users"]}

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(comercial_token),
        json={"user_id": ajeno_id, "name": "Lote de otra empresa", "geometry": polygon(0.31)},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["parcel"]["user_id"] == ajeno_id


def test_los_permisos_por_fila_ya_no_abren_la_gestion_de_lotes(client: TestClient, admin_token: str):
    # Fuera de la lista blanca, los permisos antiguos no sirven por ninguna vía.
    # Incluye las rutas de Graniot «en nombre de otro usuario», que antes solo
    # miraban el permiso de la fila (hallazgo C-001).
    comercial_email = _client_email("comercial")
    _create_user(client, admin_token, email=comercial_email)
    _grant_parcel_permission(comercial_email, global_scope=True)
    token = _sign_in(client, comercial_email, "Lotes2026!")

    cliente_id = _create_user(client, admin_token, email=_client_email())

    context = client.get("/api/compat/admin/parcels/context", headers=_auth(token))
    assert context.json()["data"]["allowed"] is False

    users = client.get("/api/compat/admin/parcels/users", headers=_auth(token))
    assert users.status_code == 403

    manual = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(token),
        json={"user_id": cliente_id, "name": "Lote ajeno", "geometry": polygon(0.40)},
    )
    assert manual.status_code == 403

    target = client.get(
        "/api/graniot/parcels/sync-target",
        headers=_auth(token),
        params={"user_id": cliente_id},
    )
    assert target.status_code == 403, target.text

    unsync = client.delete(
        f"/api/graniot/parcels/sync-local/{uuid.uuid4()}",
        headers=_auth(token),
        params={"user_id": cliente_id},
    )
    assert unsync.status_code == 403, unsync.text


def test_no_se_borran_lotes_de_otra_empresa(client: TestClient, admin_token: str):
    company_a = _create_company(client, admin_token, f"Empresa A {uuid.uuid4().hex[:6]}")
    company_b = _create_company(client, admin_token, f"Empresa B {uuid.uuid4().hex[:6]}")
    _create_user(client, admin_token, email=_client_email("a"), company_id=company_a)
    _create_user(client, admin_token, email=_client_email("b"), company_id=company_b)

    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"company_id": company_a, "name": "Lote intacto", "geometry": polygon(0.50)},
    )
    parcel_id = created.json()["data"]["parcel"]["id"]

    response = client.post(
        "/api/compat/admin/parcels/delete",
        headers=_auth(admin_token),
        json={"company_id": company_b, "ids": [parcel_id]},
    )
    assert response.status_code == 404

    listed = client.get(
        "/api/compat/admin/parcels/company",
        headers=_auth(admin_token),
        params={"company_id": company_a},
    )
    assert [p["id"] for p in listed.json()["data"]["parcels"]] == [parcel_id]


def test_un_admin_de_empresa_no_asciende_a_nadie(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Empresa C {uuid.uuid4().hex[:6]}")
    company_admin_email = f"admin-empresa-{uuid.uuid4().hex[:8]}@dataris-test.com"
    _create_user(
        client,
        admin_token,
        email=company_admin_email,
        company_id=company_id,
        admin_role="company_admin",
    )
    company_admin_token = _sign_in(client, company_admin_email, "Lotes2026!")

    comercial_id = _create_user(client, admin_token, email=_client_email("comercial"), company_id=company_id)

    response = client.post(
        "/api/compat/tables/admin_users/update",
        headers=_auth(company_admin_token),
        json={
            "data": {
                compat.PARCEL_MANAGER_FIELD: True,
                compat.PARCEL_MANAGER_ALL_FIELD: True,
                "admin_role": "superadmin",
            },
            "filters": [{"column": "user_id", "op": "eq", "value": comercial_id}],
        },
    )
    assert response.status_code == 200, response.text

    db = compat.read_db(force_refresh=True)
    row = next(r for r in compat.table(db, "admin_users") if r.get("user_id") == comercial_id)
    # El rol no sube y los permisos marcados no le abren la gestión de lotes.
    assert row["admin_role"] == "company_user"
    assert compat.can_manage_parcels(db, comercial_id) is False


def test_el_alta_sin_lista_de_modulos_hereda_el_paquete_de_la_empresa(client: TestClient, admin_token: str):
    # Antes, un alta sin `modules` bloqueaba de forma explícita todo el paquete
    # de la empresa y el usuario nacía sin módulos.
    company_id = _create_company(client, admin_token, f"Empresa D {uuid.uuid4().hex[:6]}")
    paquete = client.put(
        f"/api/compat/admin/module-access/companies/{company_id}",
        headers=_auth(admin_token),
        json={"modules": {"satelite": True, "telemetria": True}},
    )
    assert paquete.status_code == 200, paquete.text

    hereda_id = _create_user(client, admin_token, email=_client_email("hereda"), company_id=company_id)
    detalle = client.get(f"/api/compat/admin/module-access/users/{hereda_id}", headers=_auth(admin_token))
    modulos = {m["id"]: m for m in detalle.json()["data"]["modules"]}
    for module_id in ("satelite", "telemetria"):
        assert modulos[module_id]["override"] is None
        assert modulos[module_id]["effective"] is True

    # Con lista explícita se respeta: lo que no se marca queda bloqueado.
    response = client.post(
        "/api/compat/admin/users/manual",
        headers=_auth(admin_token),
        json={
            "email": _client_email("elige"),
            "password": "Lotes2026!",
            "company_id": company_id,
            "admin_role": "company_user",
            "modules": ["satelite"],
        },
    )
    assert response.status_code == 200, response.text
    elige_id = response.json()["data"]["user"]["id"]
    detalle = client.get(f"/api/compat/admin/module-access/users/{elige_id}", headers=_auth(admin_token))
    modulos = {m["id"]: m for m in detalle.json()["data"]["modules"]}
    assert modulos["satelite"]["effective"] is True
    assert modulos["telemetria"]["effective"] is False


# --- Migración de los lotes cargados por usuario ------------------------------


def _legacy_parcel(user_id: str, name: str, geometry: dict, **extra) -> str:
    parcel_id = extra.pop("id", None) or str(uuid.uuid4())
    with compat.LOCK:
        db = compat.read_db()
        compat.table(db, "parcels").append(
            compat.normalize_record_geometries(
                "parcels",
                {"id": parcel_id, "user_id": user_id, "name": name, "geometry": geometry, "created_at": compat.now(), **extra},
            )
        )
        compat.write_db(db)
    return parcel_id


def test_la_migracion_funde_los_lotes_repetidos_y_mueve_sus_referencias(client: TestClient, admin_token: str, monkeypatch):
    from app.api.routers import compat_parcels_admin

    monkeypatch.setattr(compat_parcels_admin, "_sql_mirrored", lambda ids: [])
    company_id = _create_company(client, admin_token, f"Migra {uuid.uuid4().hex[:6]}")
    a_email, b_email = _client_email("a"), _client_email("b")
    a_id = _create_user(client, admin_token, email=a_email, company_id=company_id)
    b_id = _create_user(client, admin_token, email=b_email, company_id=company_id)

    # El mismo lote en las dos cuentas (b lo tiene en Graniot) y uno propio de cada una.
    a_copia = _legacy_parcel(a_id, "Lote común", polygon(0.60))
    b_copia = _legacy_parcel(b_id, "Lote común (b)", polygon(0.60), graniot_parcel_id=99)
    solo_a = _legacy_parcel(a_id, "Solo de A", polygon(0.62))
    solo_b = _legacy_parcel(b_id, "Solo de B", polygon(0.64))
    with compat.LOCK:
        db = compat.read_db()
        compat.table(db, "field_notes").append({"id": str(uuid.uuid4()), "user_id": a_id, "parcel_id": a_copia, "note": "x"})
        compat.write_db(db)

    plan = client.post(
        "/api/compat/admin/parcels/migrate",
        headers=_auth(admin_token),
        json={"company_id": company_id, "owner_user_id": b_id},
    )
    assert plan.status_code == 200, plan.text
    data = plan.json()["data"]
    assert data["dry_run"] is True
    assert (data["lots_before"], data["lots_after"], data["dropped"]) == (4, 3, 1)
    assert data["references_moved"] == {"field_notes": 1}
    # La simulación no cambia nada.
    assert {p["id"] for p in _parcels_of(client, _sign_in(client, a_email, "Lotes2026!"))} == {a_copia, solo_a}

    applied = client.post(
        "/api/compat/admin/parcels/migrate",
        headers=_auth(admin_token),
        json={"company_id": company_id, "owner_user_id": b_id, "dry_run": False},
    )
    assert applied.status_code == 200, applied.text

    # Los dos ven los mismos 3 lotes: la copia que estaba en Graniot se quedó.
    for email in (a_email, b_email):
        assert {p["id"] for p in _parcels_of(client, _sign_in(client, email, "Lotes2026!"))} == {b_copia, solo_a, solo_b}
    db = compat.read_db(force_refresh=True)
    rows = {r["id"]: r for r in compat.table(db, "parcels") if r["id"] in {b_copia, solo_a, solo_b}}
    assert all(r["company_id"] == company_id for r in rows.values())
    # Lo que ya está en Graniot sigue a nombre de su cuenta; lo demás, del titular.
    assert rows[b_copia]["user_id"] == b_id and rows[solo_a]["user_id"] == b_id
    nota = next(n for n in compat.table(db, "field_notes") if n.get("note") == "x" and n.get("user_id") == a_id)
    assert nota["parcel_id"] == b_copia
    company = next(c for c in compat.table(db, "companies") if c["id"] == company_id)
    assert company[compat.COMPANY_PARCEL_OWNER_FIELD] == b_id

    listed = client.get("/api/compat/admin/parcels/company", headers=_auth(admin_token), params={"company_id": company_id})
    empresa = listed.json()["data"]["company"]
    assert (empresa["parcel_count"], empresa["legacy_parcel_count"]) == (3, 0)
    assert empresa["owner"]["id"] == b_id and empresa["owner_pinned"] is True


def test_la_migracion_no_borra_copias_que_usa_la_bitacora(client: TestClient, admin_token: str, monkeypatch):
    from app.api.routers import compat_parcels_admin

    company_id = _create_company(client, admin_token, f"Bitacora {uuid.uuid4().hex[:6]}")
    a_id = _create_user(client, admin_token, email=_client_email("a"), company_id=company_id)
    b_id = _create_user(client, admin_token, email=_client_email("b"), company_id=company_id)
    a_copia = _legacy_parcel(a_id, "Común", polygon(0.70))
    _legacy_parcel(b_id, "Común", polygon(0.70), graniot_parcel_id=5)
    monkeypatch.setattr(compat_parcels_admin, "_sql_mirrored", lambda ids: [a_copia] if a_copia in ids else [])

    response = client.post(
        "/api/compat/admin/parcels/migrate",
        headers=_auth(admin_token),
        json={"company_id": company_id, "dry_run": False},
    )
    assert response.status_code == 409
    assert "Bitácora" in response.json()["detail"]
    db = compat.read_db(force_refresh=True)
    assert any(r["id"] == a_copia and not r.get("company_id") for r in compat.table(db, "parcels"))


def test_el_titular_fijado_manda_sobre_el_correo_de_la_empresa(client: TestClient, admin_token: str):
    company_id = _create_company(client, admin_token, f"Titular {uuid.uuid4().hex[:6]}")
    _create_user(client, admin_token, email=_client_email("admin"), company_id=company_id, admin_role="company_admin")
    otro_id = _create_user(client, admin_token, email=_client_email("otro"), company_id=company_id)

    fijado = client.post(
        "/api/compat/admin/parcels/owner",
        headers=_auth(admin_token),
        json={"company_id": company_id, "user_id": otro_id},
    )
    assert fijado.status_code == 200, fijado.text
    created = client.post(
        "/api/compat/admin/parcels/manual",
        headers=_auth(admin_token),
        json={"company_id": company_id, "name": "Del titular fijado", "geometry": polygon(0.80)},
    )
    assert created.json()["data"]["parcel"]["user_id"] == otro_id

    ajeno_id = _create_user(client, admin_token, email=_client_email("ajeno"), company_id=_create_company(client, admin_token, f"X {uuid.uuid4().hex[:6]}"))
    rechazo = client.post(
        "/api/compat/admin/parcels/owner",
        headers=_auth(admin_token),
        json={"company_id": company_id, "user_id": ajeno_id},
    )
    assert rechazo.status_code == 400


def test_rehome_pasa_a_la_cuenta_del_titular_los_lotes_que_viven_en_otra(client: TestClient, admin_token: str, monkeypatch):
    from app.api.routers import compat_parcels_admin

    subidos = []
    monkeypatch.setattr(
        compat_parcels_admin,
        "schedule_graniot_parcel_sync",
        lambda bg, user, rows, **kw: subidos.append((user["id"], [r["id"] for r in rows])),
    )
    company_id = _create_company(client, admin_token, f"Rehome {uuid.uuid4().hex[:6]}")
    titular_id = _create_user(client, admin_token, email=_client_email("titular"), company_id=company_id, admin_role="company_admin")
    otro_id = _create_user(client, admin_token, email=_client_email("otro"), company_id=company_id)
    ajeno = _legacy_parcel(otro_id, "En otra cuenta", polygon(0.90), company_id=company_id,
                           graniot_parcel_id=7, graniot_account_email="otro@graniot")
    propio = _legacy_parcel(titular_id, "Ya del titular", polygon(0.92), company_id=company_id, graniot_parcel_id=8)

    plan = client.post("/api/compat/admin/parcels/rehome", headers=_auth(admin_token), json={"company_id": company_id})
    assert plan.status_code == 200, plan.text
    assert [p["id"] for p in plan.json()["data"]["parcels"]] == [ajeno]
    assert subidos == []

    hecho = client.post("/api/compat/admin/parcels/rehome", headers=_auth(admin_token), json={"company_id": company_id, "dry_run": False})
    assert hecho.status_code == 200, hecho.text
    db = compat.read_db(force_refresh=True)
    row = next(r for r in compat.table(db, "parcels") if r["id"] == ajeno)
    assert row["user_id"] == titular_id
    assert not row.get("graniot_parcel_id")
    assert row["graniot_previous_account_email"] == "otro@graniot"
    assert subidos == [(titular_id, [ajeno])]
    assert next(r for r in compat.table(db, "parcels") if r["id"] == propio)["graniot_parcel_id"] == 8
