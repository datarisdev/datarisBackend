"""Los lotes son de la empresa, no de cada usuario.

Un lote con `company_id` lo ve todo el equipo de esa empresa y vive en la cuenta
de Graniot de su titular (el usuario a cuyo nombre se guarda). Los lotes
cargados antes del cambio no llevan `company_id` y siguen siendo solo de su
usuario hasta que se migren.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from fastapi import HTTPException

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-lotes-empresa-")

from app.api.routers import compat  # noqa: E402
from app.api.routers import graniot  # noqa: E402
from app.api.routers import sentinel2  # noqa: E402
from app.api.routers import weather  # noqa: E402

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-90.5, 14.5], [-90.5, 14.51], [-90.49, 14.51], [-90.49, 14.5], [-90.5, 14.5]]],
}


def _state() -> dict:
    """Empresa A con titular, colega y un tercero sin rol; empresa B con uno."""
    return {
        "users": [
            {"id": "titular", "email": "titular@a.com", "created_at": "2026-01-02"},
            {"id": "colega", "email": "colega@a.com", "created_at": "2026-01-01"},
            {"id": "ajeno", "email": "ajeno@b.com", "created_at": "2026-01-01"},
        ],
        "tables": {
            "companies": [
                {"id": "A", "name": "Empresa A", "email": "titular@a.com"},
                {"id": "B", "name": "Empresa B"},
            ],
            "admin_users": [
                {"user_id": "titular", "company_id": "A", "admin_role": "company_admin", "is_active": True, "created_at": "2026-01-02"},
                {"user_id": "colega", "company_id": "A", "admin_role": "company_user", "is_active": True, "created_at": "2026-01-01"},
                {"user_id": "ajeno", "company_id": "B", "admin_role": "company_admin", "is_active": True},
            ],
            "profiles": [],
            "parcels": [
                {"id": "empresa", "company_id": "A", "user_id": "titular", "name": "Lote 1", "geometry": SQUARE},
                {"id": "viejo", "user_id": "colega", "name": "Lote 2", "geometry": SQUARE},
                {"id": "de-b", "company_id": "B", "user_id": "ajeno", "name": "Lote 1", "geometry": SQUARE},
            ],
        },
    }


def _ids(rows) -> set:
    return {row["id"] for row in rows}


def test_cada_usuario_ve_los_lotes_de_su_empresa_y_los_suyos_sin_migrar():
    db = _state()
    assert _ids(compat.visible_parcels(db, "titular")) == {"empresa"}
    assert _ids(compat.visible_parcels(db, "colega")) == {"empresa", "viejo"}
    assert _ids(compat.visible_parcels(db, "ajeno")) == {"de-b"}
    assert _ids(compat.scoped_table_rows(db, "parcels", {"id": "colega"})) == {"empresa", "viejo"}


def test_los_hijos_de_un_lote_de_empresa_se_ven_desde_la_empresa():
    db = _state()
    db["tables"]["field_notes"] = [{"id": "nota", "parcel_id": "empresa", "user_id": "titular"}]
    assert _ids(compat.scoped_table_rows(db, "field_notes", {"id": "colega"})) == {"nota"}
    assert compat.scoped_table_rows(db, "field_notes", {"id": "ajeno"}) == []


def test_el_mismo_nombre_en_dos_empresas_no_se_funde():
    rows = compat.dedupe_user_parcels(_state()["tables"]["parcels"])
    assert _ids(rows) == {"empresa", "viejo", "de-b"}


def test_una_resubida_actualiza_el_lote_de_la_empresa_y_no_el_de_otra():
    rows = _state()["tables"]["parcels"]
    entrante = {"company_id": "A", "user_id": "titular", "name": "Lote 1"}
    assert compat.find_existing_user_parcel(rows, entrante, "titular")["id"] == "empresa"
    # El lote personal del colega con otro nombre no se toca, ni el de B.
    assert compat.find_existing_user_parcel(rows, {"company_id": "A", "user_id": "titular", "name": "Lote 2"}, "titular") is None


def test_titular_es_el_usuario_del_correo_de_la_empresa():
    db = _state()
    assert compat.company_parcel_owner(db, "A")["id"] == "titular"
    # Sin correo de empresa: su primer administrador.
    assert compat.company_parcel_owner(db, "B")["id"] == "ajeno"
    db["tables"]["companies"][0]["email"] = None
    db["tables"]["admin_users"][0]["admin_role"] = "company_user"
    # Sin administrador de empresa: el alta más antigua del equipo.
    assert compat.company_parcel_owner(db, "A")["id"] == "colega"
    assert compat.company_parcel_owner(db, "Z") is None


def test_graniot_encuentra_el_lote_para_un_colega_y_opera_como_el_titular():
    db = _state()
    local = graniot._visible_local_parcel(db, "empresa", {"id": "colega"})
    assert local["id"] == "empresa"
    assert graniot._lot_owner_in(db, local, {"id": "colega"})["id"] == "titular"
    assert graniot._visible_local_parcel(db, "empresa", {"id": "ajeno"}) is None


def test_sincronizar_desde_un_colega_usa_la_cuenta_del_titular(monkeypatch):
    db = _state()
    monkeypatch.setattr(graniot, "read_db", lambda *a, **k: db)
    seen = {}

    async def fake_target(user, local, operation=None):
        seen["user"] = user["id"]
        raise HTTPException(status_code=418, detail="parar aquí")

    monkeypatch.setattr(graniot, "_sync_target_for_row", fake_target)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(graniot.sync_local_parcel_to_graniot({"id": "colega", "email": "colega@a.com"}, "empresa"))
    assert exc.value.status_code == 418
    assert seen["user"] == "titular"


def test_sentinel_y_clima_abren_los_lotes_de_la_empresa(monkeypatch):
    db = _state()
    monkeypatch.setattr(compat, "read_db", lambda *a, **k: db)
    assert sentinel2._get_owned_parcel(None, "empresa", {"id": "colega"})["id"] == "empresa"
    assert weather._owned_parcel("empresa", {"id": "colega"})["id"] == "empresa"
    with pytest.raises(HTTPException):
        sentinel2._get_owned_parcel(None, "empresa", {"id": "ajeno"})
    with pytest.raises(HTTPException):
        weather._owned_parcel("viejo", {"id": "titular"})


def test_el_mismo_nombre_en_otra_finca_de_la_empresa_es_otro_lote():
    rows = [
        {"id": "isla", "company_id": "A", "user_id": "titular", "name": "Lote 5", "finca": "La Isla"},
        {"id": "jose", "company_id": "A", "user_id": "titular", "name": "Lote 5", "finca": "San José"},
        # Re-subida en la misma finca: sí es el mismo lote (gana la más reciente).
        {"id": "isla-v2", "company_id": "A", "user_id": "titular", "name": "lote 5", "finca": "La Isla", "updated_at": "2026-09-24"},
    ]
    assert _ids(compat.dedupe_user_parcels(rows)) == {"jose", "isla-v2"}
    entrante = {"company_id": "A", "user_id": "titular", "name": "Lote 5", "finca": "San José"}
    assert compat.find_existing_user_parcel(rows, entrante, "titular")["id"] == "jose"


def test_los_lotes_personales_siguen_deduplicando_por_nombre():
    rows = [
        {"id": "a", "user_id": "u", "name": "Lote 5", "finca": "La Isla"},
        {"id": "b", "user_id": "u", "name": "Lote 5", "finca": "San José", "updated_at": "2026-09-24"},
    ]
    assert _ids(compat.dedupe_user_parcels(rows)) == {"b"}


# --- Portal de Graniot por empresa --------------------------------------------


def test_el_equipo_de_una_empresa_con_lotes_de_empresa_comparte_el_portal_del_titular():
    db = _state()
    assert compat.company_portal_user(db, {"id": "colega"})["id"] == "titular"
    assert compat.company_portal_user(db, {"id": "titular"})["id"] == "titular"


def test_sin_lotes_de_empresa_cada_uno_conserva_su_portal():
    db = _state()
    db["tables"]["parcels"] = [p for p in db["tables"]["parcels"] if p.get("company_id") != "A"]
    assert compat.company_portal_user(db, {"id": "colega"})["id"] == "colega"
    # Sin empresa, tampoco cambia nada.
    assert compat.company_portal_user(db, {"id": "suelto"})["id"] == "suelto"


def test_con_titular_fijado_se_comparte_aunque_aun_no_haya_lotes():
    db = _state()
    db["tables"]["parcels"] = []
    db["tables"]["companies"][0][compat.COMPANY_PARCEL_OWNER_FIELD] = "colega"
    assert compat.company_portal_user(db, {"id": "titular"})["id"] == "colega"


def test_el_portal_se_busca_con_el_correo_del_titular(monkeypatch):
    db = _state()
    monkeypatch.setattr(graniot, "read_db", lambda *a, **k: db)
    monkeypatch.setattr(graniot.settings, "GRANIOT_EMBED_PER_USER_ENABLED", True)
    monkeypatch.setattr(graniot, "_embed_service_account_emails", lambda: set())
    buscados = []

    async def fake_platform_user(email):
        buscados.append(email)
        return None

    async def fake_find(email, **kwargs):
        return None

    monkeypatch.setattr(graniot, "_platform_user_for_email", fake_platform_user)
    monkeypatch.setattr(graniot, "_find_embed_account", fake_find)
    asyncio.run(graniot._embed_account_for_user({"id": "colega", "email": "colega@a.com"}))
    assert buscados == ["titular@a.com"]


def test_al_dar_de_alta_a_un_colega_no_se_le_crea_cuenta_en_graniot(monkeypatch):
    db = _state()
    monkeypatch.setattr(compat, "read_db", lambda *a, **k: db)
    llamadas = []

    async def fake_ensure(user, provisioned_by=None):
        llamadas.append(user["id"])
        return {"provisioned": True}

    monkeypatch.setattr(graniot, "ensure_embed_account_for_user", fake_ensure)
    asyncio.run(compat._graniot_ensure_embed_account_task({"id": "colega", "email": "colega@a.com"}))
    asyncio.run(compat._graniot_ensure_embed_account_task({"id": "titular", "email": "titular@a.com"}))
    assert llamadas == ["titular"]
