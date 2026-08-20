"""El auto-sync de map-layer se ejecuta como el usuario autenticado.

Desde el blindaje de roles, `_attempt_auto_sync_for_map_layer` llamaba al
ENDPOINT `sync_local_parcel` como función normal: su parámetro
`user_id = Query(default=None)` llegaba como objeto Query, `_acting_user` lo
tomaba por «en nombre de otro usuario» y respondía 403 a cualquier cliente sin
permiso de gestor. El diálogo mostraba «pide al administrador sincronizarlo».
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-autosync-")

from app.api.routers import graniot  # noqa: E402

USER = {"id": "user-gmateo", "email": "gmateo@dataris.es"}
WMS_URL = "https://app.graniot.com/api/wms/?access_key=eyJwYXJjZWxfa2V5IjoieCJ9:1wwiI7:firma"


def test_sincroniza_como_el_usuario_autenticado_y_devuelve_las_fuentes(monkeypatch):
    calls = []

    async def fake_sync(user, parcel_id, payload=None, **kwargs):
        calls.append((user, parcel_id, payload))
        return {
            "parcel": {"id": parcel_id, "graniot_parcel_id": "900", "graniot_wms_url": WMS_URL, "graniot_wms_access_key": "k"},
            "graniot": None,
        }

    monkeypatch.setattr(graniot, "_require_user", lambda authorization: USER)
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", fake_sync)
    monkeypatch.setattr(graniot, "read_db", lambda **kw: {"parcels": []})

    warnings: list = []
    sources = asyncio.run(graniot._attempt_auto_sync_for_map_layer(
        local_parcel_id="lote-1", authorization="Bearer x", warnings=warnings,
    ))

    assert calls and calls[0][0] is USER, "tiene que sincronizar como el dueño autenticado, no «en nombre de» nadie"
    assert calls[0][1] == "lote-1"
    assert sources and sources[0]["graniot_parcel_id"] == "900"
    assert any("sincronizado automáticamente" in w for w in warnings)


def test_un_fallo_de_sync_explica_el_motivo(monkeypatch):
    from fastapi import HTTPException

    async def failing_sync(user, parcel_id, payload=None, **kwargs):
        raise HTTPException(status_code=503, detail="Graniot no configurado")

    monkeypatch.setattr(graniot, "_require_user", lambda authorization: USER)
    monkeypatch.setattr(graniot, "sync_local_parcel_to_graniot", failing_sync)

    warnings: list = []
    sources = asyncio.run(graniot._attempt_auto_sync_for_map_layer(
        local_parcel_id="lote-1", authorization="Bearer x", warnings=warnings,
    ))
    assert sources == []
    assert warnings and "Graniot no configurado" in warnings[0]
