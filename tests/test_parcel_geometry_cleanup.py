"""Arreglos de raíz contra polígonos duplicados y contornos de finca.

Cubre dos defensas nuevas en la ingesta de lotes:
  - `_drop_finca_contours`: descarta el polígono que envuelve la finca cuando
    viene como un lote más, sin tocar un lote que apenas contenga a unos pocos.
  - dedupe por GEOMETRÍA en `find_existing_user_parcel`: re-subir el mismo lote
    con otro nombre actualiza el existente en vez de crear un duplicado.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-geom-cleanup-")

from shapely.geometry import mapping, box  # noqa: E402

from app.services.telemetry.parcel_upload import _drop_finca_contours  # noqa: E402
from app.api.routers import compat  # noqa: E402


def _feature(geom, name):
    return {"type": "Feature", "properties": {"name": name}, "geometry": mapping(geom)}


# --- Descarte del contorno de finca -----------------------------------------


def test_descarta_el_contorno_que_envuelve_toda_la_finca():
    # 4 lotes pequeños en cuadrícula + 1 polígono que los envuelve a todos.
    lotes = [
        _feature(box(0, 0, 1, 1), "L1"),
        _feature(box(2, 0, 3, 1), "L2"),
        _feature(box(0, 2, 1, 3), "L3"),
        _feature(box(2, 2, 3, 3), "L4"),
    ]
    contorno = _feature(box(-0.5, -0.5, 3.5, 3.5), "Contorno finca")
    kept = _drop_finca_contours(lotes + [contorno])
    nombres = {f["properties"]["name"] for f in kept}
    assert "Contorno finca" not in nombres
    assert nombres == {"L1", "L2", "L3", "L4"}


def test_conserva_un_lote_que_solo_contiene_unos_pocos():
    # Un lote grande que contiene 1 de 4 (una despoblación dentro) NO es contorno.
    lote_con_hueco = _feature(box(0, 0, 2, 2), "La Isla 18")
    despoblacion = _feature(box(0.5, 0.5, 1, 1), "Despoblacion 1")
    otros = [
        _feature(box(5, 5, 6, 6), "La Isla 19"),
        _feature(box(7, 7, 8, 8), "La Isla 20"),
    ]
    kept = _drop_finca_contours([lote_con_hueco, despoblacion] + otros)
    nombres = {f["properties"]["name"] for f in kept}
    # Nada se descarta: ningún polígono envuelve al 70% de los demás.
    assert nombres == {"La Isla 18", "Despoblacion 1", "La Isla 19", "La Isla 20"}


def test_no_toca_archivos_pequenos():
    dos = [_feature(box(0, 0, 1, 1), "A"), _feature(box(0, 0, 1, 1), "B")]
    assert _drop_finca_contours(dos) == dos


# --- Dedupe por geometría ----------------------------------------------------


def _parcel_row(user_id, name, geom):
    return compat.normalize_record_geometries("parcels", {
        "id": name,
        "user_id": user_id,
        "name": name,
        "geometry": {"type": "FeatureCollection", "features": [_feature(geom, name)]},
    })


def test_resubir_mismo_lote_con_otro_nombre_no_duplica():
    uid = "user-1"
    existente = _parcel_row(uid, "1190", box(0, 0, 1, 1))
    rows = [existente]
    # Mismo polígono, nombre distinto (como '1190.' tras una re-subida).
    entrante = _parcel_row(uid, "1190.", box(0, 0, 1, 1))
    entrante["id"] = "otro-id"
    match = compat.find_existing_user_parcel(rows, entrante, uid)
    assert match is existente, "debería reconocer el mismo lote por geometría"


def test_lotes_distintos_no_se_confunden():
    uid = "user-1"
    rows = [_parcel_row(uid, "A", box(0, 0, 1, 1))]
    entrante = _parcel_row(uid, "B", box(10, 10, 11, 11))
    entrante["id"] = "b-id"
    assert compat.find_existing_user_parcel(rows, entrante, uid) is None


def test_no_cruza_entre_usuarios():
    rows = [_parcel_row("user-1", "A", box(0, 0, 1, 1))]
    entrante = _parcel_row("user-2", "A2", box(0, 0, 1, 1))
    entrante["id"] = "a2"
    assert compat.find_existing_user_parcel(rows, entrante, "user-2") is None
