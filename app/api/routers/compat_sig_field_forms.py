"""SIG agrícola › Formularios de campo (Bitácoras Sustenta).

Los envíos de DigiForms del formulario «Bitácoras Sustenta» se guardan en dos
tablas del store compat:

- `sig_field_form_parcels`: la ficha de cada parcela en el ciclo (productor,
  ciclo actual, opción de reducción, fertilización histórica…). Llega con el
  primer envío de la parcela; la clave es `parcela_clave` + `ciclo_actual`.
- `sig_field_form_records`: cada fila de bitácora (actividad, producto,
  cantidad, coordenada…). Los envíos posteriores solo añaden filas aquí.

Los % de nitrógeno de cada producto viven en `sig_field_form_products`, un
catálogo editable por empresa: el cálculo de la reducción de N no debe depender
de un valor escrito en el código.

La vista es solo para administradores (superadmin o company_admin): José lo pidió
así el 13 oct 2026. La asignación de cada punto a su parcela (point-in-polygon)
la hace el frontend con los linderos que ya descarga; aquí se guarda la clave
que se capturó en campo, no la deducida.
"""

from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Header, HTTPException
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.api.routers.compat import (
    LOCK,
    _company_for_user,
    _is_platform_superadmin,
    active_admin_row,
    bearer_user,
    now,
    read_db,
    table,
    visible_parcels,
    write_db,
)

router = APIRouter(prefix="/compat/sig-agricola/field-forms", tags=["SIG Agrícola · Formularios de campo"])

FORM_KEY = "bitacoras-sustenta"
FORM_NAME = "Bitácoras Sustenta"
PROFILES_TABLE = "sig_field_form_parcels"
RECORDS_TABLE = "sig_field_form_records"
PRODUCTS_TABLE = "sig_field_form_products"
# El API genérico de tablas las acota a administradores de la empresa
# (SIG_FIELD_FORM_TABLES en compat.py).

DEMO_BATCH = "sustenta-demo-gt"
ADMIN_ROLES = {"superadmin", "company_admin"}

# Recuadro de Guatemala (lng/lat). Basta para separar las parcelas de Guatemala
# de las de México que conviven en la cuenta de administración.
GUATEMALA_BBOX = (-92.30, 13.60, -88.10, 17.90)

ACTIVIDADES = ("Riego", "Fertilización química", "Aplicación biofertilizante", "Otra")

DEFAULT_PRODUCTS: List[Dict[str, Any]] = [
    {"id": "amoniaco-anhidro", "nombre": "Amoniaco anhidro", "n_pct": 82.0, "unidad": "kg", "precio": 14.2},
    {"id": "urea", "nombre": "Urea", "n_pct": 46.0, "unidad": "kg", "precio": 11.6},
    {"id": "map-11-52-00", "nombre": "MAP 11-52-00", "n_pct": 11.0, "unidad": "kg", "precio": 15.8},
    {"id": "sulfato-de-amonio", "nombre": "Sulfato de amonio", "n_pct": 21.0, "unidad": "kg", "precio": 7.4},
    {"id": "nitrato-de-amonio", "nombre": "Nitrato de amonio", "n_pct": 33.5, "unidad": "kg", "precio": 12.9},
]


# ── Acceso ───────────────────────────────────────────────────────────────────


