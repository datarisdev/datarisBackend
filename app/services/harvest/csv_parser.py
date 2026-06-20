"""Parser de CSV para cosecha mecánica.

Soporta dos formatos:
  - COSECHA_MECANICA: separador ';', columnas en español (máquina cosechadora)
  - YIELD_MONITOR:    separador ',', columnas en inglés (monitor de rendimiento Greentronic)

La detección del formato es automática por nombre de columnas.
Hace forward-fill de las columnas de metadatos que se rellenan solo en la primera fila.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Mapeo de columnas por formato ────────────────────────────────────────────

_CM_MAP: dict[str, list[str]] = {
    "lat":        ["LATITUD", "Latitud", "latitud"],
    "lng":        ["LONGITUD", "Longitud", "longitud"],
    "velocidad":  ["VELOCIDAD (Km/H)", "VELOCIDAD", "Velocidad"],
    "presion":    ["PRESION DE CORTADOR BASE (Bar)", "PRESION CORTADOR BASE", "PRESION DE CORTADOR BASE"],
    "tch":        ["TCH"],
    "tah":        ["TAH"],
    "rpm":        ["RPM"],
    "combustible":["COMBUSTIBLE"],
    "calidad_gps":["CALIDAD GPS"],
    "piloto_auto":["PILOTO AUTOMATICO", "Piloto Automático"],
    "modo_corte": ["MODO DE CORTE BASE", "MODO DE CORTE"],
    "timestamp":  ["FECHA_HORA_TRAZA", "FECHA INICIO"],
    # metadata (se hace ffill)
    "finca":      ["NOMBRE FINCA", "FINCA"],
    "lote":       ["LOTE"],
    "operador":   ["OPERADOR"],
    "responsable":["RESPONSABLE"],
    "equipo":     ["EQUIPO"],
    "zafra":      ["ZAFRA"],
}

_YM_MAP: dict[str, list[str]] = {
    "lat":          ["Latitude", "latitude", "LAT", "lat"],
    "lng":          ["Longitude", "longitude", "LNG", "LON"],
    "velocidad":    ["GroundSpeed(km/h)", "GroundSpeed"],
    "rpm":          ["RPM"],
    "calidad_gps":  ["GPSQuality"],
    "rendimiento":  ["Yield(kg /ha )", "Yield(kg/ha)", "Yield"],
    "peso":         ["Weight(kg )", "Weight(kg)"],
    "ancho":        ["SwathWidth(cm)", "SwathWidth"],
    "finca":        ["FieldName"],
    "lote":         ["Field"],
    "timestamp_d":  ["Date"],
    "timestamp_t":  ["Time"],
}

_META_COLS_CM = ["finca", "lote", "operador", "responsable", "equipo", "zafra"]


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _find(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
        for actual in df.columns:
            if actual.strip().upper() == c.upper():
                return actual
    return None


def _to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", ".").str.strip(),
        errors="coerce",
    )


def _compute_estado(vel: float | None, rpm: float | None) -> str:
    v = vel or 0.0
    r = rpm or 0.0
    if v <= 0:
        return "detenido"
    if v > 0.5 and r > 1800:
        return "cosechando"
    if v > 0.5:
        return "traslado"
    return "ralenti"


def _read_df(content: bytes) -> pd.DataFrame:
    """Intenta leer el CSV con distintos separadores y encodings."""
    for sep in (";", ",", "\t"):
        for enc in ("utf-8", "latin1", "utf-8-sig"):
            try:
                df = pd.read_csv(
                    io.BytesIO(content),
                    sep=sep,
                    encoding=enc,
                    low_memory=False,
                    on_bad_lines="skip",
                )
                if len(df.columns) >= 4 and len(df) > 0:
                    return df
            except Exception:
                continue
    raise ValueError("No se pudo leer el CSV con ningún separador/encoding conocido")


def _detect_format(df: pd.DataFrame) -> str:
    upper = {c.upper().strip() for c in df.columns}
    if any("LATITUD" in c for c in upper):
        return "COSECHA_MECANICA"
    if any("LATITUDE" in c for c in upper):
        return "YIELD_MONITOR"
    return "UNKNOWN"


# ── Parsers por formato ────────────────────────────────────────────────────────

def _parse_cosecha_mecanica(df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cols = {k: _find(df, v) for k, v in _CM_MAP.items()}

    # Forward-fill columnas de metadatos (solo la primera fila las tiene)
    for key in _META_COLS_CM:
        c = cols.get(key)
        if c:
            df[c] = df[c].replace("", np.nan).ffill()

    # Limpiar y convertir coordenadas
    lat_col, lng_col = cols.get("lat"), cols.get("lng")
    if not lat_col or not lng_col:
        raise ValueError("No se encontraron columnas de latitud/longitud en COSECHA_MECANICA")

    df[lat_col] = _to_float(df[lat_col])
    df[lng_col] = _to_float(df[lng_col])
    df = df.dropna(subset=[lat_col, lng_col])
    df = df[(df[lat_col].abs() <= 90) & (df[lng_col].abs() <= 180)]
    df = df[(df[lat_col] != 0) | (df[lng_col] != 0)]

    for key in ("velocidad", "presion", "tch", "tah", "combustible"):
        c = cols.get(key)
        if c:
            df[c] = _to_float(df[c])
    for key in ("rpm", "calidad_gps"):
        c = cols.get(key)
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    def _safe(row: pd.Series, key: str) -> Any:
        c = cols.get(key)
        if not c:
            return None
        val = row[c]
        return None if pd.isna(val) else val

    points: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        lat = float(row[lat_col])
        lng = float(row[lng_col])
        vel = float(_safe(row, "velocidad") or 0)
        rpm_val = _safe(row, "rpm")
        rpm_int = int(rpm_val) if rpm_val is not None else None

        points.append({
            "lat": lat,
            "lng": lng,
            "velocidad_kmh": vel or None,
            "presion_cortador_bar": _safe(row, "presion"),
            "tch": _safe(row, "tch"),
            "tah": _safe(row, "tah"),
            "rpm": rpm_int,
            "combustible": _safe(row, "combustible"),
            "calidad_gps": int(_safe(row, "calidad_gps")) if _safe(row, "calidad_gps") is not None else None,
            "piloto_automatico": str(_safe(row, "piloto_auto") or "").strip() or None,
            "modo_corte": str(_safe(row, "modo_corte") or "").strip() or None,
            "estado": _compute_estado(vel, float(rpm_int or 0)),
        })

    meta = {}
    for key in _META_COLS_CM:
        c = cols.get(key)
        if c:
            vals = df[c].dropna()
            if len(vals):
                meta[key] = str(vals.iloc[0]).strip()

    return points, meta


def _parse_yield_monitor(df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cols = {k: _find(df, v) for k, v in _YM_MAP.items()}

    lat_col, lng_col = cols.get("lat"), cols.get("lng")
    if not lat_col or not lng_col:
        raise ValueError("No se encontraron columnas Latitude/Longitude en YIELD_MONITOR")

    df[lat_col] = _to_float(df[lat_col])
    df[lng_col] = _to_float(df[lng_col])
    df = df.dropna(subset=[lat_col, lng_col])
    df = df[(df[lat_col].abs() <= 90) & (df[lng_col].abs() <= 180)]
    df = df[(df[lat_col] != 0) | (df[lng_col] != 0)]

    for key in ("velocidad", "rendimiento", "peso", "ancho"):
        c = cols.get(key)
        if c:
            df[c] = _to_float(df[c])
    for key in ("rpm", "calidad_gps"):
        c = cols.get(key)
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    def _safe(row: pd.Series, key: str) -> Any:
        c = cols.get(key)
        if not c:
            return None
        val = row[c]
        return None if pd.isna(val) else val

    points: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        lat = float(row[lat_col])
        lng = float(row[lng_col])
        vel = float(_safe(row, "velocidad") or 0)
        rpm_val = _safe(row, "rpm")

        points.append({
            "lat": lat,
            "lng": lng,
            "velocidad_kmh": vel or None,
            "rendimiento_kg_ha": _safe(row, "rendimiento"),
            "peso_carga_kg": _safe(row, "peso"),
            "ancho_faena_cm": _safe(row, "ancho"),
            "rpm": int(rpm_val) if rpm_val is not None else None,
            "calidad_gps": int(_safe(row, "calidad_gps")) if _safe(row, "calidad_gps") is not None else None,
            "estado": _compute_estado(vel, float(rpm_val or 0)),
        })

    meta = {}
    for key in ("finca", "lote"):
        c = cols.get(key)
        if c:
            vals = df[c].dropna()
            if len(vals):
                meta[key] = str(vals.iloc[0]).strip()

    return points, meta


# ── Stats pre-calculadas ──────────────────────────────────────────────────────

def compute_stats(points: list[dict[str, Any]], fmt: str) -> dict[str, Any]:
    """Calcula KPIs y distribución de estados en un solo pase sobre los datos."""
    if not points:
        return {}

    def _avg(key: str) -> float | None:
        vals = [p[key] for p in points if p.get(key) is not None]
        return round(float(np.mean(vals)), 3) if vals else None

    total = len(points)
    estado_counts: dict[str, int] = {}
    for p in points:
        e = p.get("estado", "detenido")
        estado_counts[e] = estado_counts.get(e, 0) + 1

    piloto_auto_count = sum(
        1 for p in points
        if str(p.get("piloto_automatico") or "").strip().lower()
        in ("engaged", "yes", "true", "1", "activo", "on")
    )

    stats: dict[str, Any] = {
        "avg_velocidad": _avg("velocidad_kmh"),
        "avg_rpm":       _avg("rpm"),
        "avg_combustible": _avg("combustible"),
        "pct_cosechando": round(estado_counts.get("cosechando", 0) / total * 100, 1),
        "pct_traslado":   round(estado_counts.get("traslado", 0) / total * 100, 1),
        "pct_ralenti":    round(estado_counts.get("ralenti", 0) / total * 100, 1),
        "pct_detenido":   round(estado_counts.get("detenido", 0) / total * 100, 1),
        "pct_piloto_auto": round(piloto_auto_count / total * 100, 1),
        "eficiencia_field": round(estado_counts.get("cosechando", 0) / total * 100, 1),
    }

    if fmt == "COSECHA_MECANICA":
        stats["avg_tch"] = _avg("tch")
        stats["avg_tah"] = _avg("tah")
        stats["avg_presion"] = _avg("presion_cortador_bar")
    else:
        stats["avg_rendimiento_kg_ha"] = _avg("rendimiento_kg_ha")

    return stats


def compute_bbox(points: list[dict[str, Any]]) -> dict[str, float] | None:
    if not points:
        return None
    lats = [p["lat"] for p in points]
    lngs = [p["lng"] for p in points]
    return {
        "min_lat": float(min(lats)),
        "max_lat": float(max(lats)),
        "min_lng": float(min(lngs)),
        "max_lng": float(max(lngs)),
    }


# ── Entrada pública ───────────────────────────────────────────────────────────

def parse_harvest_csv(content: bytes) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Parsea un CSV de cosecha y devuelve (points, formato, metadata).

    points es una lista de dicts listos para insertar en HarvestPoint.
    """
    df = _read_df(content)
    fmt = _detect_format(df)

    logger.info("Formato detectado: %s  (%d filas raw)", fmt, len(df))

    if fmt == "COSECHA_MECANICA":
        points, meta = _parse_cosecha_mecanica(df)
    elif fmt == "YIELD_MONITOR":
        points, meta = _parse_yield_monitor(df)
    else:
        # Intento genérico: buscar Lat/Lon en cualquier nombre
        lat_c = _find(df, ["lat", "latitude", "y", "latitud"])
        lng_c = _find(df, ["lng", "lon", "longitude", "x", "longitud"])
        if not lat_c or not lng_c:
            raise ValueError(
                "Formato de CSV no reconocido. "
                "Se necesitan columnas de latitud/longitud. "
                "Formatos soportados: COSECHA_MECANICA (LATITUD/LONGITUD) y YIELD_MONITOR (Latitude/Longitude)."
            )
        df[lat_c] = _to_float(df[lat_c])
        df[lng_c] = _to_float(df[lng_c])
        df = df.dropna(subset=[lat_c, lng_c])
        points = [
            {"lat": float(r[lat_c]), "lng": float(r[lng_c]), "estado": "detenido"}
            for _, r in df.iterrows()
        ]
        meta = {}

    logger.info("Puntos válidos: %d", len(points))
    return points, fmt, meta
