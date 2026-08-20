"""Una parcela de Graniot de otro país no puede «pertenecer» a un lote.

Visto el 20 ago 2026: lotes de Veracruz tenían como «subparcela» una parcela
de Costa Rica que entró por el emparejado por NOMBRE (``"1" in "Lote 1 CR"``).
El mapa encuadraba las dos imágenes, se alejaba hasta medio continente y la
capa satelital del lote quedaba de un píxel: «cargó, se alejó el mapa y no se
ve la capa».
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-lejanas-")

from app.api.routers import graniot  # noqa: E402


def square(lon: float, lat: float, size: float = 0.002) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat],
    ]]}


VERACRUZ = (-96.43, 19.36)
COSTA_RICA = (-84.42, 9.52)
LOCAL = {"id": "lote-1", "name": "1", "geometry": square(*VERACRUZ)}


def feature(fid: int, name: str, lonlat: tuple, with_geometry: bool = True) -> dict:
    lon, lat = lonlat
    return {
        "id": fid,
        "geometry": square(lon, lat) if with_geometry else None,
        "properties": {"name": name, "key": f"key-{fid}", "image_url": "BBOX=0,0,1,1&WIDTH=1&HEIGHT=1"},
    }


def source_at(fid: str, lonlat: tuple) -> dict:
    lon, lat = lonlat
    return {
        "graniot_parcel_id": fid,
        "graniot_wms_access_key": f"k-{fid}",
        "graniot_wms_url": f"https://app.graniot.com/api/wms/?access_key=k-{fid}",
        "graniot_bbox": [lon, lat, lon + 0.002, lat + 0.002],
    }


def test_el_emparejado_por_nombre_exige_cercania():
    # Ninguna se solapa con el lote (la de Veracruz está a 1 km), así que la
    # geometría no decide y se cae al nombre: solo la cercana puede entrar.
    cerca = feature(1, "Lote 1 norte", (VERACRUZ[0] + 0.01, VERACRUZ[1]))
    lejos = feature(2, "Lote 1 CR", COSTA_RICA)
    matches = graniot._find_graniot_matches_for_local(LOCAL, {"type": "FeatureCollection", "features": [lejos, cerca]})
    assert [m["id"] for m in matches] == [1]


def test_sin_geometria_en_graniot_el_nombre_sigue_valiendo():
    sin_geom = feature(3, "Lote 1", COSTA_RICA, with_geometry=False)
    matches = graniot._find_graniot_matches_for_local(LOCAL, {"type": "FeatureCollection", "features": [sin_geom]})
    assert [m["id"] for m in matches] == [3]


def test_al_pintar_se_descarta_la_fuente_de_otro_pais():
    warnings: list = []
    kept = graniot._drop_sources_far_from_lot(LOCAL, [source_at("a", VERACRUZ), source_at("b", COSTA_RICA)], warnings)
    assert [s["graniot_parcel_id"] for s in kept] == ["a"]
    assert warnings and "lejos" in warnings[0]


def test_si_todas_estan_lejos_no_se_deja_el_mapa_en_blanco():
    warnings: list = []
    sources = [source_at("b", COSTA_RICA)]
    assert graniot._drop_sources_far_from_lot(LOCAL, sources, warnings) == sources
    assert warnings == []


def test_sin_geometria_local_no_se_descarta_nada():
    sources = [source_at("a", VERACRUZ), source_at("b", COSTA_RICA)]
    assert graniot._drop_sources_far_from_lot({"id": "x", "name": "x"}, sources, []) == sources
