from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, UploadFile
from openpyxl import load_workbook
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from app.api.routers.compat import LOCK, bearer_user, now, read_db, table, write_db

router = APIRouter(prefix="/compat/sig-agricola", tags=["SIG Agrícola compatibility"])

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_ROWS_PER_IMPORT = 25_000
HARVEST_FORM_TYPE = "harvest"
PEST_WEED_FORM_TYPE = "pest_weed"
DIGIFORMS_EXCEL_SOURCE = "digiforms_excel_export"


def _require_user(authorization: Optional[str]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec="minutes")
    return str(value).strip()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return _safe_text(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _normalize(value: Any) -> str:
    text = _safe_text(value).lower()
    replacements = str.maketrans("áéíóúüñ", "aeiouun")
    return "".join(ch for ch in text.translate(replacements) if ch.isalnum())


def _first_value(row: Dict[str, Any], aliases: Sequence[str], default: Any = "") -> Any:
    normalized = {_normalize(key): value for key, value in row.items() if not str(key).startswith("_")}
    for alias in aliases:
        key = _normalize(alias)
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    return default


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip().replace(" ", ""))
    except (TypeError, ValueError):
        return None
    return number


def _parse_coordinates(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    latitude = _first_value(row, ["latitud", "lat", "latitude"], None)
    longitude = _first_value(row, ["longitud", "lng", "lon", "longitude", "long"], None)
    if latitude not in (None, "") and longitude not in (None, ""):
        lat = _to_float(latitude)
        lng = _to_float(longitude)
        if lat is not None and lng is not None and not (lat == 0 and lng == 0):
            return lat, lng

    raw = _safe_text(
        _first_value(
            row,
            [
                "GeoLocalizacion",
                "Geo Localizacion",
                "Geolocalización",
                "UBICACION",
                "Ubicación",
                "Location",
                "coordenadas",
                "coords",
            ],
        )
    )
    if not raw or raw.replace(" ", "") in {"-1,-1", "0,0"}:
        return None
    parts = [part.strip() for part in raw.replace(";", ",").split(",")]
    if len(parts) < 2:
        return None
    first = _to_float(parts[0])
    second = _to_float(parts[1])
    if first is None or second is None:
        return None
    if abs(first) > 90:
        lng, lat = first, second
    else:
        lat, lng = first, second
    if lat == 0 and lng == 0:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _header_score(values: Sequence[Any], form_type: str) -> int:
    normalized = {_normalize(value) for value in values if value not in (None, "")}
    if form_type == PEST_WEED_FORM_TYPE:
        targets = {"idrespuesta", "geolocalizacion", "maleza", "plaga", "evidenciafotografica"}
    else:
        targets = {"responseid", "ubicacion", "status", "metododecosecha", "parcela"}
    return sum(1 for target in targets if target in normalized)


def _read_xlsx_rows(content: bytes, form_type: str) -> List[Dict[str, Any]]:
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el Excel .xlsx: {exc}") from exc
    worksheet = workbook[workbook.sheetnames[0]]
    raw_rows = list(worksheet.iter_rows(values_only=False))
    if not raw_rows:
        return []

    search_limit = min(len(raw_rows), 35)
    header_index = max(range(search_limit), key=lambda index: _header_score([cell.value for cell in raw_rows[index]], form_type))
    if _header_score([cell.value for cell in raw_rows[header_index]], form_type) < 2:
        raise HTTPException(status_code=400, detail="No se identificaron encabezados compatibles con la exportación de DigiForms.")

    headers = [_safe_text(cell.value) or f"col_{position + 1}" for position, cell in enumerate(raw_rows[header_index])]
    rows: List[Dict[str, Any]] = []
    for cells in raw_rows[header_index + 1 :]:
        values = [cell.value for cell in cells]
        if not any(value not in (None, "") for value in values):
            continue
        row: Dict[str, Any] = {}
        hyperlinks: Dict[str, str] = {}
        for index, header in enumerate(headers):
            cell = cells[index] if index < len(cells) else None
            value = cell.value if cell is not None else None
            row[header] = value
            if cell is not None and cell.hyperlink and cell.hyperlink.target:
                hyperlinks[_normalize(header)] = str(cell.hyperlink.target)
                hyperlinks[f"col_{index + 1}"] = str(cell.hyperlink.target)
                hyperlinks[f"value_{_normalize(value)}"] = str(cell.hyperlink.target)
        row["_hyperlinks"] = hyperlinks
        rows.append(row)
        if len(rows) >= MAX_ROWS_PER_IMPORT:
            break
    return rows


def _read_csv_rows(content: bytes) -> List[Dict[str, Any]]:
    decoded = content.decode("utf-8-sig", errors="replace")
    sample = decoded[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(decoded), dialect=dialect)
    rows = [dict(row) for row in reader if row]
    return rows[:MAX_ROWS_PER_IMPORT]


def _read_rows(filename: str, content: bytes, form_type: str) -> List[Dict[str, Any]]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        return _read_csv_rows(content)
    if suffix == ".xlsx":
        return _read_xlsx_rows(content, form_type)
    if suffix == ".xls":
        raise HTTPException(status_code=400, detail="El formato .xls antiguo no es compatible. Exporta el reporte de DigiForms como .xlsx o .csv.")
    raise HTTPException(status_code=400, detail="Formato no compatible. Usa una exportación .xlsx o .csv de DigiForms.")


def _parse_parcels(raw: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Las parcelas enviadas no contienen JSON válido.") from exc
    if isinstance(payload, dict):
        payload = payload.get("parcels") or payload.get("data") or []
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="El listado de parcelas debe ser un arreglo JSON.")
    return [parcel for parcel in payload if isinstance(parcel, dict)]


def _feature_name(properties: Dict[str, Any], fallback: str) -> str:
    for key in ["name", "Name", "NAME", "ID", "id", "lote", "Lote", "codigo", "Código"]:
        value = properties.get(key)
        if value not in (None, ""):
            return _safe_text(value)
    return fallback


def _iter_parcel_features(parcels: Sequence[Dict[str, Any]]) -> Iterable[Tuple[str, str, BaseGeometry]]:
    for parcel in parcels:
        parcel_id = _safe_text(parcel.get("id"))
        if not parcel_id:
            continue
        parcel_name = _safe_text(parcel.get("name")) or parcel_id
        geometry = parcel.get("geometry")
        if not isinstance(geometry, dict):
            continue
        if geometry.get("type") == "FeatureCollection" or isinstance(geometry.get("features"), list):
            for feature in geometry.get("features") or []:
                if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                    continue
                try:
                    feature_geometry = shape(feature["geometry"])
                except Exception:
                    continue
                if feature_geometry.is_empty:
                    continue
                name = _feature_name(feature.get("properties") or {}, parcel_name)
                yield parcel_id, f"{parcel_id}::{name}", feature_geometry
            continue
        if geometry.get("type") == "Feature" and isinstance(geometry.get("geometry"), dict):
            try:
                feature_geometry = shape(geometry["geometry"])
            except Exception:
                continue
            if not feature_geometry.is_empty:
                name = _feature_name(geometry.get("properties") or {}, parcel_name)
                yield parcel_id, f"{parcel_id}::{name}", feature_geometry
            continue
        try:
            parcel_geometry = shape(geometry)
        except Exception:
            continue
        if not parcel_geometry.is_empty:
            yield parcel_id, f"{parcel_id}::{parcel_name}", parcel_geometry


def _build_feature_index(parcels: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, BaseGeometry]]:
    return list(_iter_parcel_features(parcels))


