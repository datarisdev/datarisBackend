from __future__ import annotations

"""Deterministic commercial-demo dataset for DATARIS.

The demo tenant is intentionally isolated from real tenants.  Rows created here
carry ``demo_seed`` and can be rebuilt safely whenever a seller starts a new
presentation.  The seeder mutates the compatibility JSON document only; it does
not call routers or external services, which keeps bootstrap deterministic in
Cloud Run and in local development.
"""

import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List
from urllib.parse import quote

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"

DEMO_TAG = "commercial-demo-v1"
DEMO_EMAIL = os.getenv("DATARIS_DEMO_EMAIL", "demo@dataris.app").strip().lower()
DEMO_PASSWORD = os.getenv("DATARIS_DEMO_PASSWORD", "DatarisDemo2026!")
DEMO_NAMESPACE = uuid.UUID("73668c09-7de2-4930-8958-7be0af5cbe0c")
DEMO_USER_ID = str(uuid.uuid5(DEMO_NAMESPACE, "user:commercial-demo"))
DEMO_COMPANY_ID = str(uuid.uuid5(DEMO_NAMESPACE, "company:agrovision-demo"))

DEMO_MODULES = [
    ("dashboard", "Dashboard", "Centro de control operativo", "LayoutDashboard"),
    ("satelite", "Monitoreo Satelital", "Análisis multitemporal e índices vegetativos", "Satellite"),
    ("mapeo", "Mapeo", "Mapeo y análisis geoespacial de labores", "Map"),
    ("telemetria", "Telemetría", "Indicadores y métricas de maquinaria", "Activity"),
    ("sig-agricola", "SIG Agrícola", "Seguimiento territorial de cosecha, malezas y plagas", "Sprout"),
    ("aplicaciones-aereas", "Aplicaciones Aéreas", "Control de aplicaciones y cobertura", "Plane"),
    ("ortofoto-analysis", "Análisis de ortofotos", "Procesamiento visual de ortomosaicos", "Image"),
    ("personal", "Personal de Campo", "Control biométrico y georreferenciado", "Users"),
    ("alertas", "Alertas inteligentes", "Detección proactiva de riesgos operativos", "Bell"),
    ("digiforms", "DigiformsApp", "Formularios digitales de campo, GPS y evidencia", "FileText"),
]


def _id(name: str) -> str:
    return str(uuid.uuid5(DEMO_NAMESPACE, name))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(days_ago: float = 0, *, hours: float = 0) -> str:
    return (_now() - timedelta(days=days_ago, hours=hours)).isoformat()


def _date(days_ago: int = 0) -> str:
    return (_now() - timedelta(days=days_ago)).date().isoformat()


def _time_at(days_ago: int, hour: int, minute: int = 0) -> str:
    day = (_now() - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).isoformat()


def _tag(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**row, "demo_seed": DEMO_TAG}


