"""Lectura del almacén COMPAT para reflejar parcelas.

Estas pruebas cubren la parte del puente que no necesita base de datos: si la
parcela no se localiza en el JSON, la Bitácora vuelve a responder «Parcela no
encontrada» a lotes que el usuario está viendo en pantalla, que es exactamente
el fallo que este módulo existe para evitar.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import compat_mirror


PARCEL_ID = "3f4a2b10-9c8d-4e7f-9a1b-2c3d4e5f6a7b"
OWNER_ID = "11111111-2222-3333-4444-555555555555"

STATE = {
    "users": [{"id": OWNER_ID, "email": "tecnico@campo.test", "password_hash": "x"}],
    "tables": {
        "parcels": [
            {
                "id": PARCEL_ID,
                "user_id": OWNER_ID,
                "name": "Lote El Sauce",
                "area": 12.5,
                "geometry": {"type": "Polygon", "coordinates": []},
                "geometry_geojson": {"type": "FeatureCollection", "features": []},
            },
            {"id": str(uuid.uuid4()), "user_id": OWNER_ID, "name": "Otro lote", "area": 3.2},
        ]
    },
}


@pytest.fixture()
def compat_state(monkeypatch):
    monkeypatch.setattr(compat_mirror, "_compat_state", lambda: STATE)


class TestLocalizarLaParcela:
    def test_encuentra_la_parcela_por_id(self, compat_state):
        assert compat_mirror.find_compat_parcel(PARCEL_ID)["name"] == "Lote El Sauce"

    def test_acepta_un_uuid_ademas_de_texto(self, compat_state):
        """El id llega como UUID desde la ruta y como str desde el JSON."""
        assert compat_mirror.find_compat_parcel(uuid.UUID(PARCEL_ID)) is not None

    def test_una_parcela_ajena_al_almacen_no_aparece(self, compat_state):
        assert compat_mirror.find_compat_parcel(uuid.uuid4()) is None

    def test_encuentra_al_dueno_para_la_clave_foranea(self, compat_state):
        assert compat_mirror.find_compat_user(OWNER_ID)["email"] == "tecnico@campo.test"


class TestReflejo:
    def test_copia_nombre_superficie_y_dueno(self, compat_state):
        row = compat_mirror.find_compat_parcel(PARCEL_ID)
        parcel = compat_mirror._parcel_from_compat(row, uuid.UUID(OWNER_ID))

        assert parcel.name == "Lote El Sauce"
        assert parcel.area == pytest.approx(12.5)
        assert str(parcel.user_id) == OWNER_ID

    def test_prefiere_la_geometria_normalizada(self, compat_state):
        """`geometry_geojson` es la que se compara contra el GPS del registro."""
        row = compat_mirror.find_compat_parcel(PARCEL_ID)
        parcel = compat_mirror._parcel_from_compat(row, uuid.UUID(OWNER_ID))

        assert parcel.geometry["type"] == "FeatureCollection"

    def test_una_parcela_sin_geometria_no_rompe_la_columna_not_null(self, compat_state):
        parcel = compat_mirror._parcel_from_compat({"id": PARCEL_ID}, uuid.UUID(OWNER_ID))

        assert parcel.geometry == {}
        assert parcel.name == "Parcela sin nombre"


class TestNombresParaLaLista:
    def test_devuelve_solo_los_pedidos(self, compat_state):
        wanted = uuid.UUID(PARCEL_ID)
        names = compat_mirror.compat_parcel_names([wanted])

        assert names == {wanted: "Lote El Sauce"}

    def test_sin_ids_no_toca_el_almacen(self):
        assert compat_mirror.compat_parcel_names([]) == {}


class TestAlmacenCaido:
    def test_un_fallo_del_almacen_no_tumba_la_api(self, monkeypatch):
        """Sin este resguardo, un COMPAT inaccesible daría 500 en toda la bitácora."""
        import app.api.routers.compat as compat

        def explota(*args, **kwargs):
            raise RuntimeError("Neon no responde")

        monkeypatch.setattr(compat, "read_db", explota)

        assert compat_mirror._compat_state() == {}
        assert compat_mirror.find_compat_parcel(PARCEL_ID) is None