def _require_admin(db: Dict[str, Any], authorization: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Solo administradores. Devuelve el usuario y su empresa."""
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    user_id = str(user.get("id") or "")
    admin = active_admin_row(db, user_id)
    if not admin or admin.get("admin_role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Formularios de campo es solo para administradores")
    return user, _company_for_user(db, user_id)


def _in_scope(row: Dict[str, Any], *, company_id: Optional[str], user_id: str) -> bool:
    row_company = str(row.get("company_id") or "")
    if company_id and row_company:
        return row_company == str(company_id)
    return str(row.get("user_id") or "") == user_id


# ── Catálogo de productos ────────────────────────────────────────────────────


def _products_for(db: Dict[str, Any], company_id: Optional[str], user_id: str) -> List[Dict[str, Any]]:
    rows = [dict(row) for row in table(db, PRODUCTS_TABLE) if _in_scope(row, company_id=company_id, user_id=user_id)]
    if rows:
        return sorted(rows, key=lambda row: str(row.get("nombre") or ""))
    # Sin catálogo propio se ofrece el de referencia; se guarda al editarlo.
    return [dict(item, default=True) for item in DEFAULT_PRODUCTS]


def _clean_product(raw: Dict[str, Any], *, user_id: str, company_id: Optional[str], timestamp: str) -> Dict[str, Any]:
    nombre = str(raw.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="Cada producto necesita un nombre.")
    try:
        n_pct = float(raw.get("n_pct"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"El % de N de «{nombre}» debe ser un número.")
    if not 0 <= n_pct <= 100:
        raise HTTPException(status_code=400, detail=f"El % de N de «{nombre}» debe estar entre 0 y 100.")
    precio = raw.get("precio")
    try:
        precio = float(precio) if precio not in (None, "") else None
    except (TypeError, ValueError):
        precio = None
    return {
        "id": str(raw.get("id") or "").strip() or str(uuid.uuid4()),
        "user_id": user_id,
        "company_id": company_id,
        "nombre": nombre,
        "n_pct": n_pct,
        "unidad": str(raw.get("unidad") or "kg").strip() or "kg",
        "precio": precio,
        "updated_at": timestamp,
    }


# ── Lectura ──────────────────────────────────────────────────────────────────

RECORD_FIELDS = (
    "id", "form_key", "folio", "parcela_clave", "lat", "lng", "fecha_actividad", "actividad", "producto",
    "unidad", "cantidad_ha", "precio_unidad", "observaciones", "capturado_por", "digiforms_submission_id",
    "origen", "sincronizado_en", "created_at",
)


@router.get("")
def get_field_forms(authorization: Optional[str] = Header(default=None)):
    """Todo lo que la vista necesita en una sola petición (el store se lee entero en cada una)."""
    with LOCK:
        db = read_db()
        user, company_id = _require_admin(db, authorization)
        user_id = str(user.get("id") or "")
        records = [
            {key: row.get(key) for key in RECORD_FIELDS}
            for row in table(db, RECORDS_TABLE)
            if _in_scope(row, company_id=company_id, user_id=user_id)
        ]
        profiles = [
            {key: value for key, value in row.items() if key not in {"user_id", "company_id", "curp"}}
            for row in table(db, PROFILES_TABLE)
            if _in_scope(row, company_id=company_id, user_id=user_id)
        ]
        products = _products_for(db, company_id, user_id)
    records.sort(key=lambda row: str(row.get("fecha_actividad") or ""))
    by_form: Dict[str, int] = {}
    for row in records:
        key = str(row.get("form_key") or FORM_KEY)
        by_form[key] = by_form.get(key, 0) + 1
    forms = [{"key": FORM_KEY, "name": FORM_NAME, "count": by_form.get(FORM_KEY, 0), "digiforms_url": None}]
    return {
        "data": {"forms": forms, "profiles": profiles, "records": records, "products": products},
        "error": None,
    }


@router.put("/products")
def replace_products(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Reemplaza el catálogo de productos (% N) de la empresa."""
    incoming = payload.get("products")
    if not isinstance(incoming, list) or not incoming:
        raise HTTPException(status_code=400, detail="El cuerpo debe traer la lista completa en 'products'.")
    if len(incoming) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 productos.")
    timestamp = now()
    with LOCK:
        db = read_db()
        user, company_id = _require_admin(db, authorization)
        user_id = str(user.get("id") or "")
        cleaned = [_clean_product(raw if isinstance(raw, dict) else {}, user_id=user_id, company_id=company_id, timestamp=timestamp) for raw in incoming]
        rows = table(db, PRODUCTS_TABLE)
        db["tables"][PRODUCTS_TABLE] = [row for row in rows if not _in_scope(row, company_id=company_id, user_id=user_id)] + cleaned
        write_db(db)
    return {"data": cleaned, "error": None, "message": "Catálogo de productos guardado."}


# ── Demo (cuenta de administración, parcelas de Guatemala) ───────────────────


def _parcel_geometry(row: Dict[str, Any]) -> Optional[BaseGeometry]:
    raw = row.get("geometry_geojson") or row.get("geometry")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        if raw.get("type") == "FeatureCollection":
            parts = [shape(f["geometry"]) for f in raw.get("features") or [] if isinstance(f, dict) and f.get("geometry")]
            geom = unary_union(parts) if parts else None
        elif raw.get("type") == "Feature":
            geom = shape(raw["geometry"]) if raw.get("geometry") else None
        else:
            geom = shape(raw)
    except Exception:
        return None
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.is_empty:
        return None
    return geom


def _in_guatemala(geom: BaseGeometry) -> bool:
    c = geom.centroid
    west, south, east, north = GUATEMALA_BBOX
    return west <= c.x <= east and south <= c.y <= north


def _metres_to_degrees(metres: float, lat: float) -> float:
    """Aproximación suficiente para márgenes de unos metros."""
    return metres / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))


