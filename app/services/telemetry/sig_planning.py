"""Cruce de un shapefile de lotes con una tabla Excel de planificación.

Caso de uso: el usuario tiene un shapefile con un identificador de lote (p.ej.
`ID`) y un Excel con una fila por lote (zona, administrador, finca, variedad,
fechas de corte, etc.). Este módulo une ambos por el identificador y devuelve
un GeoJSON con las propiedades combinadas, más la lista de columnas del Excel
que son aptas para "colorear por categoría" (symbology categórica, el
equivalente a un "categorized renderer" de QGIS/ArcGIS).

No persiste nada: es un cruce en memoria pensado para previsualizar y
explorar el mapeo, no para guardar un dataset permanente.
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping

MAX_CATEGORICAL_UNIQUE_VALUES = 60
MIN_CATEGORICAL_UNIQUE_VALUES = 2
MAX_FEATURES = 20_000


def _normalize_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    # Excel a veces guarda IDs numéricos con ".0" mientras el shapefile los
    # trae como texto con ceros a la izquierda (ej. "04105").
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _find_id_column(columns: List[str], hint: Optional[str]) -> Optional[str]:
    normalized = {col: str(col).strip().lower() for col in columns}
    if hint:
        hint_norm = hint.strip().lower()
        for col, norm in normalized.items():
            if norm == hint_norm:
                return col
    for col, norm in normalized.items():
        if norm == "id":
            return col
    for col, norm in normalized.items():
        if norm.endswith("_id") or norm.endswith(" id") or norm == "codigo" or norm == "código":
            return col
    return None


def _safe_extract_zip(content: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("../") or "/../" in name:
                continue
            target = (destination / name).resolve()
            if not str(target).startswith(str(destination.resolve())):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                dst.write(src.read())


def _read_shapefile(content: bytes) -> gpd.GeoDataFrame:
    with tempfile.TemporaryDirectory(prefix="dataris-sig-shp-") as tmp_name:
        tmp = Path(tmp_name)
        try:
            _safe_extract_zip(content, tmp)
        except zipfile.BadZipFile as exc:
            raise ValueError("El archivo de lotes debe ser un .zip válido con el shapefile") from exc

        shp_candidates = sorted(tmp.rglob("*.shp"), key=lambda p: len(str(p)))
        if not shp_candidates:
            raise ValueError("El ZIP no contiene ningún archivo .shp")

        gdf = gpd.read_file(shp_candidates[0])

    if gdf.empty:
        raise ValueError("El shapefile no contiene geometrías")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326", allow_override=True)
    gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty:
        raise ValueError("El shapefile no tiene geometrías válidas")
    if len(gdf) > MAX_FEATURES:
        raise ValueError(f"El shapefile tiene {len(gdf)} lotes; el máximo soportado es {MAX_FEATURES}")
    return gdf


def _read_excel_table(content: bytes) -> pd.DataFrame:
    try:
        excel_file = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as exc:
        raise ValueError(f"No se pudo leer el archivo Excel: {exc}") from exc

    best_df: Optional[pd.DataFrame] = None
    for sheet_name in excel_file.sheet_names:
        try:
            # Primero solo encabezados: pandas convierte de forma automática una
            # columna de puros dígitos a int64 y así se pierden ceros a la
            # izquierda (ej. "06505" -> 6505). Se detecta la columna ID por
            # nombre y se fuerza a texto antes de leer los datos completos.
            header_df = excel_file.parse(sheet_name=sheet_name, nrows=0)
        except Exception:
            continue
        id_col = _find_id_column(list(header_df.columns), None)
        if id_col is None:
            continue
        df = excel_file.parse(sheet_name=sheet_name, dtype={id_col: str})
        df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
        if df.empty:
            continue
        if best_df is None or len(df) > len(best_df):
            best_df = df
    if best_df is None:
        raise ValueError(
            "No se encontró en el Excel una hoja con una columna de identificador "
            "(ID/Código) para cruzar con el shapefile"
        )
    return best_df


def _is_categorical(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series) or pd.api.types.is_numeric_dtype(series):
        return False
    values = series.dropna().astype(str).str.strip()
    values = values[(values != "") & (values.str.lower() != "nan")]
    if values.empty:
        return False
    unique = values.nunique()
    return MIN_CATEGORICAL_UNIQUE_VALUES <= unique <= MAX_CATEGORICAL_UNIQUE_VALUES


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # numpy/pandas scalars (np.float64, np.int64, ...) no son serializables a
    # JSON directamente; .item() los convierte a su equivalente nativo de Python.
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            value = str(value)
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return str(value)


def process_planning_upload(
    shapefile_zip_bytes: bytes,
    excel_bytes: bytes,
    id_field_hint: Optional[str] = None,
) -> Dict[str, Any]:
    gdf = _read_shapefile(shapefile_zip_bytes)
    shp_id_col = _find_id_column(list(gdf.columns), id_field_hint)
    if not shp_id_col:
        raise ValueError("El shapefile no tiene una columna identificadora (ID) para cruzar con el Excel")

    excel_df = _read_excel_table(excel_bytes)
    excel_id_col = _find_id_column(list(excel_df.columns), id_field_hint)
    if not excel_id_col:
        raise ValueError("El Excel no tiene una columna identificadora (ID) para cruzar con el shapefile")

    excel_df = excel_df.copy()
    excel_df["_join_id"] = excel_df[excel_id_col].map(_normalize_id)
    excel_df = excel_df[excel_df["_join_id"].notna()]
    excel_df = excel_df.drop_duplicates(subset="_join_id", keep="first").set_index("_join_id")

    excel_columns = [c for c in excel_df.columns if c not in (excel_id_col, "_join_id")]
    categorical_fields = [c for c in excel_columns if _is_categorical(excel_df[c])]

    metric_crs = None
    try:
        metric_crs = gdf.estimate_utm_crs()
    except Exception:
        metric_crs = None
    areas_ha = None
    if metric_crs is not None:
        try:
            areas_ha = gdf.geometry.to_crs(metric_crs).area / 10000.0
        except Exception:
            areas_ha = None

    features: List[Dict[str, Any]] = []
    matched_ids: set[str] = set()
    area_total_ha = 0.0

    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        join_id = _normalize_id(row[shp_id_col])
        props: Dict[str, Any] = {"__sig_id": join_id if join_id is not None else _jsonable(row[shp_id_col])}

        excel_row = excel_df.loc[join_id] if join_id is not None and join_id in excel_df.index else None
        if excel_row is not None:
            matched_ids.add(join_id)
            for col in excel_columns:
                props[str(col)] = _jsonable(excel_row[col])
            props["__sig_matched"] = True
        else:
            props["__sig_matched"] = False

        if areas_ha is not None:
            area_ha = float(areas_ha.loc[idx])
            area_total_ha += area_ha
            props["__sig_area_ha"] = round(area_ha, 4)

        features.append({"type": "Feature", "geometry": mapping(geom), "properties": props})

    excel_unmatched = int((~pd.Series(list(excel_df.index)).isin(matched_ids)).sum())

    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "categoricalFields": [str(c) for c in categorical_fields],
        "allFields": [str(c) for c in excel_columns],
        "idField": str(shp_id_col),
        "stats": {
            "totalFeatures": len(features),
            "matched": len(matched_ids),
            "unmatchedShapefile": len(features) - len(matched_ids),
            "unmatchedExcel": excel_unmatched,
            "totalAreaHa": round(area_total_ha, 2) if areas_ha is not None else None,
        },
    }
