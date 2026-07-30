"""Verificación de que un registro se tomó dentro de la parcela."""

from __future__ import annotations

from app.modules.field_log.geo import point_in_geometry, verify_location

# Cuadrado de ~1 km de lado cerca de Villadiego, en orden GeoJSON [lng, lat].
SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [-101.0100, 20.0100],
            [-101.0000, 20.0100],
            [-101.0000, 20.0200],
            [-101.0100, 20.0200],
            [-101.0100, 20.0100],
        ]
    ],
}


class TestPointInGeometry:
    def test_point_inside(self):
        assert point_in_geometry(20.0150, -101.0050, SQUARE) is True

    def test_point_far_outside(self):
        assert point_in_geometry(20.5000, -101.5000, SQUARE) is False

    def test_point_just_outside_is_accepted_within_tolerance(self):
        """Un GPS de teléfono tiene metros de error.

        Marcar como sospechoso a un técnico parado en el linde del lote sería
        castigar al usuario honesto por la imprecisión del aparato.
        """
        assert point_in_geometry(20.0201, -101.0050, SQUARE) is True

    def test_tolerance_can_be_disabled(self):
        assert point_in_geometry(20.0201, -101.0050, SQUARE, tolerance=0) is False

    def test_multipolygon_and_feature_wrappers(self):
        multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]}
        feature = {"type": "Feature", "geometry": SQUARE, "properties": {}}
        collection = {"type": "FeatureCollection", "features": [feature]}

        for geometry in (multi, feature, collection):
            assert point_in_geometry(20.0150, -101.0050, geometry) is True


class TestVerifyLocation:
    def test_gps_inside_is_verified(self):
        assert verify_location({"lat": 20.015, "lng": -101.005}, SQUARE) is True

    def test_gps_outside_is_rejected(self):
        assert verify_location({"lat": 21.0, "lng": -102.0}, SQUARE) is False

    def test_manual_location_is_not_judged(self):
        """Una ubicación escrita a mano no afirma nada sobre estar en campo."""
        assert (
            verify_location({"lat": 20.015, "lng": -101.005, "source": "manual"}, SQUARE)
            is None
        )

    def test_missing_coordinates_or_geometry_return_unknown(self):
        assert verify_location(None, SQUARE) is None
        assert verify_location({}, SQUARE) is None
        assert verify_location({"lat": 20.0, "lng": -101.0}, None) is None

    def test_alternative_coordinate_keys(self):
        assert verify_location({"latitude": 20.015, "longitude": -101.005}, SQUARE) is True

    def test_non_numeric_coordinates_return_unknown(self):
        assert verify_location({"lat": "sin señal", "lng": "-101"}, SQUARE) is None