def _match_point(lat: float, lng: float, features: Sequence[Tuple[str, str, BaseGeometry]]) -> Tuple[Optional[str], Optional[str]]:
    point = Point(lng, lat)
    for parcel_id, feature_key, geometry in features:
        try:
            if geometry.covers(point):
                return parcel_id, feature_key
        except Exception:
            continue
    return None, None


def _canonical_harvest_state(value: Any) -> str:
    raw = _safe_text(value).lower()
    if any(word in raw for word in ["terminad", "finaliz", "completed", "done", "cosechad"]):
        return "Terminada"
    if any(word in raw for word in ["proceso", "parcial", "progress"]):
        return "En proceso"
    return "No iniciada"


def _stable_external_id(form_type: str, row: Dict[str, Any], index: int) -> str:
    value = _first_value(row, ["Response Id", "IdRespuesta", "response_id", "id_respuesta", "id"], "")
    if value not in (None, ""):
        return _safe_text(value)
    safe_payload = json.dumps(_json_safe(row), sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(f"{form_type}:{index}:{safe_payload}".encode("utf-8")).hexdigest()[:20]
    return f"generated-{digest}"


def _harvest_record(row: Dict[str, Any], index: int, features: Sequence[Tuple[str, str, BaseGeometry]]) -> Optional[Dict[str, Any]]:
    coords = _parse_coordinates(row)
    if not coords:
        return None
    lat, lng = coords
    parcel_id, feature_key = _match_point(lat, lng, features)
    return {
        "external_response_id": _stable_external_id(HARVEST_FORM_TYPE, row, index),
        "parcela": _safe_text(_first_value(row, ["Parcela", "Nombre", "Lote"], f"Parcela {index + 1}")),
        "zona": _safe_text(_first_value(row, ["Zona", "Bloque"], "-")) or "-",
        "metodo": _safe_text(_first_value(row, ["Metodo de Cosecha", "Metodode Cosecha", "Método", "Metodo"], "-")) or "-",
        "canacond": _safe_text(_first_value(row, ["CANACOND", "Condicion", "Condición"], "-")) or "-",
        "terminal": _safe_text(_first_value(row, ["Terminal", "Operador"], "-")) or "-",
        "estado": _canonical_harvest_state(_first_value(row, ["Status", "Estado", "State"], "")),
        "lat": lat,
        "lng": lng,
        "hora_inicio": _safe_text(_first_value(row, ["Hora", "Hora Inicio", "Start At"], "-")) or "-",
        "hora_fin": _safe_text(_first_value(row, ["Hora Finalizacion", "Hora Finalización", "End At"], "-")) or "-",
        "fecha": _safe_text(_first_value(row, ["Fecha"], "-")) or "-",
        "fecha_fin": _safe_text(_first_value(row, ["Fecha Finalizacion", "Fecha Finalización", "Fecha Fin"], "-")) or "-",
        "parcel_id": parcel_id,
        "feature_key": feature_key,
        "outside_registered_parcel": feature_key is None,
        "raw_payload": _json_safe({key: value for key, value in row.items() if key != "_hyperlinks"}),
    }


def _photo_links(row: Dict[str, Any]) -> Tuple[str, str]:
    hyperlinks = row.get("_hyperlinks") or {}
    if not isinstance(hyperlinks, dict):
        return "", ""
    evidence = ""
    signature = ""
    for key, value in hyperlinks.items():
        normalized_key = _normalize(key)
        if not value:
            continue
        if "firma" in normalized_key:
            signature = signature or _safe_text(value)
        elif "evidencia" in normalized_key or "fotograf" in normalized_key:
            evidence = evidence or _safe_text(value)
    if not evidence:
        # DigiForms exports may put the evidence image immediately after the text column.
        evidence = _safe_text(hyperlinks.get("col_23") or hyperlinks.get("col_22") or "")
    if not signature:
        signature = _safe_text(hyperlinks.get("col_24") or hyperlinks.get("col_25") or "")
    return evidence, signature


def _pest_weed_record(row: Dict[str, Any], index: int, features: Sequence[Tuple[str, str, BaseGeometry]]) -> Optional[Dict[str, Any]]:
    coords = _parse_coordinates(row)
    if not coords:
        return None
    lat, lng = coords
    parcel_id, feature_key = _match_point(lat, lng, features)
    photo_url, signature_url = _photo_links(row)
    return {
        "external_response_id": _stable_external_id(PEST_WEED_FORM_TYPE, row, index),
        "terminal": _safe_text(_first_value(row, ["Terminal"], "")),
        "fecha": _safe_text(_first_value(row, ["Fecha"], "")),
        "hora": _safe_text(_first_value(row, ["Hora"], "")),
        "zona": _safe_text(_first_value(row, ["Zona"], "")),
        "maleza": _safe_text(_first_value(row, ["Maleza"], "")),
        "tipo_maleza": _safe_text(_first_value(row, ["TipodeMaleza", "Tipo de Maleza"], "")),
        "plaga": _safe_text(_first_value(row, ["Plaga"], "")),
        "tipo_plaga": _safe_text(_first_value(row, ["TiposdePlaga", "Tipos de Plaga", "Tipo de Plaga"], "")),
        "enfermedades": _safe_text(_first_value(row, ["Enfermedades"], "")),
        "sintomas": _safe_text(_first_value(row, ["Sintomas", "Síntomas"], "")),
        "observaciones": _safe_text(_first_value(row, ["Observaciones"], "")),
        "firma": signature_url,
        "firma_texto": _safe_text(_first_value(row, ["Firma"], "")),
        "evidencia_fotografica": _safe_text(_first_value(row, ["EvidenciaFotografica", "Evidencia Fotografica", "Evidencia Fotográfica"], "")),
        "photo_url": photo_url,
        "signature_url": signature_url,
        "lat": lat,
        "lng": lng,
        "parcel_id": parcel_id,
        "feature_key": feature_key,
        "outside_registered_parcel": feature_key is None,
        "raw_payload": _json_safe({key: value for key, value in row.items() if key != "_hyperlinks"}),
    }


def _record_table_name(form_type: str) -> str:
    return "sig_harvest_records" if form_type == HARVEST_FORM_TYPE else "sig_pest_weed_records"


def _safe_record(row: Dict[str, Any]) -> Dict[str, Any]:
    return dict(row)


def _import_records(
    *,
    db: Dict[str, Any],
    user_id: str,
    form_type: str,
    filename: str,
    content: bytes,
    parcels: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    rows = _read_rows(filename, content, form_type)
    features = _build_feature_index(parcels)
    parser = _harvest_record if form_type == HARVEST_FORM_TYPE else _pest_weed_record
    parsed_records: List[Dict[str, Any]] = []
    invalid_rows = 0
    for index, row in enumerate(rows):
        record = parser(row, index, features)
        if record is None:
            invalid_rows += 1
            continue
        parsed_records.append(record)

    if not parsed_records:
        raise HTTPException(status_code=400, detail="No se encontraron registros con coordenadas válidas en la exportación.")

    import_run_id = str(uuid.uuid4())
    timestamp = now()
    destination = table(db, _record_table_name(form_type))
    existing_by_external = {
        str(item.get("external_response_id")): item
        for item in destination
        if str(item.get("user_id") or "") == user_id and item.get("external_response_id")
    }
    imported_rows = 0
    updated_rows = 0
    duplicate_rows = 0
    seen_in_file: set[str] = set()

    for parsed in parsed_records:
        external_id = str(parsed["external_response_id"])
        if external_id in seen_in_file:
            duplicate_rows += 1
        seen_in_file.add(external_id)
        common = {
            **parsed,
            "user_id": user_id,
            "source": DIGIFORMS_EXCEL_SOURCE,
            "source_file_name": filename,
            "import_run_id": import_run_id,
            "updated_at": timestamp,
        }
        existing = existing_by_external.get(external_id)
        if existing:
            existing.update(common)
            updated_rows += 1
        else:
            item = {"id": str(uuid.uuid4()), "created_at": timestamp, **common}
            destination.append(item)
            existing_by_external[external_id] = item
            imported_rows += 1

    outside_rows = sum(1 for item in parsed_records if item.get("outside_registered_parcel"))
    run = {
        "id": import_run_id,
        "user_id": user_id,
        "form_type": form_type,
        "source": DIGIFORMS_EXCEL_SOURCE,
        "file_name": filename,
        "status": "completed",
        "total_rows": len(rows),
        "valid_rows": len(parsed_records),
        "imported_rows": imported_rows,
        "updated_rows": updated_rows,
        "duplicate_rows": duplicate_rows,
        "invalid_rows": invalid_rows,
        "outside_registered_parcel_rows": outside_rows,
        "parcel_features_received": len(features),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    table(db, "sig_import_runs").append(run)
    return run


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera el límite de 15 MB.")
    return content


@router.get("/integration-status")
def integration_status(authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    with LOCK:
        db = read_db()
        imports = [row for row in table(db, "sig_import_runs") if str(row.get("user_id") or "") == str(user.get("id") or "")]
        latest = max(imports, key=lambda row: str(row.get("created_at") or ""), default=None)
    return {
        "data": {
            "capture_platform": "DigiformsApp",
            "active_results_channel": DIGIFORMS_EXCEL_SOURCE,
            "active_results_channel_label": "Exportación Excel de DigiForms",
            "automatic_forms_results_api_enabled": False,
            "automatic_forms_results_api_status": "not_configured",
            "automatic_forms_results_api_message": "No se configuró una Forms/Results API porque no existe un endpoint oficial disponible dentro de la integración entregada. La carga Excel es el canal funcional habilitado.",
            "user_api_scope": "La integración externa configurada en Dataris administra usuarios de DigiFormsApp. Las respuestas SIG se incorporan mediante exportaciones .xlsx o .csv.",
            "latest_import": latest,
        },
        "error": None,
    }


@router.get("/imports")
def list_imports(form_type: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        rows = [row for row in table(db, "sig_import_runs") if str(row.get("user_id") or "") == user_id]
        if form_type:
            rows = [row for row in rows if row.get("form_type") == form_type]
        rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:100]
    return {"data": rows, "error": None, "count": len(rows)}


@router.post("/imports/harvest")
async def import_harvest(
    file: UploadFile = File(...),
    parcels_json: str = Form("[]"),
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    content = await _read_upload(file)
    parcels = _parse_parcels(parcels_json)
    with LOCK:
        db = read_db()
        run = _import_records(
            db=db,
            user_id=str(user.get("id") or ""),
            form_type=HARVEST_FORM_TYPE,
            filename=file.filename or "digiforms-cosecha.xlsx",
            content=content,
            parcels=parcels,
        )
        write_db(db)
    return {"data": run, "error": None, "message": "Exportación de cosecha importada y persistida correctamente."}


@router.post("/imports/pest-weed")
async def import_pest_weed(
    file: UploadFile = File(...),
    parcels_json: str = Form("[]"),
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(authorization)
    content = await _read_upload(file)
    parcels = _parse_parcels(parcels_json)
    with LOCK:
        db = read_db()
        run = _import_records(
            db=db,
            user_id=str(user.get("id") or ""),
            form_type=PEST_WEED_FORM_TYPE,
            filename=file.filename or "digiforms-malezas-plagas.xlsx",
            content=content,
            parcels=parcels,
        )
        write_db(db)
    return {"data": run, "error": None, "message": "Exportación de malezas y plagas importada y persistida correctamente."}


@router.get("/harvest-records")
def list_harvest_records(authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        rows = [
            _safe_record(row)
            for row in table(db, "sig_harvest_records")
            if str(row.get("user_id") or "") == user_id
        ]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
    return {"data": rows, "error": None, "count": len(rows)}


@router.get("/pest-weed-records")
def list_pest_weed_records(authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        rows = [
            _safe_record(row)
            for row in table(db, "sig_pest_weed_records")
            if str(row.get("user_id") or "") == user_id
        ]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
    return {"data": rows, "error": None, "count": len(rows)}


@router.get("/harvest-overrides")
def list_harvest_overrides(authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        rows = [dict(row) for row in table(db, "sig_harvest_overrides") if str(row.get("user_id") or "") == user_id]
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return {"data": rows, "error": None, "count": len(rows)}


@router.put("/harvest-overrides")
def upsert_harvest_override(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    feature_key = _safe_text(payload.get("feature_key"))
    parcel_id = _safe_text(payload.get("parcel_id"))
    if not feature_key or not parcel_id:
        raise HTTPException(status_code=400, detail="feature_key y parcel_id son obligatorios.")
    timestamp = now()
    with LOCK:
        db = read_db()
        rows = table(db, "sig_harvest_overrides")
        existing = next(
            (row for row in rows if str(row.get("user_id") or "") == user_id and row.get("feature_key") == feature_key),
            None,
        )
        values = {
            "user_id": user_id,
            "feature_key": feature_key,
            "parcel_id": parcel_id,
            "estado": _canonical_harvest_state(payload.get("estado")),
            "metodo": _safe_text(payload.get("metodo")),
            "fecha": _safe_text(payload.get("fecha")),
            "terminal": _safe_text(payload.get("terminal")),
            "zona": _safe_text(payload.get("zona")),
            "updated_at": timestamp,
        }
        if existing:
            existing.update(values)
            row = existing
        else:
            row = {"id": str(uuid.uuid4()), "created_at": timestamp, **values}
            rows.append(row)
        write_db(db)
    return {"data": row, "error": None, "message": "Corrección manual guardada."}


@router.delete("/harvest-overrides/{feature_key:path}")
def delete_harvest_override(feature_key: str, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    user_id = str(user.get("id") or "")
    with LOCK:
        db = read_db()
        rows = table(db, "sig_harvest_overrides")
        before = len(rows)
        db["tables"]["sig_harvest_overrides"] = [
            row
            for row in rows
            if not (str(row.get("user_id") or "") == user_id and row.get("feature_key") == feature_key)
        ]
        deleted = before != len(db["tables"]["sig_harvest_overrides"])
        write_db(db)
    if not deleted:
        raise HTTPException(status_code=404, detail="No se encontró la corrección manual.")
    return {"data": {"ok": True}, "error": None}