def _area_ha(geom: BaseGeometry) -> float:
    lat = geom.centroid.y
    kx = 111_320.0 * math.cos(math.radians(lat))
    ky = 110_540.0
    from shapely.affinity import scale

    return scale(geom, xfact=kx, yfact=ky, origin=(0, 0)).area / 10_000.0


def _point_inside(rng: random.Random, inner: BaseGeometry) -> Tuple[float, float]:
    minx, miny, maxx, maxy = inner.bounds
    for _ in range(5_000):
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if inner.contains(Point(x, y)):
            return x, y
    p = inner.representative_point()
    return p.x, p.y


def _point_outside(rng: random.Random, geom: BaseGeometry, others: BaseGeometry) -> Tuple[float, float]:
    """Un punto fuera del lindero, a menos de 150 m de él y fuera de cualquier otra parcela."""
    lat = geom.centroid.y
    boundary = geom.boundary
    for _ in range(500):
        anchor = boundary.interpolate(rng.uniform(0, boundary.length))
        c = geom.centroid
        dx, dy = anchor.x - c.x, anchor.y - c.y
        norm = math.hypot(dx, dy) or 1.0
        step = _metres_to_degrees(rng.uniform(35, 110), lat)
        candidate = Point(anchor.x + dx / norm * step, anchor.y + dy / norm * step)
        if others.contains(candidate):
            continue
        distance_m = candidate.distance(geom) * 111_320.0 * math.cos(math.radians(lat))
        if 5 < distance_m < 150:
            return candidate.x, candidate.y
    p = boundary.interpolate(0)
    return p.x + _metres_to_degrees(40, lat), p.y


