"""Verificación de que un registro se tomó dentro de la parcela.

Es la respuesta al requisito de veracidad: el sello se calcula en el servidor,
contra la geometría real de la parcela, y no se puede falsear desde el cliente.

La prueba punto-en-polígono va implementada a mano (ray casting) en lugar de
usar shapely: son treinta líneas, evita cargar el stack geoespacial en las
rutas calientes de la API y el módulo sigue funcionando en los despliegues
recortados de dependencias que ya existen en este repo.
"""

from __future__ import annotations

from typing import Any, Iterable

# Margen de tolerancia en grados (~30 m en el ecuador). Un GPS de teléfono en
# campo abierto ronda los 5-15 m, y un técnico que camina el borde del lote
# marcaría "fuera" sin este margen.
DEFAULT_TOLERANCE_DEGREES = 0.0003


def _rings_from_geometry(geometry: Any) -> Iterable[list[list[float]]]:
    """Extrae los anillos exteriores de cualquier envoltorio GeoJSON razonable."""
    if not isinstance(geometry, dict):
        return []

    geo_type = geometry.get("type")

    if geo_type == "FeatureCollection":
        rings: list[list[list[float]]] = []
        for feature in geometry.get("features") or []:
            rings.extend(_rings_from_geometry(feature))
        return rings

    if geo_type == "Feature":
        return _rings_from_geometry(geometry.get("geometry"))

    if geo_type == "GeometryCollection":
        rings = []
        for item in geometry.get("geometries") or []:
            rings.extend(_rings_from_geometry(item))
        return rings

    coordinates = geometry.get("coordinates")
    if not coordinates:
        return []

    if geo_type == "Polygon":
        # Solo el anillo exterior: los huecos interiores de una parcela agrícola
        # son casos raros y tratarlos como excluyentes generaría falsos "fuera".
        return [coordinates[0]] if coordinates else []

    if geo_type == "MultiPolygon":
        return [polygon[0] for polygon in coordinates if polygon]

    return []


def _point_in_ring(lng: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray casting estándar sobre un anillo en orden [lng, lat]."""
    inside = False
    count = len(ring)
    if count < 3:
        return False

    j = count - 1
    for i in range(count):
        try:
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
        except (TypeError, ValueError, IndexError):
            j = i
            continue

        intersects = ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i

    return inside


def _ring_bounds(ring: list[list[float]]) -> tuple[float, float, float, float] | None:
    xs, ys = [], []
    for point in ring:
        try:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def point_in_geometry(
    lat: float,
    lng: float,
    geometry: Any,
    *,
    tolerance: float = DEFAULT_TOLERANCE_DEGREES,
) -> bool:
    """¿Cae el punto dentro de la geometría, con margen de tolerancia?"""
    rings = list(_rings_from_geometry(geometry))
    if not rings:
        return False

    for ring in rings:
        if _point_in_ring(lng, lat, ring):
            return True

    # Fuera de los anillos: se acepta si está dentro del margen del envolvente.
    # Cubre el caso del técnico parado justo en el linde con GPS impreciso.
    if tolerance > 0:
        for ring in rings:
            bounds = _ring_bounds(ring)
            if not bounds:
                continue
            min_x, min_y, max_x, max_y = bounds
            if (min_x - tolerance) <= lng <= (max_x + tolerance) and (
                min_y - tolerance
            ) <= lat <= (max_y + tolerance):
                return True

    return False


def verify_location(location: Any, geometry: Any) -> bool | None:
    """Sello de verificación de una ubicación capturada.

    Devuelve None cuando no se puede afirmar nada —sin coordenadas, ubicación
    escrita a mano o parcela sin geometría—, para no marcar como sospechoso un
    registro que simplemente no traía GPS.
    """
    if not isinstance(location, dict):
        return None
    if location.get("source") == "manual":
        return None

    lat = location.get("lat", location.get("latitude"))
    lng = location.get("lng", location.get("longitude", location.get("lon")))
    if lat is None or lng is None:
        return None

    try:
        lat_value = float(lat)
        lng_value = float(lng)
    except (TypeError, ValueError):
        return None

    if not geometry:
        return None

    return point_in_geometry(lat_value, lng_value, geometry)