def _table(db: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return db.setdefault("tables", {}).setdefault(name, [])


def _bounds(lat: float, lng: float, dlat: float, dlng: float) -> Dict[str, float]:
    return {
        "south": round(lat - dlat, 7),
        "north": round(lat + dlat, 7),
        "west": round(lng - dlng, 7),
        "east": round(lng + dlng, 7),
    }


def _rect_geometry(name: str, lat: float, lng: float, dlat: float, dlng: float) -> Dict[str, Any]:
    ring = [
        [lng - dlng, lat - dlat],
        [lng + dlng, lat - dlat],
        [lng + dlng, lat + dlat],
        [lng - dlng, lat + dlat],
        [lng - dlng, lat - dlat],
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name, "lote": name, "source": "demo"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }


def _line_collection(lat: float, lng: float, dlat: float, dlng: float, *, lines: int = 9) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    for index in range(lines):
        ratio = (index + 1) / (lines + 1)
        y = lat - dlat + 2 * dlat * ratio
        features.append(
            {
                "type": "Feature",
                "properties": {"line": index + 1, "coverage": "aplicada"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lng - dlng * 0.88, y], [lng + dlng * 0.88, y]],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _load_real_geodata(filename: str) -> Any:
    path = _DATA_DIR / filename
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _translate_offsets(offsets: List[List[float]], target_lat: float, target_lng: float) -> List[List[float]]:
    """Translate normalized [dlat, dlng] offsets to absolute [lat, lng] coordinates."""
    return [[o[0] + target_lat, o[1] + target_lng] for o in offsets]


def _real_drone_geojson(center_lat: float, center_lng: float) -> Dict[str, Any]:
    """Return GeoJSON FeatureCollection of real DJI drone flight tracks centered over the given location."""
    raw = _load_real_geodata("real_drone_tracks.json")
    if not raw:
        return {"type": "FeatureCollection", "features": []}
    features: List[Dict[str, Any]] = []
    for track_idx, offsets in enumerate(raw):
        coords = _translate_offsets(offsets, center_lat, center_lng)
        # GeoJSON LineString uses [lng, lat] order
        features.append({
            "type": "Feature",
            "properties": {"line": track_idx + 1, "coverage": "aplicada", "source": "dji_kml"},
            "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in coords]},
        })
    return {"type": "FeatureCollection", "features": features}


def _real_heli_geojson(center_lat: float, center_lng: float) -> Dict[str, Any]:
    """Return GeoJSON FeatureCollection of real helicopter spray polygon + flight tracks."""
    raw = _load_real_geodata("real_helicopter_data.json")
    if not raw:
        return {"type": "FeatureCollection", "features": []}
    features: List[Dict[str, Any]] = []
    # Spray zone polygon
    poly_coords = _translate_offsets(raw["polygon"], center_lat, center_lng)
    ring = [[p[1], p[0]] for p in poly_coords]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    features.append({
        "type": "Feature",
        "properties": {"coverage": "zona_aplicacion", "source": "heli_kml"},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    })
    # Flight track lines
    for line_idx, offsets in enumerate(raw.get("lines", [])):
        coords = _translate_offsets(offsets, center_lat, center_lng)
        features.append({
            "type": "Feature",
            "properties": {"line": line_idx + 1, "coverage": "aplicada", "source": "heli_kml"},
            "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in coords]},
        })
    return {"type": "FeatureCollection", "features": features}


def _real_cosecha_points(center_lat: float, center_lng: float) -> List[Dict[str, Any]]:
    """Return list of GPS track points from real harvest machine CSV, centered over given location."""
    raw = _load_real_geodata("real_cosecha_track.json")
    if not raw:
        return []
    # Each item: [dlat, dlng, tch, vel]
    result = []
    for item in raw:
        dlat, dlng = item[0], item[1]
        tch = item[2] if len(item) > 2 else 0.0
        vel = item[3] if len(item) > 3 else 0.0
        result.append({"lat": round(dlat + center_lat, 7), "lng": round(dlng + center_lng, 7), "tch": tch, "vel": vel})
    return result


def _point_grid(lat: float, lng: float, dlat: float, dlng: float, count: int, *, offset: float = 0) -> Iterable[tuple[float, float, int]]:
    columns = max(4, int(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    produced = 0
    for row in range(rows):
        for column in range(columns):
            if produced >= count:
                return
            y_ratio = (row + 0.5) / rows
            x_ratio = (column + 0.5) / columns
            yield (
                lat - dlat * 0.78 + 2 * dlat * 0.78 * y_ratio,
                lng - dlng * 0.78 + 2 * dlng * 0.78 * x_ratio,
                produced,
            )
            produced += 1


def _avatar(name: str) -> str:
    return f"https://api.dicebear.com/9.x/initials/svg?seed={quote(name)}&backgroundType=gradientLinear"


def _featured_real_specs() -> List[Dict[str, Any]]:
    """Load top-3-by-area specs from each shapefile-derived JSON file.

    Returns up to 6 specs with real WGS-84 geometry plus derived lat/lng/dlat/dlng
    fields needed by the data-generation helpers.  Returns an empty list when the
    JSON files are absent so the caller can fall back to synthetic rectangles.
    """
    result: List[Dict[str, Any]] = []
    for filename in ("lotes_salgado.json",):
        path = _DATA_DIR / filename
        if not path.exists():
            continue
        try:
            raw: List[Dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        top3 = sorted(raw, key=lambda s: s.get("area") or 0, reverse=True)[:3]
        for item in top3:
            bounds = item.get("bounds") or {}
            center = item.get("center") or {}
            lat = float(center.get("lat") or 0)
            lng = float(center.get("lng") or 0)
            dlat = abs((bounds.get("north", lat + 0.005) - bounds.get("south", lat - 0.005)) / 2)
            dlng = abs((bounds.get("east", lng + 0.005) - bounds.get("west", lng - 0.005)) / 2)
            spec = dict(item)
            spec.update({
                "lat": lat,
                "lng": lng,
                "dlat": dlat,
                "dlng": dlng,
                "farm": item.get("finca") or "",
                "code": item.get("codigo") or item.get("lote") or item["key"],
            })
            result.append(spec)
    return result


def _parcel_specs() -> List[Dict[str, Any]]:
    real = _featured_real_specs()
    ndvi_vals = [0.78, 0.64, 0.22, 0.71, 0.55, 0.69]
    crop_configs = [
        ("Caña de azúcar", "CP72-2086"),
        ("Caña de azúcar", "CG98-10"),
        ("Caña de azúcar", "CP73-1547"),
        ("Caña de azúcar", "Mex79-431"),
        ("Caña de azúcar", "CG02-163"),
        ("Caña de azúcar", "CP72-2086"),
    ]
    if real:
        for i, spec in enumerate(real[:6]):
            spec["ndvi"] = ndvi_vals[i]
            spec["crop"] = crop_configs[i][0]
            spec["variety"] = crop_configs[i][1]
        return real[:6]
    # Fallback: synthetic rectangular parcels when JSON files are absent
    return [
        {"key": "aurora-norte", "name": "Lote Aurora Norte", "code": "AN-01", "farm": "Finca Aurora", "area": 72.4, "lat": 14.2948, "lng": -90.7850, "dlat": 0.0042, "dlng": 0.0053, "ndvi": 0.78, "crop": "Caña de azúcar", "variety": "CP72-2086"},
        {"key": "aurora-centro", "name": "Lote Aurora Centro", "code": "AC-02", "farm": "Finca Aurora", "area": 86.1, "lat": 14.2842, "lng": -90.7790, "dlat": 0.0047, "dlng": 0.0058, "ndvi": 0.64, "crop": "Caña de azúcar", "variety": "CG98-10"},
        {"key": "aurora-sur", "name": "Lote Aurora Sur", "code": "AS-03", "farm": "Finca Aurora", "area": 64.8, "lat": 14.2738, "lng": -90.7866, "dlat": 0.0039, "dlng": 0.0051, "ndvi": 0.22, "crop": "Caña de azúcar", "variety": "CP73-1547"},
        {"key": "el-roble", "name": "Lote El Roble", "code": "ER-04", "farm": "Finca El Roble", "area": 97.5, "lat": 14.2980, "lng": -90.7662, "dlat": 0.0052, "dlng": 0.0060, "ndvi": 0.71, "crop": "Caña de azúcar", "variety": "Mex79-431"},
        {"key": "la-esperanza", "name": "Lote La Esperanza", "code": "LE-05", "farm": "Finca La Esperanza", "area": 58.3, "lat": 14.2812, "lng": -90.7584, "dlat": 0.0037, "dlng": 0.0049, "ndvi": 0.55, "crop": "Caña de azúcar", "variety": "CG02-163"},
        {"key": "rio-claro", "name": "Lote Río Claro", "code": "RC-06", "farm": "Finca Río Claro", "area": 76.9, "lat": 14.2692, "lng": -90.7650, "dlat": 0.0043, "dlng": 0.0054, "ndvi": 0.69, "crop": "Caña de azúcar", "variety": "CP72-2086"},
    ]


def _parcel_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index, spec in enumerate(_parcel_specs()):
        parcel_id = _id(f"parcel:{spec['key']}")
        geometry = spec["geometry"] if spec.get("geometry") else _rect_geometry(spec["name"], spec["lat"], spec["lng"], spec["dlat"], spec["dlng"])
        bounds_val = spec.get("bounds") or _bounds(spec["lat"], spec["lng"], spec["dlat"], spec["dlng"])
        center_val = spec.get("center") or {"lat": spec["lat"], "lng": spec["lng"]}
        rows.append(
            _tag(
                {
                    "id": parcel_id,
                    "user_id": DEMO_USER_ID,
                    "company_id": DEMO_COMPANY_ID,
                    "name": spec["name"],
                    "finca": spec.get("farm") or "",
                    "lote": spec.get("lote") or spec["name"],
                    "codigo": spec.get("code") or spec.get("codigo"),
                    "crop": spec["crop"],
                    "variety": spec["variety"],
                    "area": spec["area"],
                    "area_ha": spec["area"],
                    "hectares": spec["area"],
                    "geometry": geometry,
                    "geometry_geojson": geometry,
                    "bounds": bounds_val,
                    "geometry_bounds": bounds_val,
                    "geometry_center": center_val,
                    "source": "commercial_demo",
                    "demo_featured": True,
                    "created_at": _iso(150 + index),
                    "updated_at": _iso(index + 1),
                }
            )
        )
    return rows


def _get_featured_keys() -> set:
    """Keys already used as featured (data-rich) parcels — skip in real_parcel_rows."""
    return {spec["key"] for spec in _parcel_specs()}


def _real_parcel_rows() -> List[Dict[str, Any]]:
    """Load pre-processed real parcels from ZIP-derived JSON files.

    Files are generated once by scripts/generate_demo_parcels.py and committed
    to the repository.  Each file contains an array of parcel specs with
    WGS-84 GeoJSON geometries.  Parcels already promoted as featured are skipped
    to avoid duplicates.  If a file is absent the source is silently skipped so
    the rest of the demo remains functional.
    """
    featured_keys = _get_featured_keys()
    sources = [
        ("lotes_salgado.json", "lotes_salgado"),
    ]
    rows: List[Dict[str, Any]] = []
    for filename, source_tag in sources:
        path = _DATA_DIR / filename
        if not path.exists():
            continue
        try:
            specs: List[Dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for idx, spec in enumerate(specs):
            if spec.get("key") in featured_keys:
                continue
            parcel_id = _id(f"real-parcel:{source_tag}:{spec.get('key', idx)}")
            geometry = spec.get("geometry")
            bounds = spec.get("bounds") or {}
            center = spec.get("center") or {}
            rows.append(
                _tag(
                    {
                        "id": parcel_id,
                        "user_id": DEMO_USER_ID,
                        "company_id": DEMO_COMPANY_ID,
                        "name": spec.get("name", ""),
                        "finca": spec.get("finca", ""),
                        "lote": spec.get("lote", ""),
                        "codigo": spec.get("codigo"),
                        "external_id": spec.get("external_id"),
                        "crop": None,
                        "variety": None,
                        "area": spec.get("area"),
                        "area_ha": spec.get("area"),
                        "hectares": spec.get("area"),
                        "geometry": geometry,
                        "geometry_geojson": geometry,
                        "bounds": bounds,
                        "geometry_bounds": bounds,
                        "geometry_center": center,
                        "source": f"real_demo_{source_tag}",
                        "created_at": _iso(100 + idx),
                        "updated_at": _iso(1),
                    }
                )
            )
    return rows


def _crop_rows(parcels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = _parcel_specs()
    return [
        _tag(
            {
                "id": _id(f"crop:{spec['key']}"),
                "user_id": DEMO_USER_ID,
                "parcel_id": parcel["id"],
                "crop_name": spec["crop"],
                "variety": spec["variety"],
                "planting_date": _date(260 - index * 13),
                "season": "Zafra 2025/2026",
                "status": "active",
                "created_at": _iso(130 + index),
                "updated_at": _iso(index + 1),
            }
        )
        for index, (parcel, spec) in enumerate(zip(parcels, specs))
    ]


def _satellite_side(spec: Dict[str, Any], days_ago: int, ndvi: float, index_type: str = "NDVI") -> Dict[str, Any]:
    bounds = spec.get("bounds") or _bounds(spec["lat"], spec["lng"], spec["dlat"], spec["dlng"])
    return {
        "date": _date(days_ago),
        "image_date": _date(days_ago),
        "index": index_type,
        "index_type": index_type,
        "statistics": {
            "min": round(max(-1, ndvi - 0.29), 3),
            "max": round(min(1, ndvi + 0.18), 3),
            "mean": round(ndvi, 3),
            "average_ndvi": round(ndvi, 3),
            "ndvi_mean": round(ndvi, 3),
            "coverage_percent": 96.8,
        },
        "ndvi_mean": round(ndvi, 3) if index_type == "NDVI" else None,
        "average_ndvi": round(ndvi, 3) if index_type == "NDVI" else None,
        "bounds": bounds,
        "coverage_percent": 96.8,
        "unavailable_percent": 3.2,
        "cloud_coverage": 2.4,
        "is_partial": False,
        "quality_mask_applied": True,
        "source": "sentinel-2-l2a-demo",
    }


def _satellite_rows(parcels: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    specs = _parcel_specs()
    comparisons: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    for index, (parcel, spec) in enumerate(zip(parcels, specs)):
        previous = min(0.9, max(0.18, spec["ndvi"] - (0.04 if index != 2 else -0.08)))
        current_days = 2 + index * 2
        previous_days = current_days + 19
        left = _satellite_side(spec, previous_days, previous)
        right = _satellite_side(spec, current_days, spec["ndvi"])
        comparisons.append(
            _tag(
                {
                    "id": _id(f"satellite-comparison:{spec['key']}"),
                    "user_id": DEMO_USER_ID,
                    "parcel_id": parcel["id"],
                    "status": "completed",
                    "title": f"Evolución NDVI · {spec['name']}",
                    "left": left,
                    "right": right,
                    "compared_at": _iso(current_days),
                    "created_at": _iso(current_days),
                    "updated_at": _iso(current_days),
                }
            )
        )
        for side_index, side in enumerate((left, right)):
            images.append(
                _tag(
                    {
                        "id": _id(f"satellite-image:{spec['key']}:{side_index}"),
                        "user_id": DEMO_USER_ID,
                        "parcel_id": parcel["id"],
                        "status": "completed",
                        "analysis_type": "satellite_layer",
                        "index_type": side["index_type"],
                        "image_date": side["image_date"],
                        "statistics": side["statistics"],
                        "bounds": side["bounds"],
                        "coverage_percent": side["coverage_percent"],
                        "source": side["source"],
                        "created_at": _iso(previous_days if side_index == 0 else current_days),
                        "updated_at": _iso(previous_days if side_index == 0 else current_days),
                    }
                )
            )
        jobs.append(
            _tag(
                {
                    "id": _id(f"satellite-job:{spec['key']}"),
                    "user_id": DEMO_USER_ID,
                    "parcel_id": parcel["id"],
                    "index_type": "NDVI",
                    "status": "completed" if index < 5 else "processing",
                    "created_at": _iso(current_days, hours=2),
                    "updated_at": _iso(current_days),
                }
            )
        )
    return images, comparisons, jobs


def _field_notes(parcels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = [
        (0, "Validar sector con vigor sobresaliente", "Confirmar en recorrido de campo el manejo aplicado en el bloque norte.", "inspeccion", "completada", ["NDVI", "seguimiento"]),
        (2, "Revisar posible estrés vegetal", "Prioridad alta: verificar humedad del suelo y presencia de plagas en el cuadrante suroeste.", "alerta", "en_proceso", ["NDVI bajo", "prioridad alta"]),
        (1, "Programar fertilización foliar", "Coordinar ventana de aplicación con el equipo aéreo y validar condiciones de viento.", "tarea", "pendiente", ["fertilización", "aéreo"]),
        (3, "Inspección de drenajes", "Confirmar limpieza preventiva antes del siguiente evento de lluvia.", "mantenimiento", "pendiente", ["drenaje", "prevención"]),
        (4, "Seguimiento de maleza localizada", "Comparar evidencia de DigiForms contra el último recorrido de supervisión.", "inspeccion", "en_proceso", ["SIG", "maleza"]),
        (5, "Validar cierre de cosecha", "Revisar avance y actualizar estado territorial en el SIG agrícola.", "cosecha", "completada", ["cosecha", "SIG"]),
        (0, "Calibrar sensor de rendimiento", "Validar telemetría antes del próximo recorrido de maquinaria.", "telemetria", "pendiente", ["maquinaria", "sensor"]),
        (3, "Comparar ortofoto reciente", "Revisar la cobertura detectada y documentar cambios relevantes.", "analisis", "completada", ["ortofoto", "comparación"]),
    ]
    rows: List[Dict[str, Any]] = []
    for index, (parcel_index, title, content, note_type, status, tags) in enumerate(specs):
        parcel = parcels[parcel_index % len(parcels)]
        center = parcel["geometry_center"]
        rows.append(
            _tag(
                {
                    "id": _id(f"field-note:{index}"),
                    "user_id": DEMO_USER_ID,
                    "parcel_id": parcel["id"],
                    "title": title,
                    "content": content,
                    "description": content,
                    "note_type": note_type,
                    "status": status,
                    "reference_date": _date(index + 1),
                    "index_type": "NDVI" if "NDVI" in " ".join(tags) else None,
                    "layer_date": _date(index + 3),
                    "location": {"lat": center["lat"] + 0.0005, "lng": center["lng"] - 0.0004},
                    "tags": tags,
                    "attachments": [],
                    "created_at": _iso(index + 1, hours=3),
                    "updated_at": _iso(index),
                }
            )
        )
    return rows


def _load_kml_analysis(filename: str) -> Dict[str, Any]:
    """Load a pre-processed KML/geometry JSON file for an aerial analysis."""
    data = _load_real_geodata(filename) or {}
    return {
        "geojson": data.get("geojson") or {"type": "FeatureCollection", "features": []},
        "bounds": data.get("bounds") or {},
        "name": data.get("name") or filename.replace(".json", ""),
    }


def _bounds_from_kml(kml_bounds: Dict[str, Any]) -> Dict[str, float]:
    """Convert KML bounds dict to seed bounds format."""
    if not kml_bounds:
        return {}
    return {
        "south": kml_bounds.get("south", 0),
        "north": kml_bounds.get("north", 0),
        "west": kml_bounds.get("west", 0),
        "east": kml_bounds.get("east", 0),
    }


def _aerial_rows(parcels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Real analyses from actual KML/ZIP files — one row per farm/project.
    # Coordinates are real GPS data from the field; map will zoom to the real location.
    real_configs = [
        # (file, aircraft, ha, pct, uncovered, overlap, alt, speed, product, dose, pilot, days_ago)
        ("drone_jiboa.json",       "drone",       70.8, 97.8, 1.6, 0.7,  3.8,  24.5, "Fertilizante foliar NPK",   "12 L/ha", "Carlos Méndez",  3),
        ("drone_sandiego1.json",   "drone",       56.2, 96.4, 2.1, 0.8,  4.1,  22.8, "Herbicida selectivo",       "2.2 L/ha","Carlos Méndez",  7),
        ("drone_sandiego2.json",   "drone",       48.9, 95.8, 2.4, 1.0,  4.0,  23.1, "Maduración química",        "3.5 L/ha","Pedro Alvarado", 11),
        ("drone_launion.json",     "drone",       83.4, 98.1, 1.3, 0.5,  4.2,  25.0, "Urea foliar",               "8 L/ha",  "Carlos Méndez",  15),
        ("drone_madretierra.json", "drone",       64.7, 96.9, 2.0, 0.9,  3.9,  24.8, "Fertilizante nitrogenado",  "6 L/ha",  "Pedro Alvarado", 19),
        ("helicopter_sandiego.json","helicoptero",84.5, 96.9, 2.6, 1.1, 11.7,  92.0, "Madurante agrícola",        "1.5 L/ha","Andrea López",   23),
    ]
    rows: List[Dict[str, Any]] = []
    for index, (filename, aircraft, total, pct, uncovered, overlap, altitude, speed, product, dose, pilot, days_ago) in enumerate(real_configs):
        kml = _load_kml_analysis(filename)
        geojson = kml["geojson"]
        kml_bounds = kml["bounds"]
        farm_name = kml["name"]
        parcel = parcels[index % len(parcels)]

        # Use actual KML bounds; fall back to parcel bounds if data missing
        bounds = _bounds_from_kml(kml_bounds) if kml_bounds else parcel["geometry_bounds"]

        rows.append(
            _tag(
                {
                    "id": _id(f"aerial-analysis:{index}"),
                    "user_id": DEMO_USER_ID,
                    "lote_id": parcel["id"],
                    "parcel_id": parcel["id"],
                    "parcel_name": farm_name,
                    "aircraft_type": aircraft,
                    "fecha_aplicacion": _date(days_ago),
                    "piloto": pilot,
                    "producto": product,
                    "dosis": dose,
                    "status": "completed",
                    "total_ha": total,
                    "covered_area": total,
                    "covered_pct": pct,
                    "uncovered_ha": uncovered,
                    "overlap_ha": overlap,
                    "avg_altitude": altitude,
                    "avg_speed": speed,
                    "parcel_results": [
                        {
                            "name": farm_name, "totalHa": total,
                            "coveredHa": round(total * pct / 100, 2), "coveredPct": pct,
                            "uncoveredHa": uncovered, "overlapHa": overlap,
                            "avgAltitude": altitude, "avgSpeed": speed,
                            "uniqueLines": len(geojson.get("features", [])),
                            "volume": round(total * float(dose.split()[0]), 1),
                            "coveredGeom": geojson,
                        }
                    ],
                    "parcel_geometries": {"parcelas": parcel["geometry_geojson"], "sprOnTracks": geojson},
                    "bounds": bounds,
                    "observaciones": "Aplicación dentro de parámetros. Cobertura uniforme verificada con trazabilidad GPS.",
                    "created_at": _iso(days_ago),
                    "updated_at": _iso(days_ago - 1),
                }
            )
        )
    return rows


def _mapping_rows(parcels: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    configs = [
        (0, "Mapa de rendimiento · Aurora Norte", "cosecha", "Cosechadora JD 3520", "Mario Estrada", ["rendimiento", "velocidad", "humedad"]),
        (1, "Mapa de compactación · Aurora Centro", "preparacion", "Tractor Case IH Magnum", "Sofía García", ["compactacion", "velocidad", "rpm"]),
        (3, "Mapa de fertilización · El Roble", "fertilizacion", "Aplicador Jacto Uniport", "José Pérez", ["dosis", "velocidad", "presion"]),
        (5, "Mapa de cosecha · Río Claro", "cosecha", "Cosechadora JD 3520", "Mario Estrada", ["rendimiento", "velocidad", "humedad"]),
    ]
    sessions: List[Dict[str, Any]] = []
    points: List[Dict[str, Any]] = []
    for session_index, (parcel_index, name, labor, machine, person, variables) in enumerate(configs):
        parcel = parcels[parcel_index % len(parcels)]
        session_id = _id(f"mapping-session:{session_index}")
        b = parcel["geometry_bounds"]
        sessions.append(
            _tag(
                {
                    "id": session_id,
                    "user_id": DEMO_USER_ID,
                    "parcel_id": parcel["id"],
                    "name": name,
                    "zafra": "2025/2026",
                    "labor": labor,
                    "responsable": person,
                    "maquinaria": machine,
                    "analysis_type": "variable_rate_mapping",
                    "file_name": f"{(parcel.get('codigo') or 'demo').lower()}-{labor}.csv",
                    "variables": variables,
                    "bounds": {"minLat": b["south"], "maxLat": b["north"], "minLng": b["west"], "maxLng": b["east"]},
                    "created_at": _iso(5 + session_index * 6),
                    "updated_at": _iso(4 + session_index * 6),
                }
            )
        )
        # For cosecha sessions use real harvest machine GPS track, for others use synthetic grid
        if labor == "cosecha" and session_index == 0:
            real_pts = _real_cosecha_points(parcel["geometry_center"]["lat"], parcel["geometry_center"]["lng"])
            pt_source = [(p["lat"], p["lng"], i, {"velocidad": round(p["vel"], 2), "tch": round(p["tch"], 2), "humedad": round(16.5 + math.sin(i * 0.3) * 2.5, 2), "rendimiento": round(p["tch"] * 0.95 + session_index * 2, 2), "compactacion": round(1.2 + abs(math.sin(i * 0.2)) * 0.4, 2), "dosis": 0.0, "rpm": round(1750 + math.cos(i * 0.25) * 120, 0), "presion": round(3.1 + math.sin(i * 0.4) * 0.3, 2)}) for i, p in enumerate(real_pts)]
        else:
            pt_source = [(lat, lng, point_index, {"velocidad": round(6.2 + math.sin(point_index / 5 + session_index) * 1.3, 2), "humedad": round(18.5 + math.cos(point_index / 4) * 3.2, 2), "rendimiento": round(92 + math.sin(point_index / 5 + session_index) * 14 + session_index * 2, 2), "compactacion": round(1.25 + abs(math.sin(point_index / 5 + session_index)) * 0.42, 2), "dosis": round(11.8 + math.sin(point_index / 5 + session_index) * 1.7, 2), "rpm": round(1780 + math.sin(point_index / 5 + session_index) * 160, 0), "presion": round(3.2 + math.sin(point_index / 5 + session_index) * 0.35, 2)}) for lat, lng, point_index in _point_grid(parcel["geometry_center"]["lat"], parcel["geometry_center"]["lng"], (b["north"] - b["south"]) / 2, (b["east"] - b["west"]) / 2, 42)]
        for lat, lng, point_index, attrs in pt_source:
            points.append(
                _tag(
                    {
                        "id": _id(f"mapping-point:{session_index}:{point_index}"),
                        "user_id": DEMO_USER_ID,
                        "session_id": session_id,
                        "latitude": round(lat, 7),
                        "longitude": round(lng, 7),
                        "attributes": attrs,
                        "created_at": _iso(5 + session_index * 6),
                        "updated_at": _iso(4 + session_index * 6),
                    }
                )
            )
    return sessions, points


def _labor_rows(parcels: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    workers = [
        ("EMP-001", "María González", 0), ("EMP-002", "Carlos Hernández", 0), ("EMP-003", "Ana López", 1),
        ("EMP-004", "Pedro Morales", 1), ("EMP-005", "Lucía Ramírez", 2), ("EMP-006", "Diego Castillo", 2),
        ("EMP-007", "Sofía García", 3), ("EMP-008", "Jorge Pérez", 3), ("EMP-009", "Elena Vásquez", 4),
        ("EMP-010", "Miguel Chacón", 4), ("EMP-011", "Andrea Mendoza", 5), ("EMP-012", "Luis Estrada", 5),
    ]
    photos = [
        _tag({"id": _id(f"labor-photo:{code}"), "user_id": DEMO_USER_ID, "empleado_codigo": code, "foto_url": _avatar(name), "created_at": _iso(80), "updated_at": _iso(1)})
        for code, name, _ in workers
    ]
    rows: List[Dict[str, Any]] = []
    for index, (code, name, parcel_index) in enumerate(workers):
        parcel = parcels[parcel_index % len(parcels)]
        center = parcel["geometry_center"]
        day = 0 if index < 6 else 1
        start_hour = 6 + (index % 3)
        entrada = _time_at(day, start_hour, 10 + index % 8)
        # Keep first three employees actively in-field. Include two meaningful anomalies.
        if index < 3:
            salida = None
        elif index == 4:
            salida = _time_at(day, start_hour + 15, 15)
        elif index == 8:
            salida = _time_at(day, start_hour + 1, 15)
        else:
            salida = _time_at(day, start_hour + 8, 20 + index % 10)
        lat_salida = center["lat"] + (0.010 if index == 9 else 0.0007)
        lng_salida = center["lng"] + (0.010 if index == 9 else 0.0004)
        rows.append(
            _tag(
                {
                    "id": _id(f"labor-register:{index}"),
                    "user_id": DEMO_USER_ID,
                    "id_registro": 9100 + index,
                    "empleado_codigo": code,
                    "empleado_nombre": name,
                    "entrada": entrada,
                    "salida": salida,
                    "entrada_real": entrada,
                    "salida_real": salida,
                    "lat_entrada": center["lat"] + 0.0002,
                    "lon_entrada": center["lng"] - 0.0002,
                    "lat_salida": lat_salida if salida else None,
                    "lon_salida": lng_salida if salida else None,
                    "lote_codigo": parcel["codigo"],
                    "lote_nombre": parcel["name"],
                    "pais": "Guatemala",
                    "foto_url": _avatar(name),
                    "created_at": _iso(day),
                    "updated_at": _iso(day),
                }
            )
        )
    return rows, photos


def _sig_rows(parcels: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    harvest: List[Dict[str, Any]] = []
    pest: List[Dict[str, Any]] = []
    raw: List[Dict[str, Any]] = []
    for parcel_index, parcel in enumerate(parcels):
        center = parcel["geometry_center"]
        feature_key = f"{parcel['id']}::{parcel['name']}"
        states = ["Terminada", "En proceso", "No iniciada"]
        for point_index in range(3):
            index = parcel_index * 3 + point_index
            state = states[(parcel_index + point_index) % len(states)]
            record = _tag(
                {
                    "id": _id(f"sig-harvest:{index}"),
                    "user_id": DEMO_USER_ID,
                    "company_id": DEMO_COMPANY_ID,
                    "external_response_id": f"DG-H-{1040 + index}",
                    "parcela": parcel["name"],
                    "zona": f"Bloque {chr(65 + point_index)}",
                    "metodo": "Mecánica" if point_index != 1 else "Manual",
                    "canacond": "Óptima" if point_index != 2 else "Programada",
                    "terminal": f"Tablet Campo {1 + parcel_index % 3}",
                    "estado": state,
                    "lat": center["lat"] + (point_index - 1) * 0.0011,
                    "lng": center["lng"] + (point_index - 1) * 0.0010,
                    "hora_inicio": "06:30",
                    "hora_fin": "15:45" if state == "Terminada" else "-",
                    "fecha": _date(2 + parcel_index),
                    "fecha_fin": _date(1 + parcel_index) if state == "Terminada" else "-",
                    "parcel_id": parcel["id"],
                    "feature_key": feature_key,
                    "outside_registered_parcel": False,
                    "digiforms_state": "1",
                    "dynamic_fields": {"toneladas_estimadas": 140 + index * 3, "supervisor": "Equipo Comercial Demo"},
                    "raw_payload": {"source": "Digiforms Data API Demo", "response_id": 1040 + index},
                    "source": "digiforms_results_api",
                    "created_at": _iso(2 + parcel_index),
                    "updated_at": _iso(1 + parcel_index),
                }
            )
            harvest.append(record)
            raw.append(_tag({"id": _id(f"sig-raw-harvest:{index}"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "harvest", "form_id": "DEMO-HARVEST-01", "external_response_id": record["external_response_id"], "digiforms_state": "1", "dynamic_fields": record["dynamic_fields"], "raw_payload": record["raw_payload"], "image_urls": [], "created_at": record["created_at"], "updated_at": record["updated_at"]}))

        if parcel_index in {1, 2, 4, 5}:
            index = parcel_index
            weed = "Coyolillo" if parcel_index in {2, 4} else "Caminadora"
            plague = "Salivazo" if parcel_index == 2 else "Sin incidencia crítica"
            record = _tag(
                {
                    "id": _id(f"sig-pest:{index}"),
                    "user_id": DEMO_USER_ID,
                    "company_id": DEMO_COMPANY_ID,
                    "external_response_id": f"DG-P-{2080 + index}",
                    "terminal": f"Móvil Supervisor {1 + parcel_index % 2}",
                    "fecha": _date(parcel_index + 1),
                    "hora": "09:20",
                    "zona": f"Sector {parcel['codigo']}",
                    "maleza": "Sí",
                    "tipo_maleza": weed,
                    "plaga": "Sí" if parcel_index == 2 else "No",
                    "tipo_plaga": plague,
                    "enfermedades": "Sin evidencia" if parcel_index != 2 else "Revisión preventiva requerida",
                    "sintomas": "Focos localizados detectados durante recorrido digital.",
                    "observaciones": "Registro georreferenciado desde formulario de campo. Programar seguimiento.",
                    "firma": "Supervisor de campo",
                    "firma_texto": "Supervisor de campo",
                    "evidencia_fotografica": "",
                    "photo_url": "",
                    "signature_url": "",
                    "lat": center["lat"] + 0.0008,
                    "lng": center["lng"] - 0.0009,
                    "parcel_id": parcel["id"],
                    "feature_key": feature_key,
                    "outside_registered_parcel": False,
                    "digiforms_state": "1",
                    "dynamic_fields": {"severidad": "Media" if parcel_index == 2 else "Baja", "acciones": "Inspección dirigida"},
                    "raw_payload": {"source": "Digiforms Data API Demo", "response_id": 2080 + index},
                    "source": "digiforms_results_api",
                    "created_at": _iso(parcel_index + 1),
                    "updated_at": _iso(parcel_index),
                }
            )
            pest.append(record)
            raw.append(_tag({"id": _id(f"sig-raw-pest:{index}"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "pest_weed", "form_id": "DEMO-PEST-01", "external_response_id": record["external_response_id"], "digiforms_state": "1", "dynamic_fields": record["dynamic_fields"], "raw_payload": record["raw_payload"], "image_urls": [], "created_at": record["created_at"], "updated_at": record["updated_at"]}))

    import_runs = [
        _tag({"id": _id("sig-import:harvest"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "harvest", "form_id": "DEMO-HARVEST-01", "source": "digiforms_results_api", "file_name": "Sincronización automática DigiForms", "status": "completed", "total_rows": len(harvest), "valid_rows": len(harvest), "imported_rows": len(harvest), "updated_rows": 0, "duplicate_rows": 0, "invalid_rows": 0, "outside_registered_parcel_rows": 0, "parcel_features_received": len(parcels), "images_received": 0, "cursor_before": 1021, "cursor_after": 1057, "approved_rows": len(harvest), "pending_rows": 0, "rejected_rows": 0, "deleted_rows": 0, "sync_mode": "incremental_response_id", "created_at": _iso(1), "updated_at": _iso(1)}),
        _tag({"id": _id("sig-import:pest"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "pest_weed", "form_id": "DEMO-PEST-01", "source": "digiforms_results_api", "file_name": "Sincronización automática DigiForms", "status": "completed", "total_rows": len(pest), "valid_rows": len(pest), "imported_rows": len(pest), "updated_rows": 0, "duplicate_rows": 0, "invalid_rows": 0, "outside_registered_parcel_rows": 0, "parcel_features_received": len(parcels), "images_received": 0, "cursor_before": 2072, "cursor_after": 2085, "approved_rows": len(pest), "pending_rows": 0, "rejected_rows": 0, "deleted_rows": 0, "sync_mode": "incremental_response_id", "created_at": _iso(2), "updated_at": _iso(2)}),
    ]
    cursors = [
        _tag({"id": _id("sig-cursor:harvest"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "harvest", "form_id": "DEMO-HARVEST-01", "last_response_id": 1057, "last_sync_at": _iso(1), "last_success_at": _iso(1), "last_error": None, "last_request_mode": "incremental_response_id", "created_at": _iso(30), "updated_at": _iso(1)}),
        _tag({"id": _id("sig-cursor:pest"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "form_type": "pest_weed", "form_id": "DEMO-PEST-01", "last_response_id": 2085, "last_sync_at": _iso(2), "last_success_at": _iso(2), "last_error": None, "last_request_mode": "incremental_response_id", "created_at": _iso(30), "updated_at": _iso(2)}),
    ]
    mappings = [
        _tag({"id": _id("digiforms-map:harvest"), "company_id": DEMO_COMPANY_ID, "form_type": "harvest", "display_name": "Monitoreo de cosecha", "form_id": "DEMO-HARVEST-01", "is_enabled": True, "initial_response_id": 1, "initial_sync_start_date": _date(365), "initial_sync_end_date": _date(0), "created_at": _iso(60), "updated_at": _iso(1)}),
        _tag({"id": _id("digiforms-map:pest"), "company_id": DEMO_COMPANY_ID, "form_type": "pest_weed", "display_name": "Malezas y plagas", "form_id": "DEMO-PEST-01", "is_enabled": True, "initial_response_id": 1, "initial_sync_start_date": _date(365), "initial_sync_end_date": _date(0), "created_at": _iso(60), "updated_at": _iso(1)}),
    ]
    return {
        "sig_harvest_records": harvest,
        "sig_pest_weed_records": pest,
        "digiforms_raw_submissions": raw,
        "sig_import_runs": import_runs,
        "sig_sync_cursors": cursors,
        "digiforms_form_mappings": mappings,
    }


def _extension_rows() -> Dict[str, List[Dict[str, Any]]]:
    request_id = _id("extension-request:digiforms")
    return {
        "extension_requests": [
            _tag({"id": request_id, "extension_id": "digiforms", "extension_name": "DigiformsApp", "status": "approved", "request_type": "existing_account", "has_existing_account": True, "existing_digiforms_user_id": "AGROVISION-DEMO", "company_id": DEMO_COMPANY_ID, "company_name_snapshot": "Agrovisión Demo", "requested_by_user_id": DEMO_USER_ID, "requester_email": DEMO_EMAIL, "requester_name": "Ejecutivo Demo", "contact_notes": "Dataset comercial precargado para recorrido de demostración.", "client_message": "Integración DigiForms habilitada para la demostración comercial.", "admin_notes": "Configuración demo aislada de servicios externos.", "reviewed_by_user_id": DEMO_USER_ID, "reviewed_at": _iso(40), "enabled_at": _iso(40), "created_at": _iso(45), "updated_at": _iso(2)}),
        ],
        "digiforms_accounts": [
            _tag({"id": _id("digiforms-account:demo"), "company_id": DEMO_COMPANY_ID, "user_id": DEMO_USER_ID, "extension_request_id": request_id, "digiforms_client_id": "DEMO-CLIENT", "digiforms_user_id": "AGROVISION-DEMO", "digiforms_user_name": "Agrovisión Demo", "digiforms_email": DEMO_EMAIL, "profile": "admin", "mode": "commercial_demo", "active": True, "api_status": "demo_dataset_ready", "created_at": _iso(40), "updated_at": _iso(1)})
        ],
        "digiforms_connections": [
            _tag({"id": _id("digiforms-connection:demo"), "company_id": DEMO_COMPANY_ID, "client_id": "DEMO-CLIENT", "api_user": "demo-api-user", "encrypted_api_password": "commercial-demo-placeholder", "auto_sync_enabled": True, "connection_status": "demo_ready", "last_connection_test_at": _iso(1), "last_connection_error": None, "configured_by_user_id": DEMO_USER_ID, "created_at": _iso(40), "updated_at": _iso(1)})
        ],
        "digiforms_user_links": [
            _tag({"id": _id("digiforms-user:supervisor"), "company_id": DEMO_COMPANY_ID, "dataris_user_id": DEMO_USER_ID, "created_by_user_id": DEMO_USER_ID, "digiforms_client_id": "DEMO-CLIENT", "digiforms_user_id": "SUP-CAMPO-01", "digiforms_user_name": "María González", "digiforms_email": "maria.demo@dataris.app", "profile": "admin", "mode": "commercial_demo", "active": True, "external_status": "verified_demo", "last_api_action": "demo_seed", "last_api_status": "ok", "created_at": _iso(25), "updated_at": _iso(1)}),
            _tag({"id": _id("digiforms-user:tecnico"), "company_id": DEMO_COMPANY_ID, "dataris_user_id": DEMO_USER_ID, "created_by_user_id": DEMO_USER_ID, "digiforms_client_id": "DEMO-CLIENT", "digiforms_user_id": "TEC-CAMPO-02", "digiforms_user_name": "Carlos Hernández", "digiforms_email": "carlos.demo@dataris.app", "profile": "user", "mode": "commercial_demo", "active": True, "external_status": "verified_demo", "last_api_action": "demo_seed", "last_api_status": "ok", "created_at": _iso(24), "updated_at": _iso(1)}),
            _tag({"id": _id("digiforms-user:inspector"), "company_id": DEMO_COMPANY_ID, "dataris_user_id": DEMO_USER_ID, "created_by_user_id": DEMO_USER_ID, "digiforms_client_id": "DEMO-CLIENT", "digiforms_user_id": "INS-CAMPO-03", "digiforms_user_name": "Ana López", "digiforms_email": "ana.demo@dataris.app", "profile": "user", "mode": "commercial_demo", "active": True, "external_status": "verified_demo", "last_api_action": "demo_seed", "last_api_status": "ok", "created_at": _iso(23), "updated_at": _iso(1)}),
        ],
        "digiforms_operation_logs": [
            _tag({"id": _id("digiforms-log:sync"), "user_id": DEMO_USER_ID, "created_by_user_id": DEMO_USER_ID, "action": "sync_results", "status": "ok", "message": "Dataset comercial sincronizado correctamente.", "created_at": _iso(1), "updated_at": _iso(1)})
        ],
    }


def _base_rows(password_hash: Callable[[str], str]) -> Dict[str, List[Dict[str, Any]]]:
    created = _iso(200)
    updated = _iso(1)
    modules = [module_id for module_id, *_ in DEMO_MODULES]
    return {
        "users": [
            _tag({"id": DEMO_USER_ID, "email": DEMO_EMAIL, "password_hash": password_hash(DEMO_PASSWORD), "is_active": True, "created_at": created, "updated_at": updated, "user_metadata": {"first_name": "Ejecutivo", "last_name": "Demo", "company_name": "Agrovisión Demo", "is_demo": True, "demo_profile": "commercial"}, "app_metadata": {"is_demo": True, "demo_profile": "commercial"}})
        ],
        "profiles": [
            _tag({"id": DEMO_USER_ID, "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "email": DEMO_EMAIL, "first_name": "Ejecutivo", "last_name": "Demo", "company_name": "Agrovisión Demo", "company_logo_url": None, "avatar_url": _avatar("Ejecutivo Demo"), "phone": "+502 5555-0101", "location": "Escuintla, Guatemala", "hectareas": 456, "max_users": 25, "created_at": created, "updated_at": updated})
        ],
        "user_roles": [_tag({"id": _id("role:demo-company-admin"), "user_id": DEMO_USER_ID, "role": "company_admin", "created_at": created, "updated_at": updated})],
        "companies": [_tag({"id": DEMO_COMPANY_ID, "name": "Agrovisión Demo", "email": DEMO_EMAIL, "phone": "+502 5555-0101", "cif": "DEMO-COMERCIAL", "max_hectares": 2500, "used_hectares": 456, "is_active": True, "created_at": created, "updated_at": updated})],
        "admin_users": [_tag({"id": _id("admin-user:demo"), "user_id": DEMO_USER_ID, "company_id": DEMO_COMPANY_ID, "admin_role": "company_admin", "assigned_hectares": 2500, "created_by": None, "is_active": True, "created_at": created, "updated_at": updated})],
        "company_modules": [_tag({"id": _id(f"company-module:{module_id}"), "company_id": DEMO_COMPANY_ID, "module_id": module_id, "is_enabled": True, "is_active": True, "created_at": created, "updated_at": updated}) for module_id in modules],
        "user_modules": [_tag({"id": _id(f"user-module:{module_id}"), "user_id": DEMO_USER_ID, "admin_user_id": _id("admin-user:demo"), "module_id": module_id, "is_enabled": True, "is_active": True, "created_at": created, "updated_at": updated}) for module_id in modules],
    }


def _seed_rows(password_hash: Callable[[str], str]) -> Dict[str, List[Dict[str, Any]]]:
    parcels = _parcel_rows()
    # Real lots from customer shapefiles — appended after the synthetic parcels
    # so all rich demo data (satellite, notes, etc.) tied to the synthetic parcel
    # IDs remains intact.
    real_parcels = _real_parcel_rows()
    satellite_images, comparisons, jobs = _satellite_rows(parcels)
    aerial = _aerial_rows(parcels)
    sessions, points = _mapping_rows(parcels)
    labor, photos = _labor_rows(parcels)
    rows = _base_rows(password_hash)
    rows.update(
        {
            "parcels": parcels + real_parcels,
            "parcel_crops": _crop_rows(parcels),
            "satellite_images": satellite_images,
            "satellite_comparisons": comparisons,
            "satellite_jobs": jobs,
            "field_notes": _field_notes(parcels),
            "aerial_analyses": aerial,
            "analysis_sessions": sessions,
            "analysis_data_points": points,
            "laborapp_registros": labor,
            "laborapp_empleados_foto": photos,
        }
    )
    rows.update(_sig_rows(parcels))
    rows.update(_extension_rows())
    return rows


def _row_reference_values(row: Dict[str, Any]) -> Iterable[str]:
    """Yield relational identifiers stored by compatibility tables.

    The compatibility layer is intentionally flexible: old and new screens can
    add rows with slightly different shapes.  Looking at relational ``*_id``
    fields lets the demo reset remove children created during a live demo too
    (for example points added to a mapping session), without touching rows from
    real tenants.
    """

    for key, value in row.items():
        if key == "id" or not (key.endswith("_id") or key in {"owner_id"}):
            continue
        if isinstance(value, (str, int)):
            yield str(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int)):
                    yield str(item)


def _is_direct_demo_row(table_name: str, row: Dict[str, Any]) -> bool:
    if row.get("demo_seed") == DEMO_TAG:
        return True
    if table_name == "users":
        return (
            str(row.get("id") or "") == DEMO_USER_ID
            or str(row.get("email") or "").strip().lower() == DEMO_EMAIL
            or is_commercial_demo_user(row)
        )
    if table_name == "companies" and str(row.get("id") or "") == DEMO_COMPANY_ID:
        return True
    return any(reference in {DEMO_USER_ID, DEMO_COMPANY_ID} for reference in _row_reference_values(row))


def _remove_demo_tenant_rows(db: Dict[str, Any]) -> bool:
    """Delete only the isolated commercial-demo graph from the JSON state.

    Rows created through the UI during a demo do not necessarily include the
    ``demo_seed`` marker.  We therefore discover the complete graph starting
    from the deterministic demo user/company and follow relational ``*_id``
    fields until no additional demo-owned descendants remain.
    """

    tables = db.setdefault("tables", {})
    known_demo_ids = {DEMO_USER_ID, DEMO_COMPANY_ID}

    # Usuarios REALES: cualquier cuenta que no sea la propia de la demostración.
    # Son intocables. Un reseteo de demo (que se dispara con cada login del
    # usuario demo) nunca debe borrar a un usuario real ni sus filas de identidad
    # sólo porque alguna referencia cruzada apunte a un id de la demo: eso
    # eliminaría la cuenta y, con ella, cerraría la sesión de un cliente real.
    real_user_ids = {
        str(row.get("id"))
        for row in db.setdefault("users", [])
        if row.get("id") and not _is_direct_demo_row("users", row)
    }

    def _is_protected(row: Dict[str, Any]) -> bool:
        return (
            str(row.get("id") or "") in real_user_ids
            or str(row.get("user_id") or "") in real_user_ids
        )

    # Fixed-point discovery: children can reference rows discovered in an
    # earlier pass (parcel -> crop, mapping session -> data point, etc.).
    changed = True
    while changed:
        changed = False
        collections = [("users", db.setdefault("users", []))]
        collections.extend((name, rows) for name, rows in tables.items() if name != "platform_modules")
        for table_name, rows in collections:
            for row in rows:
                if _is_protected(row):
                    continue
                row_id = str(row.get("id") or "")
                belongs_to_demo = _is_direct_demo_row(table_name, row) or any(
                    reference in known_demo_ids for reference in _row_reference_values(row)
                )
                if belongs_to_demo and row_id and row_id not in known_demo_ids:
                    known_demo_ids.add(row_id)
                    changed = True

    removed = False
    users = db.setdefault("users", [])
    filtered_users = [
        row
        for row in users
        if not (
            not _is_protected(row)
            and (_is_direct_demo_row("users", row) or str(row.get("id") or "") in known_demo_ids)
        )
    ]
    if len(filtered_users) != len(users):
        db["users"] = filtered_users
        removed = True

    for table_name, rows in list(tables.items()):
        if table_name == "platform_modules":
            continue
        filtered_rows = []
        for row in rows:
            row_id = str(row.get("id") or "")
            belongs_to_demo = (
                not _is_protected(row)
                and (
                    _is_direct_demo_row(table_name, row)
                    or row_id in known_demo_ids
                    or any(reference in known_demo_ids for reference in _row_reference_values(row))
                )
            )
            if belongs_to_demo:
                removed = True
            else:
                filtered_rows.append(row)
        tables[table_name] = filtered_rows
    return removed


def _upsert_by_id(target: List[Dict[str, Any]], incoming: Iterable[Dict[str, Any]], *, replace_existing: bool) -> bool:
    changed = False
    index = {str(row.get("id")): pos for pos, row in enumerate(target) if row.get("id")}
    for row in incoming:
        row_id = str(row.get("id") or "")
        if row_id and row_id in index:
            if replace_existing and target[index[row_id]] != row:
                target[index[row_id]] = row
                changed = True
            continue
        target.append(row)
        if row_id:
            index[row_id] = len(target) - 1
        changed = True
    return changed


def is_commercial_demo_user(user: Any) -> bool:
    if isinstance(user, str):
        return user == DEMO_USER_ID or user.strip().lower() == DEMO_EMAIL
    if not isinstance(user, dict):
        return False
    metadata = user.get("user_metadata") or {}
    app_metadata = user.get("app_metadata") or {}
    return (
        str(user.get("id") or "") == DEMO_USER_ID
        or str(user.get("email") or "").strip().lower() == DEMO_EMAIL
        or metadata.get("demo_profile") == "commercial"
        or app_metadata.get("demo_profile") == "commercial"
    )


def ensure_commercial_demo(db: Dict[str, Any], *, password_hash: Callable[[str], str], reset: bool = False) -> bool:
    """Ensure or rebuild the isolated commercial-demo tenant.

    ``reset=True`` removes the isolated demo tenant graph and recreates it,
    including untagged rows created interactively during a prior presentation.
    Real tenant data remains untouched.  Normal application writes call this
    function with ``reset=False`` so interactions inside an active demo are
    preserved until the next demo login.
    """

    changed = False
    tables = db.setdefault("tables", {})
    for module_id, name, description, icon in DEMO_MODULES:
        platform_modules = tables.setdefault("platform_modules", [])
        existing = next((item for item in platform_modules if str(item.get("id") or "") == module_id), None)
        if not existing:
            platform_modules.append({"id": module_id, "name": name, "description": description, "icon": icon, "is_active": True, "created_at": _iso(200), "updated_at": _iso(1)})
            changed = True

    seeds = _seed_rows(password_hash)
    if reset:
        changed = _remove_demo_tenant_rows(db) or changed

    changed = _upsert_by_id(db.setdefault("users", []), seeds["users"], replace_existing=reset) or changed
    for table_name, rows in seeds.items():
        if table_name == "users":
            continue
        changed = _upsert_by_id(tables.setdefault(table_name, []), rows, replace_existing=reset) or changed
    return changed