def _crop_for(db: Dict[str, Any], parcel_id: str) -> Optional[str]:
    for row in table(db, "parcel_crops"):
        if str(row.get("parcel_id") or "") != parcel_id:
            continue
        for key in ("crop_name", "crop_type", "crop", "cultivo", "name"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return None


NOMBRES = [
    ("Rosa Elvira", "Tzul Pérez"), ("Marco Antonio", "Cifuentes López"), ("Ana Lucía", "Boc Xol"),
    ("Juan Carlos", "Méndez Ajú"), ("Silvia", "Chávez Morales"), ("Pedro Pablo", "Coyoy Ixcot"),
    ("María José", "Recinos Solís"), ("Héctor", "Barrios Cardona"), ("Lilian", "Gómez Pac"),
    ("Óscar Rolando", "Ramírez Quan"),
]
ASESORES = ["Ing. Daniela Ortiz", "Ing. Luis Fernando Monzón", "Ing. Karla Sical"]
HIBRIDOS = {"Maíz": ["DK-390", "P4082W", "HS-23"], "Frijol": ["ICTA Ligero", "ICTA Peten"], "Sorgo": ["ICTA Mitlán"]}
# Porcentaje de la meta de N que alcanza cada parcela de la demo, para que
# salgan los cuatro estados (dentro, al límite, excede, sin fertilizar).
OBJETIVOS = [0.82, 0.97, 1.09, 0.0, 0.74, 0.99, 0.88, 1.06, 0.0, 0.79]


def _demo_events(siembra_offset: int, amon_kg: float, map_kg: float, bio: str) -> List[Tuple[int, str, str, str, float, float, str]]:
    """(días respecto a la siembra, actividad, producto, unidad, cantidad/ha, precio, observaciones)."""
    events = [
        (-92, "Otra", "Barbecho", "servicio", 1, 1450, "Barbecho con arado de discos."),
        (-74, "Otra", "Rastreo doble", "servicio", 1, 980, "Dos pasos de rastra."),
        (-55, "Otra", "Surcado a 80 cm", "servicio", 1, 720, ""),
        (-30, "Riego", "Riego de presiembra", "servicio", 1, 420, "Lámina aproximada de 18 cm."),
        (-24, "Otra", "Monitoreo de humedad", "servicio", 0, 0, "Suelo saturado en cabecera; esperar oreo."),
        (-14, "Otra", "Rastra de sellado", "servicio", 1, 650, "Sellado posterior a la aplicación de fondo."),
        (-9, "Otra", "Monitoreo de humedad", "servicio", 0, 0, "Humedad a punto de siembra."),
        (-1, "Aplicación biofertilizante", bio, "kg", 1, 310, "Tratamiento a la semilla."),
        (0, "Otra", "Siembra", "kg", 22, 190, "Siembra con sembradora de precisión."),
        (2, "Otra", "Monitoreo de emergencia", "servicio", 0, 0, "Emergencia uniforme, sin fallas visibles."),
        (3, "Otra", "Monitoreo de plagas", "servicio", 0, 0, "Trampas de feromona instaladas; sin capturas."),
        (6, "Riego", "Riego de auxilio", "servicio", 1, 380, "Turno asignado por el módulo de riego."),
        (9, "Otra", "Monitoreo de plagas", "servicio", 0, 0, "Sin presencia de gusano cogollero."),
        (14, "Aplicación biofertilizante", "BIONAT", "L", 1, 265, "Aplicación foliar."),
        (17, "Otra", "Monitoreo de plagas", "servicio", 0, 0, "Daño de cogollero por debajo del umbral."),
        (20, "Riego", "Riego de auxilio", "servicio", 1, 380, ""),
    ]
    if amon_kg > 0:
        events.append((-18, "Fertilización química", "Amoniaco anhidro", "kg", round(amon_kg), 14.2, "Fertilización de fondo con cuchilla."))
    if map_kg > 0:
        events.append((0, "Fertilización química", "MAP 11-52-00", "kg", round(map_kg), 15.8, "Arrancador en banda a la siembra."))
    return events


def _clear_demo(db: Dict[str, Any]) -> int:
    removed = 0
    for name in (PROFILES_TABLE, RECORDS_TABLE):
        rows = table(db, name)
        kept = [row for row in rows if row.get("seed_batch") != DEMO_BATCH]
        removed += len(rows) - len(kept)
        db["tables"][name] = kept
    return removed


def seed_demo(db: Dict[str, Any], user: Dict[str, Any], *, parcel_ids: Optional[List[str]] = None, today: Optional[datetime] = None) -> Dict[str, Any]:
    """Siembra la demo sobre las parcelas reales de Guatemala del usuario. Idempotente."""
    user_id = str(user.get("id") or "")
    company_id = _company_for_user(db, user_id)
    candidates = []
    for row in visible_parcels(db, user_id):
        geom = _parcel_geometry(row)
        if geom is None or not _in_guatemala(geom):
            continue
        if parcel_ids and str(row.get("id")) not in parcel_ids:
            continue
        candidates.append((row, geom))
    candidates.sort(key=lambda item: str(item[0].get("name") or item[0].get("id")))
    if len(candidates) < 6:
        nombres = ", ".join(str(r.get("name") or r.get("id")) for r, _ in candidates) or "ninguna"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Solo hay {len(candidates)} parcelas de Guatemala en esta cuenta ({nombres}); "
                "hacen falta al menos 6. No se inventan parcelas."
            ),
        )
    chosen = candidates[:10]
    union_all = unary_union([geom for _, geom in candidates])

    _clear_demo(db)
    rng = random.Random(20261013)
    today = today or datetime.now(timezone(timedelta(hours=-6)))
    today = today.replace(minute=0, second=0, microsecond=0)
    timestamp = now()
    profiles: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    upcoming_index = len(chosen) - 1  # una parcela con siembra próxima y sin registros recientes

    for index, (parcel, geom) in enumerate(chosen):
        parcel_id = str(parcel.get("id"))
        clave = parcel_id
        area = parcel.get("area")
        try:
            superficie = round(float(area), 2) if area not in (None, "") and float(area) > 0 else round(_area_ha(geom), 2)
        except (TypeError, ValueError):
            superficie = round(_area_ha(geom), 2)
        municipio = next(
            (str(parcel.get(key)).strip() for key in ("municipio", "municipality", "finca", "zona", "zone") if str(parcel.get(key) or "").strip()),
            "Sin municipio en la parcela",
        )
        cultivo = _crop_for(db, parcel_id) or "Maíz"
        opcion = "A" if index % 2 == 0 else "B"
        hist_amon = 300.0
        hist_map = 100.0
        products_n = {"amon": 0.82, "map": 0.11}
        n_hist = hist_amon * products_n["amon"] + hist_map * products_n["map"]
        meta = n_hist * (1 - (0.15 if opcion == "A" else 0.30))
        objetivo = OBJETIVOS[index % len(OBJETIVOS)]
        map_kg = 60.0 if objetivo > 0 else 0.0
        amon_kg = max((objetivo * meta - map_kg * products_n["map"]) / products_n["amon"], 0.0) if objetivo > 0 else 0.0

        if index == upcoming_index:
            siembra_offset = -6  # siembra dentro de 6 días
        else:
            siembra_offset = 6 + (index * 3) % 19  # sembrada hace 6–24 días
        fecha_siembra = (today - timedelta(days=siembra_offset)).date()
        nombre, apellidos = NOMBRES[index % len(NOMBRES)]
        asesor = ASESORES[index % len(ASESORES)]
        hibrido = rng.choice(HIBRIDOS.get(cultivo, HIBRIDOS["Maíz"])) if cultivo in HIBRIDOS else "Variedad local"
        profiles.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DEMO_BATCH}/{parcel_id}")),
            "user_id": user_id,
            "company_id": company_id,
            "form_key": FORM_KEY,
            "parcela_clave": clave,
            "parcel_id": parcel_id,
            "nombre_parcela": parcel.get("name"),
            "parafinanciera": "Parafinanciera Sustenta del Sur",
            "municipio": municipio,
            "superficie_ha": superficie,
            "productor": {
                "nombre": nombre,
                "apellidos": apellidos,
                "telefono": f"+502 5{rng.randint(100, 999)} {rng.randint(1000, 9999)}",
                "curp_registrada": True,
            },
            "asesor": asesor,
            "ciclo_actual": "2026-B",
            "cultivo": cultivo,
            "hibrido_variedad": hibrido,
            "fecha_siembra": fecha_siembra.isoformat(),
            "densidad_siembra": 72_000 + 2_000 * (index % 4),
            "opcion_reduccion": opcion,
            "biofertilizante_preferente": "TRICHO-SIN + BIONAT" if index % 3 else "TRICHO-SIN",
            "ciclo_anterior": {
                "rendimiento_t_ha": round(7.4 + rng.uniform(-0.9, 1.2), 1),
                "precio_t": 4_850,
                "costo_ha": round(23_800 + rng.uniform(-1_500, 1_800)),
            },
            "fert_historica": [
                {"producto": "Amoniaco anhidro", "cantidad": hist_amon, "unidad": "kg", "precio": 14.2},
                {"producto": "MAP 11-52-00", "cantidad": hist_map, "unidad": "kg", "precio": 15.8},
            ],
            "origen": "demo",
            "seed_batch": DEMO_BATCH,
            "created_at": timestamp,
            "updated_at": timestamp,
        })

        inner = geom.buffer(-_metres_to_degrees(10, geom.centroid.y))
        if inner.is_empty:
            inner = geom
        bio = "TRICHO-SIN"
        for offset, actividad, producto, unidad, cantidad, precio, obs in _demo_events(siembra_offset, amon_kg, map_kg, bio):
            ago = siembra_offset - offset
            if index == upcoming_index:
                # Parcela por sembrar: solo labores de preparación y la última hace ≥ 14 días.
                ago = max(ago, 15)
                if offset > -14:
                    continue
            if ago < 0 or ago > 120:
                continue
            when = today - timedelta(days=ago)
            when = when.replace(hour=7 + rng.randint(0, 8), minute=rng.randint(0, 59))
            if when > today:
                when = today.replace(hour=max(today.hour - 1, 0))
            lng, lat = _point_inside(rng, inner)
            records.append({
                "user_id": user_id,
                "company_id": company_id,
                "form_key": FORM_KEY,
                "parcela_clave": clave,
                "lat": round(lat, 7),
                "lng": round(lng, 7),
                "fecha_actividad": when.strftime("%Y-%m-%dT%H:%M"),
                "actividad": actividad,
                "producto": producto,
                "unidad": unidad,
                "cantidad_ha": cantidad,
                "precio_unidad": precio,
                "observaciones": obs,
                "capturado_por": asesor,
                "origen": "demo",
                "seed_batch": DEMO_BATCH,
                "sincronizado_en": timestamp,
                "created_at": timestamp,
            })

    # Entre 80 y 100 registros: con muchas parcelas sobran visitas de
    # monitoreo; se recortan las más antiguas (no mueven costos ni nitrógeno).
    while len(records) > 96:
        monitoreos = [r for r in records if r["cantidad_ha"] == 0]
        if not monitoreos:
            break
        records.remove(min(monitoreos, key=lambda r: r["fecha_actividad"]))

    # Tres registros capturados a propósito fuera del lindero (en el camino).
    by_parcel: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        by_parcel.setdefault(row["parcela_clave"], []).append(row)
    geoms = {str(p.get("id")): g for p, g in chosen}
    moved = 0
    for clave, rows in by_parcel.items():
        if moved >= 3:
            break
        target = sorted(rows, key=lambda r: r["fecha_actividad"])[-1]
        lng, lat = _point_outside(rng, geoms[clave], union_all)
        target["lng"], target["lat"] = round(lng, 7), round(lat, 7)
        target["observaciones"] = (target["observaciones"] + " Capturado desde el camino de acceso.").strip()
        moved += 1

    records.sort(key=lambda r: r["fecha_actividad"])
    for i, row in enumerate(records):
        row["folio"] = f"BS-{2140 + i}"
        row["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DEMO_BATCH}/{row['folio']}"))
        row["digiforms_submission_id"] = f"demo-{2140 + i}"

    table(db, PROFILES_TABLE).extend(profiles)
    table(db, RECORDS_TABLE).extend(records)
    last30 = sum(1 for r in records if r["fecha_actividad"] >= (today - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"))
    return {
        "parcelas": [
            {
                "id": p["parcel_id"],
                "name": p["nombre_parcela"],
                "superficie_ha": p["superficie_ha"],
                "centroide": [round(geoms[p["parcel_id"]].centroid.y, 5), round(geoms[p["parcel_id"]].centroid.x, 5)],
            }
            for p in profiles
        ],
        "registros": len(records),
        "registros_ultimos_30_dias": last30,
        "fuera_de_lindero": moved,
        "seed_batch": DEMO_BATCH,
    }


@router.post("/demo")
def demo(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Carga (`seed`) o borra (`clear`) la demo. Solo el superadmin de la plataforma.

    Solo toca filas con `seed_batch = 'sustenta-demo-gt'`: los datos reales de
    DigiForms no se modifican nunca.
    """
    action = str(payload.get("action") or "seed")
    if action not in {"seed", "clear"}:
        raise HTTPException(status_code=400, detail="action debe ser 'seed' o 'clear'.")
    parcel_ids = payload.get("parcel_ids")
    if parcel_ids is not None and not isinstance(parcel_ids, list):
        raise HTTPException(status_code=400, detail="parcel_ids debe ser una lista.")
    with LOCK:
        db = read_db()
        user, _ = _require_admin(db, authorization)
        if not _is_platform_superadmin(db, str(user.get("id") or "")):
            raise HTTPException(status_code=403, detail="Solo el superadministrador de Dataris carga la demo")
        if action == "clear":
            removed = _clear_demo(db)
            write_db(db)
            return {"data": {"eliminados": removed, "seed_batch": DEMO_BATCH}, "error": None}
        result = seed_demo(db, user, parcel_ids=[str(p) for p in parcel_ids] if parcel_ids else None)
        write_db(db)
    return {"data": result, "error": None}
