
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import os
import secrets
import shutil
import smtplib
import string
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel
import httpx

try:
    import psycopg2
except Exception:  # pragma: no cover - optional fallback for local/dev environments
    psycopg2 = None

from app.core.config import settings
from app.services.telemetry.helicopter_processor import process_helicopter_zip
from app.services.telemetry.aerial_copilot import process_aerial_copilot
from app.utils.geojson_normalizer import normalize_record_geometries
from app.services.commercial_demo_seed import ensure_commercial_demo, is_commercial_demo_user
from app.services.parcel_split_migration import split_multi_feature_parcels
from app.utils.azure_blob import azure_blob_storage_disabled
from app.utils.storage_compat import (
    delete_compat_objects,
    list_compat_objects,
    read_compat_object,
    upload_compat_object,
)

router = APIRouter(prefix="/compat", tags=["Frontend Compatibility"])

ROOT = Path(os.getenv("DATARIS_COMPAT_STORAGE_DIR", "app/storage")).resolve()
DB_FILE = ROOT / "compat_db.json"
FILES = ROOT / "compat_files"
LOCK = RLock()
ENSURING_STORAGE = False
STORAGE_READY = False
STATE_TABLE_READY = False
STATE_CACHE: Optional[Dict[str, Any]] = None
STATE_CACHE_LOADED_AT = 0.0
STATE_CACHE_VERSION = 0


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# This JSON compatibility state can be large. Reading it from PostgreSQL on every
# authenticated request made login and navigation unnecessarily expensive. The
# short TTL keeps different Cloud Run workers convergent while allowing repeated
# reads inside the same request and rapid section changes to reuse the same state.
STATE_CACHE_TTL_SECONDS = _float_env("DATARIS_COMPAT_CACHE_TTL_SECONDS", 1.0)
STATE_TABLE = os.getenv("DATARIS_COMPAT_STATE_TABLE", "dataris_compat_state")
STATE_KEY = os.getenv("DATARIS_COMPAT_STATE_KEY", "default")

TABLES = [
    "profiles", "user_roles", "admin_users", "companies", "platform_modules",
    "company_modules", "user_modules", "parcels", "satellite_images", "satellite_comparisons", "satellite_jobs",
    "field_notes", "parcel_crops", "aerial_analyses", "analysis_sessions",
    "analysis_data_points", "laborapp_registros", "laborapp_empleados_foto",
    "extension_requests", "digiforms_accounts", "digiforms_user_links", "digiforms_operation_logs",
    "digiforms_connections", "digiforms_form_mappings",
    "sig_import_runs", "sig_harvest_records", "sig_pest_weed_records", "sig_harvest_overrides", "sig_sync_cursors", "digiforms_raw_submissions",
]

USER_SCOPED_TABLES = {
    "parcels",
    "satellite_images",
    "satellite_comparisons",
    "satellite_jobs",
    "field_notes",
    "parcel_crops",
    "aerial_analyses",
    "analysis_sessions",
    "analysis_data_points",
    "laborapp_registros",
    "laborapp_empleados_foto",
    "extension_requests",
    "digiforms_accounts",
    "digiforms_user_links",
    "digiforms_operation_logs",
    "sig_import_runs",
    "sig_harvest_records",
    "sig_pest_weed_records",
    "sig_harvest_overrides",
    # Reportes de campo: las plantillas y los envíos se acotan por empresa
    # (ver scoped_table_rows), no por usuario, para que el equipo de una empresa
    # comparta formularios y vea los reportes de su propia empresa.
    "report_templates",
    "report_submissions",
    "sig_sync_cursors",
    "digiforms_raw_submissions",
}

PARCEL_CHILD_TABLES = {
    "satellite_images",
    "satellite_comparisons",
    "satellite_jobs",
    "field_notes",
    "parcel_crops",
    "analysis_sessions",
}

DEFAULT_MODULES = [
    ("dashboard", "Dashboard", "Panel principal", "LayoutDashboard"),
    ("satelite", "Monitoreo Satelital", "Análisis satelital", "Satellite"),
    ("mapeo", "Mapeo", "Mapeo y análisis geoespacial", "Map"),
    ("telemetria", "Telemetría", "Indicadores y métricas", "Activity"),
    ("ortofoto-analysis", "Análisis de ortofotos", "Procesamiento visual de ortomosaicos", "Image"),
    ("sig-agricola", "SIG Agrícola", "Análisis agrícola", "Sprout"),
    ("aplicaciones-aereas", "Aplicaciones Aéreas", "Control de aplicaciones", "Plane"),
    ("personal", "Personal de Campo", "Control biométrico y georreferenciado", "Users"),
    ("alertas", "Alertas inteligentes", "Detección proactiva de riesgos operativos", "Bell"),
]

# Módulos descontinuados (Analytics, Tareas). Se filtran de las tablas en
# normalize_db() para que desaparezcan también de cualquier ambiente que ya
# los tuviera sembrados, sin necesitar una migración de datos aparte.
RETIRED_MODULE_IDS = {"analytics", "tareas"}

EXTENSION_MODULES = [
    (
        "digiforms",
        "DigiformsApp",
        "Formularios digitales de campo, captura offline, GPS, fotos y reportes desde DigiformsApp.",
        "FileText",
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def password_hash(password: str) -> str:
    return hashlib.sha256(f"{settings.JWT_SECRET_KEY}:{password}".encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return password_hash(password) == hashed


def token_for(user_id: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "type": "compat_user",
            "exp": datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "aud": "authenticated",
        "role": "authenticated",
        "email": user.get("email"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "user_metadata": user.get("user_metadata") or {},
        "app_metadata": user.get("app_metadata") or {},
    }


def session_for(user: Dict[str, Any]) -> Dict[str, Any]:
    access_token = token_for(user["id"])
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "expires_at": int((datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
        "refresh_token": access_token,
        "user": public_user(user),
    }


def default_db() -> Dict[str, Any]:
    created = now()
    admin_id = str(uuid.uuid4())
    company_id = str(uuid.uuid4())
    modules = [
        {"id": mid, "name": name, "description": desc, "icon": icon, "is_active": True, "created_at": created, "updated_at": created}
        for mid, name, desc, icon in DEFAULT_MODULES
    ]
    return {
        "users": [
            {
                "id": admin_id,
                "email": "admin@dataris.local",
                "password_hash": password_hash("admin123456"),
                "is_active": True,
                "created_at": created,
                "updated_at": created,
                "user_metadata": {"first_name": "Admin", "last_name": "DATARIS"},
            }
        ],
        "tables": {
            "profiles": [
                {
                    "id": admin_id,
                    "user_id": admin_id,
                    "email": "admin@dataris.local",
                    "first_name": "Admin",
                    "last_name": "DATARIS",
                    "company_name": "DATARIS",
                    "company_logo_url": None,
                    "avatar_url": None,
                    "phone": None,
                    "location": None,
                    "hectareas": 0,
                    "max_users": 999,
                    "created_at": created,
                    "updated_at": created,
                }
            ],
            "user_roles": [{"id": str(uuid.uuid4()), "user_id": admin_id, "role": "admin", "created_at": created}],
            "companies": [
                {
                    "id": company_id,
                    "name": "DATARIS",
                    "email": "admin@dataris.local",
                    "phone": None,
                    "cif": None,
                    "max_hectares": 999999,
                    "used_hectares": 0,
                    "is_active": True,
                    "created_at": created,
                    "updated_at": created,
                }
            ],
            "admin_users": [
                {
                    "id": str(uuid.uuid4()),
                    "user_id": admin_id,
                    "company_id": company_id,
                    "admin_role": "superadmin",
                    "assigned_hectares": 999999,
                    "created_by": None,
                    "is_active": True,
                    "created_at": created,
                    "updated_at": created,
                }
            ],
            "platform_modules": modules,
            "company_modules": [
                {"id": str(uuid.uuid4()), "company_id": company_id, "module_id": m["id"], "is_enabled": True, "created_at": created, "updated_at": created}
                for m in modules
            ],
            "user_modules": [
                {"id": str(uuid.uuid4()), "user_id": admin_id, "admin_user_id": None, "module_id": m["id"], "is_active": True, "created_at": created, "updated_at": created}
                for m in modules
            ],
            "parcels": [],
            "satellite_images": [],
            "satellite_comparisons": [],
            "field_notes": [],
            "parcel_crops": [],
            "aerial_analyses": [],
            "analysis_sessions": [],
            "analysis_data_points": [],
            "laborapp_registros": [],
            "laborapp_empleados_foto": [],
        },
    }


def database_url_for_state() -> str:
    return str(os.getenv("DATARIS_COMPAT_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()


def postgres_dsn() -> str:
    url = database_url_for_state()
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + url.split("postgresql+psycopg2://", 1)[1]
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.split("postgresql+asyncpg://", 1)[1]
    return url


def use_postgres_state() -> bool:
    mode = str(os.getenv("DATARIS_COMPAT_PERSISTENCE", "auto")).strip().lower()
    if mode in {"file", "json", "local"}:
        return False
    if mode in {"postgres", "postgresql", "db", "database"}:
        return bool(psycopg2 and postgres_dsn().startswith("postgres"))

    dsn = postgres_dsn()
    if not psycopg2 or not dsn.startswith("postgres"):
        return False

    # Local development often points to localhost and should keep using the JSON
    # file unless explicitly requested with DATARIS_COMPAT_PERSISTENCE=postgres.
    lowered = dsn.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered:
        return False
    return True


def postgres_connection():
    if not psycopg2:
        raise RuntimeError("psycopg2 is not installed")
    return psycopg2.connect(postgres_dsn())


def _cache_state(db: Dict[str, Any], *, changed: bool = False) -> Dict[str, Any]:
    global STATE_CACHE, STATE_CACHE_LOADED_AT, STATE_CACHE_VERSION
    STATE_CACHE = db
    STATE_CACHE_LOADED_AT = monotonic()
    if changed:
        STATE_CACHE_VERSION += 1
    return db


def get_state_cache_version() -> int:
    """Return a process-local revision incremented after successful writes."""
    return STATE_CACHE_VERSION


def invalidate_state_cache() -> None:
    global STATE_CACHE, STATE_CACHE_LOADED_AT
    with LOCK:
        STATE_CACHE = None
        STATE_CACHE_LOADED_AT = 0.0


def _state_cache_is_fresh() -> bool:
    if STATE_CACHE is None:
        return False
    if STATE_CACHE_TTL_SECONDS <= 0:
        return False
    return monotonic() - STATE_CACHE_LOADED_AT < STATE_CACHE_TTL_SECONDS


def ensure_state_table() -> None:
    global STATE_TABLE_READY
    if not use_postgres_state() or STATE_TABLE_READY:
        return
    with LOCK:
        if STATE_TABLE_READY:
            return
        ddl = f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                id TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        STATE_TABLE_READY = True


def read_db_from_file() -> Dict[str, Any]:
    with DB_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_db_to_file(db: Dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / f"{DB_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(DB_FILE)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_db_from_postgres() -> Optional[Dict[str, Any]]:
    ensure_state_table()
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT payload FROM {STATE_TABLE} WHERE id = %s", (STATE_KEY,))
            row = cur.fetchone()
            if not row:
                return None
            payload = row[0]
            if isinstance(payload, str):
                return json.loads(payload)
            return payload


def write_db_to_postgres(db: Dict[str, Any]) -> None:
    ensure_state_table()
    payload = json.dumps(db, ensure_ascii=False, default=str)
    sql = f"""
        INSERT INTO {STATE_TABLE} (id, payload, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (id)
        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
    """
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (STATE_KEY, payload))


def _persist_db(db: Dict[str, Any]) -> None:
    if use_postgres_state():
        write_db_to_postgres(db)
    else:
        _write_db_to_file(db)


def read_db(*, force_refresh: bool = False) -> Dict[str, Any]:
    ensure_storage()
    with LOCK:
        if not force_refresh and _state_cache_is_fresh() and STATE_CACHE is not None:
            return STATE_CACHE

        if use_postgres_state():
            data = read_db_from_postgres()
            if data is None:
                data = default_db()
                _persist_db(data)
        else:
            try:
                data = read_db_from_file()
            except Exception:
                data = default_db()
                _persist_db(data)

        normalized = normalize_db(data)
        return _cache_state(normalized)


def write_db(db: Dict[str, Any]) -> None:
    """Persist state and immediately refresh the process-local read cache.

    Cloud Run instances still converge through PostgreSQL because the read cache
    has a short TTL. Writes performed by the current worker become visible to the
    next request immediately without repeating a database read.
    """
    ensure_storage()
    with LOCK:
        normalized = normalize_db(db)
        _persist_db(normalized)
        _cache_state(normalized, changed=True)


def ensure_storage() -> None:
    global ENSURING_STORAGE, STORAGE_READY
    if STORAGE_READY or ENSURING_STORAGE:
        return

    with LOCK:
        if STORAGE_READY or ENSURING_STORAGE:
            return
        ENSURING_STORAGE = True
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            FILES.mkdir(parents=True, exist_ok=True)

            if use_postgres_state():
                ensure_state_table()
                db = read_db_from_postgres()
                if db is None:
                    if DB_FILE.exists():
                        try:
                            db = read_db_from_file()
                        except Exception:
                            db = default_db()
                    else:
                        db = default_db()
                    normalized = normalize_db(db)
                    _persist_db(normalized)
                else:
                    before = json.dumps(db, sort_keys=True, default=str)
                    normalized = normalize_db(db)
                    after = json.dumps(normalized, sort_keys=True, default=str)
                    if after != before:
                        _persist_db(normalized)
            else:
                if not DB_FILE.exists():
                    normalized = normalize_db(default_db())
                    _persist_db(normalized)
                else:
                    try:
                        db = read_db_from_file()
                    except Exception:
                        db = default_db()
                    before = json.dumps(db, sort_keys=True, default=str)
                    normalized = normalize_db(db)
                    after = json.dumps(normalized, sort_keys=True, default=str)
                    if after != before or not DB_FILE.exists():
                        _persist_db(normalized)

            _cache_state(normalized)
            STORAGE_READY = True
        finally:
            ENSURING_STORAGE = False

def normalize_db(db: Dict[str, Any]) -> Dict[str, Any]:
    db.setdefault("users", [])
    tables = db.setdefault("tables", {})
    for table_name in TABLES:
        tables.setdefault(table_name, [])
    t = now()
    if not tables.get("platform_modules"):
        tables["platform_modules"] = [
            {"id": mid, "name": name, "description": desc, "icon": icon, "is_active": True, "created_at": t, "updated_at": t}
            for mid, name, desc, icon in DEFAULT_MODULES
        ]
    else:
        existing_modules = {m.get("id") for m in tables.get("platform_modules", [])}
        for mid, name, desc, icon in DEFAULT_MODULES:
            if mid not in existing_modules:
                tables["platform_modules"].append({
                    "id": mid,
                    "name": name,
                    "description": desc,
                    "icon": icon,
                    "is_active": True,
                    "created_at": t,
                    "updated_at": t,
                })

    existing_modules = {m.get("id") for m in tables.get("platform_modules", [])}
    for mid, name, desc, icon in EXTENSION_MODULES:
        if mid not in existing_modules:
            tables.setdefault("platform_modules", []).append({
                "id": mid,
                "name": name,
                "description": desc,
                "icon": icon,
                "is_active": True,
                "created_at": t,
                "updated_at": t,
            })

    if RETIRED_MODULE_IDS:
        tables["platform_modules"] = [
            m for m in tables.get("platform_modules", []) if m.get("id") not in RETIRED_MODULE_IDS
        ]
        tables["company_modules"] = [
            m for m in tables.get("company_modules", []) if m.get("module_id") not in RETIRED_MODULE_IDS
        ]
        tables["user_modules"] = [
            m for m in tables.get("user_modules", []) if m.get("module_id") not in RETIRED_MODULE_IDS
        ]

    ensure_commercial_demo(db, password_hash=password_hash, reset=False)
    # Lotes subidos antes del split por parcela (PR #89) guardan todas las
    # parcelas en una sola fila; se dividen aquí para que la vista satelital
    # seleccione/compare parcela por parcela. ensure_storage() persiste el
    # resultado al detectar el cambio; después es un no-op.
    split_multi_feature_parcels(db, timestamp=t)
    return db

def table(db: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return db.setdefault("tables", {}).setdefault(name, [])


def bearer_user(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="SESSION_EXPIRED")
    except JWTError:
        return None
    db = read_db()
    user = next((u for u in db.get("users", []) if u.get("id") == payload.get("sub")), None)
    if user and user.get("is_active", True) is False:
        return None
    return user


def add_defaults(table_name: str, row: Dict[str, Any], user_id: Optional[str]) -> Dict[str, Any]:
    row = dict(row or {})
    t = now()
    row.setdefault("id", str(uuid.uuid4()))
    row.setdefault("created_at", t)
    row["updated_at"] = row.get("updated_at") or t
    if user_id and table_name in {"profiles", "user_roles", "parcels", "satellite_images", "satellite_comparisons", "field_notes", "parcel_crops", "aerial_analyses", "analysis_sessions", "analysis_data_points", "laborapp_registros"}:
        row.setdefault("user_id", user_id)
    if table_name == "profiles":
        row.setdefault("user_id", row.get("id") or user_id)
    if table_name == "companies":
        row.setdefault("used_hectares", 0)
        row.setdefault("is_active", True)
    if table_name == "platform_modules":
        row.setdefault("is_active", True)
    if table_name == "company_modules":
        row.setdefault("is_enabled", True)
    if table_name == "user_modules":
        row.setdefault("is_enabled", True)
        row.setdefault("is_active", True)
    if table_name == "admin_users":
        row.setdefault("admin_role", "company_admin")
        row.setdefault("is_active", True)
    row = normalize_record_geometries(table_name, row)
    return row


def normalize_lot_key(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return " ".join(text.split())
    return ""


def parcel_lot_key(row: Dict[str, Any]) -> str:
    return normalize_lot_key(row.get("lote"), row.get("codigo"), row.get("name"))


def user_parcel_ids(db: Dict[str, Any], user_id: str) -> set[str]:
    return {str(row.get("id")) for row in table(db, "parcels") if str(row.get("user_id") or "") == user_id and row.get("id")}


def _company_for_user(db: Dict[str, Any], user_id: str) -> Optional[str]:
    """Empresa a la que pertenece un usuario, sin importar `compat_extensions`.

    Se resuelve por su fila de admin, por el `company_id` del perfil o, en su
    defecto, casando el `company_name` del perfil contra el catálogo de
    empresas. Es una copia mínima de `company_for_user` para no crear un import
    circular entre este módulo y `compat_extensions`.
    """
    admin = next(
        (a for a in table(db, "admin_users") if a.get("user_id") == user_id and a.get("is_active", True)),
        None,
    )
    if admin and admin.get("company_id"):
        return admin.get("company_id")
    profile = next((p for p in table(db, "profiles") if p.get("user_id") == user_id), None)
    if profile and profile.get("company_id"):
        return profile.get("company_id")
    if profile and profile.get("company_name"):
        company = next((c for c in table(db, "companies") if c.get("name") == profile.get("company_name")), None)
        if company:
            return company.get("id")
    return None


def scoped_table_rows(db: Dict[str, Any], table_name: str, user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = table(db, table_name)
    if not user or table_name not in USER_SCOPED_TABLES:
        return rows

    user_id = str(user.get("id") or "")
    if not user_id:
        return []

    if table_name == "admin_users":
        return [row for row in rows if str(row.get("user_id") or "") == user_id]
    if table_name in PARCEL_CHILD_TABLES:
        allowed_parcels = user_parcel_ids(db, user_id)
        return [
            row
            for row in rows
            if str(row.get("user_id") or "") == user_id
            or (row.get("parcel_id") and str(row.get("parcel_id")) in allowed_parcels)
        ]
    if table_name == "extension_requests":
        return [row for row in rows if str(row.get("requested_by_user_id") or row.get("user_id") or "") == user_id]
    if table_name == "report_templates":
        # Plantillas del sistema (sin company_id) visibles para todos; las de una
        # empresa, solo para esa empresa.
        cid = str(_company_for_user(db, user_id) or "")
        return [row for row in rows if not row.get("company_id") or str(row.get("company_id")) == cid]
    if table_name == "report_submissions":
        # Los envíos se comparten dentro de la empresa; además cada quien ve los
        # suyos aunque su empresa no esté resuelta.
        cid = str(_company_for_user(db, user_id) or "")
        return [
            row
            for row in rows
            if (cid and str(row.get("company_id") or "") == cid) or str(row.get("user_id") or "") == user_id
        ]
    return [row for row in rows if str(row.get("user_id") or "") == user_id]


def dedupe_user_parcels(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = f"{row.get('user_id') or ''}:{parcel_lot_key(row) or row.get('id') or id(row)}"
        current = by_key.get(key)
        if current is None or str(row.get("updated_at") or row.get("created_at") or "") >= str(current.get("updated_at") or current.get("created_at") or ""):
            by_key[key] = row
    return list(by_key.values())


def find_existing_user_parcel(rows: List[Dict[str, Any]], row: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    row_key = parcel_lot_key(row)
    for existing in rows:
        if str(existing.get("user_id") or "") != user_id:
            continue
        if row.get("id") and str(existing.get("id")) == str(row.get("id")):
            return existing
        if row_key and parcel_lot_key(existing) == row_key:
            return existing
    return None


def upsert_user_parcel(db: Dict[str, Any], user_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a parcel without deleting the user's other lots.

    If a lot with the same normalized name/code already exists, update it in-place
    so child records keep referencing the same parcel id.
    """
    parcel_rows = table(db, "parcels")
    existing = find_existing_user_parcel(parcel_rows, row, user_id)
    if existing is not None:
        preserved_id = existing.get("id") or row.get("id")
        existing.clear()
        existing.update(row)
        existing["id"] = preserved_id
        existing["user_id"] = user_id
        return existing
    parcel_rows.append(row)
    return row


def cmp_value(value: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return value == expected
    if op == "neq":
        return value != expected
    if op == "in":
        return value in (expected or [])
    if op in {"gte", "lte", "gt", "lt"}:
        if value is None:
            return False
        try:
            a, b = (str(value), str(expected)) if isinstance(value, str) or isinstance(expected, str) else (value, expected)
            return {"gte": a >= b, "lte": a <= b, "gt": a > b, "lt": a < b}[op]
        except Exception:
            return False
    if op == "is":
        return value is expected
    if op == "like":
        return str(expected).replace("%", "") in str(value or "")
    if op == "ilike":
        return str(expected).replace("%", "").lower() in str(value or "").lower()
    return True


def apply_filters(rows: List[Dict[str, Any]], filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = rows
    for f in filters or []:
        result = [r for r in result if cmp_value(r.get(f.get("column")), f.get("op"), f.get("value"))]
    return result


def enrich(db: Dict[str, Any], table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row)
    tables = db.get("tables", {})
    if table_name == "company_modules":
        r["platform_modules"] = next((m for m in tables.get("platform_modules", []) if m.get("id") == r.get("module_id")), None)
    if table_name == "field_notes" and r.get("parcel_id"):
        parcel = next((p for p in tables.get("parcels", []) if p.get("id") == r.get("parcel_id")), None)
        if parcel:
            r["parcels"] = {"name": parcel.get("name")}
    if table_name == "admin_users" and r.get("user_id"):
        user = next((u for u in db.get("users", []) if u.get("id") == r.get("user_id")), None)
        profile = next((p for p in tables.get("profiles", []) if p.get("user_id") == r.get("user_id") or p.get("id") == r.get("user_id")), None)
        company = next((c for c in tables.get("companies", []) if c.get("id") == r.get("company_id")), None)
        if user:
            r.setdefault("email", user.get("email"))
        if profile:
            r.setdefault("first_name", profile.get("first_name"))
            r.setdefault("last_name", profile.get("last_name"))
        if company:
            r.setdefault("companies", {
                "name": company.get("name"),
                "max_hectares": company.get("max_hectares"),
                "used_hectares": company.get("used_hectares"),
            })
    if table_name == "extension_requests":
        module = next((m for m in tables.get("platform_modules", []) if m.get("id") == r.get("extension_id")), None)
        company = next((c for c in tables.get("companies", []) if c.get("id") == r.get("company_id")), None)
        requester = next((u for u in db.get("users", []) if u.get("id") == r.get("requested_by_user_id")), None)
        profile = next((p for p in tables.get("profiles", []) if p.get("user_id") == r.get("requested_by_user_id") or p.get("id") == r.get("requested_by_user_id")), None)
        r.setdefault("extension_name", (module or {}).get("name") or r.get("extension_id"))
        r.setdefault("company_name", (company or {}).get("name") or r.get("company_name_snapshot"))
        if requester:
            r.setdefault("requester_email", requester.get("email"))
        if profile:
            name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip()
            if name:
                r.setdefault("requester_name", name)
    return r


def pick(row: Dict[str, Any], select: Optional[str]) -> Dict[str, Any]:
    if not select or select.strip() in {"", "*"} or "(" in select:
        return row
    cols = [c.strip() for c in select.split(",") if c.strip()]
    if not cols or "*" in cols:
        return row
    return {c: row.get(c) for c in cols if c in row}


def clean_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email requerido o inválido")
    return email


def clean_password(value: Any) -> str:
    password = str(value or "")
    if not password:
        raise HTTPException(status_code=400, detail="Contraseña requerida")
    return password


def nested_value(payload: Dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    credentials = payload.get("credentials") or {}
    if isinstance(credentials, dict) and key in credentials:
        return credentials.get(key)
    return None


class SignUp(BaseModel):
    # No usamos EmailStr aquí porque email-validator rechaza dominios locales
    # como admin@dataris.local y FastAPI responde 422 antes de entrar al endpoint.
    email: Optional[str] = None
    password: Optional[str] = None
    options: Optional[Dict[str, Any]] = None


class SignIn(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None


@router.post("/auth/sign-up")
def sign_up(payload: Dict[str, Any] = Body(default_factory=dict)):
    with LOCK:
        db = read_db()
        email = clean_email(nested_value(payload, "email"))
        password = clean_password(nested_value(payload, "password"))
        options = payload.get("options") or {}
        metadata = (options.get("data") or {}) if isinstance(options, dict) else {}
        if any(u.get("email", "").lower() == email for u in db["users"]):
            raise HTTPException(status_code=400, detail="User already registered")
        t = now()
        user_id = str(uuid.uuid4())
        user = {"id": user_id, "email": email, "password_hash": password_hash(password), "is_active": True, "created_at": t, "updated_at": t, "user_metadata": metadata}
        db["users"].append(user)
        table(db, "profiles").append({
            "id": user_id, "user_id": user_id, "email": email,
            "first_name": metadata.get("first_name"), "last_name": metadata.get("last_name"),
            "company_name": metadata.get("company_name"), "company_logo_url": metadata.get("company_logo_url"),
            "avatar_url": metadata.get("avatar_url"), "phone": metadata.get("phone"), "location": metadata.get("location"),
            "hectareas": metadata.get("hectareas"), "max_users": metadata.get("max_users") or 0,
            "created_at": t, "updated_at": t,
        })
        table(db, "user_roles").append({"id": str(uuid.uuid4()), "user_id": user_id, "role": metadata.get("role", "admin"), "created_at": t})
        write_db(db)
        return {"data": {"user": public_user(user), "session": None}, "error": None}


@router.post("/auth/sign-in")
def sign_in(payload: Dict[str, Any] = Body(default_factory=dict)):
    email = clean_email(nested_value(payload, "email"))
    password = clean_password(nested_value(payload, "password"))
    db = read_db()
    user = next((u for u in db["users"] if u.get("email", "").lower() == email), None)
    if not user or not verify_password(password, user.get("password_hash", "")) or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="Invalid login credentials")
    # Every commercial-demo login starts from the same curated, isolated state.
    # The seeder follows only the demo tenant graph, including interactive rows,
    # and leaves records belonging to real users and companies untouched.
    if is_commercial_demo_user(user):
        with LOCK:
            db = read_db()
            ensure_commercial_demo(db, password_hash=password_hash, reset=True)
            write_db(db)
            user = next((item for item in db["users"] if item.get("id") == user.get("id")), user)
    session = session_for(user)
    return {"data": {"user": session["user"], "session": session}, "error": None}


@router.get("/auth/me")
def auth_me(authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"data": {"user": public_user(user), "session": session_for(user)}, "error": None}


@router.post("/auth/update-user")
def update_user(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with LOCK:
        db = read_db()
        db_user = next((u for u in db["users"] if u.get("id") == user["id"]), None)
        if payload.get("password"):
            db_user["password_hash"] = password_hash(str(payload["password"]))
        if payload.get("email"):
            db_user["email"] = str(payload["email"]).lower().strip()
        if payload.get("data"):
            db_user.setdefault("user_metadata", {}).update(payload["data"])
        db_user["updated_at"] = now()
        write_db(db)
        return {"data": {"user": public_user(db_user)}, "error": None}


@router.post("/auth/reset-password")
def reset_password(payload: Dict[str, Any] = Body(default_factory=dict)):
    return {"data": {"ok": True, "message": "Password reset accepted by compatibility backend."}, "error": None}


@router.delete("/auth/admin/users/{user_id}")
def delete_auth_user(user_id: str):
    with LOCK:
        db = read_db()
        db["users"] = [u for u in db["users"] if u.get("id") != user_id]
        for name, rows in db["tables"].items():
            db["tables"][name] = [r for r in rows if r.get("user_id") != user_id and r.get("id") != user_id]
        write_db(db)
    return {"data": {"ok": True}, "error": None}


def require_admin_context(authorization: Optional[str], db: Dict[str, Any]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    admin_row = next(
        (
            row
            for row in table(db, "admin_users")
            if row.get("user_id") == user.get("id") and row.get("is_active", True)
        ),
        None,
    )
    if not admin_row or admin_row.get("admin_role") not in {"superadmin", "company_admin"}:
        raise HTTPException(status_code=403, detail="No autorizado para administrar usuarios")
    return {"user": user, "admin": admin_row}


def generate_temporary_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    # Evita caracteres ambiguos para que el usuario pueda copiarla fácilmente.
    alphabet = alphabet.replace("O", "").replace("0", "").replace("l", "").replace("I", "")
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in password) and any(c.isupper() for c in password) and any(c.isdigit() for c in password):
            return password


def send_temporary_password_email(email: str, password: str, first_name: Optional[str] = None) -> Dict[str, Any]:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM_EMAIL", username or "").strip()
    sender_name = os.getenv("SMTP_FROM_NAME", "Dataris").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() not in {"0", "false", "no"}
    frontend_url = os.getenv("FRONTEND_URL", "https://app.dataris.es").rstrip("/")

    if not host or not sender:
        return {"sent": False, "reason": "SMTP no configurado"}

    display_name = first_name or "usuario"
    message = EmailMessage()
    message["Subject"] = "Acceso temporal a Dataris"
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = email
    message.set_content(
        f"Hola {display_name},\n\n"
        "Se creó tu acceso a Dataris.\n\n"
        f"Correo: {email}\n"
        f"Contraseña temporal: {password}\n\n"
        f"Ingresa en: {frontend_url}/login\n\n"
        "Por seguridad, cambia tu contraseña al ingresar por primera vez.\n\n"
        "Equipo Dataris"
    )

    try:
        with smtplib.SMTP(host, port, timeout=12) as smtp:
            if use_tls:
                smtp.starttls()
            if username and smtp_password:
                smtp.login(username, smtp_password)
            smtp.send_message(message)
        return {"sent": True, "reason": None}
    except Exception as exc:
        return {"sent": False, "reason": str(exc)}


@router.post("/admin/users/manual")
def create_manual_admin_user(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    with LOCK:
        db = read_db()
        ctx = require_admin_context(authorization, db)
        current_admin = ctx["admin"]
        is_super_admin = current_admin.get("admin_role") == "superadmin"

        email = clean_email(payload.get("email"))
        if any(str(u.get("email", "")).lower() == email for u in db.get("users", [])):
            raise HTTPException(status_code=400, detail="Ya existe un usuario con ese correo")

        manual_password = clean_password(payload.get("password"))
        if len(manual_password) < 8:
            raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")

        company_id = payload.get("company_id") or current_admin.get("company_id")
        if not is_super_admin and company_id != current_admin.get("company_id"):
            raise HTTPException(status_code=403, detail="No puedes crear usuarios para otra empresa")

        admin_role = str(payload.get("admin_role") or "company_user")
        if admin_role not in {"superadmin", "company_admin", "company_user"}:
            admin_role = "company_user"
        if not is_super_admin and admin_role in {"superadmin", "company_admin"}:
            admin_role = "company_user"

        assigned_hectares = float(payload.get("assigned_hectares") or 0)
        is_active = bool(payload.get("is_active", True))
        first_name = str(payload.get("first_name") or "").strip() or None
        last_name = str(payload.get("last_name") or "").strip() or None
        selected_modules = payload.get("modules") or []
        if not isinstance(selected_modules, list):
            selected_modules = []

        company = next((c for c in table(db, "companies") if c.get("id") == company_id), None) if company_id else None
        if company_id and not company:
            raise HTTPException(status_code=400, detail="Empresa no encontrada")
        if company and company.get("demo_seed"):
            raise HTTPException(
                status_code=400,
                detail="No se pueden crear usuarios reales en la empresa de demostración comercial",
            )

        if company:
            used = float(company.get("used_hectares") or 0)
            max_hectares = float(company.get("max_hectares") or 0)
            if assigned_hectares > max(0, max_hectares - used):
                raise HTTPException(status_code=400, detail="Las hectáreas asignadas superan el disponible de la empresa")

        if not is_super_admin and company_id:
            enabled_company_modules = {
                row.get("module_id")
                for row in table(db, "company_modules")
                if row.get("company_id") == company_id and row.get("is_enabled", True)
            }
            selected_modules = [module_id for module_id in selected_modules if module_id in enabled_company_modules]

        t = now()
        user_id = str(uuid.uuid4())
        user = {
            "id": user_id,
            "email": email,
            "password_hash": password_hash(manual_password),
            "is_active": is_active,
            "created_at": t,
            "updated_at": t,
            "user_metadata": {
                "first_name": first_name,
                "last_name": last_name,
                "manual_password": True,
            },
        }
        db.setdefault("users", []).append(user)

        table(db, "profiles").append({
            "id": user_id,
            "user_id": user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": company.get("name") if company else None,
            "company_logo_url": None,
            "avatar_url": None,
            "phone": None,
            "location": None,
            "hectareas": assigned_hectares,
            "max_users": 0,
            "created_at": t,
            "updated_at": t,
        })

        app_role = "admin" if admin_role in {"superadmin", "company_admin"} else "user"
        table(db, "user_roles").append({"id": str(uuid.uuid4()), "user_id": user_id, "role": app_role, "created_at": t})

        admin_user_id = str(uuid.uuid4())
        admin_row = {
            "id": admin_user_id,
            "user_id": user_id,
            "company_id": company_id,
            "admin_role": admin_role,
            "assigned_hectares": assigned_hectares,
            "created_by": current_admin.get("id"),
            "is_active": is_active,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "admin_users").append(admin_row)

        valid_modules = {m.get("id") for m in table(db, "platform_modules") if m.get("is_active", True)}
        for module_id in selected_modules:
            if module_id not in valid_modules:
                continue
            table(db, "user_modules").append({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "admin_user_id": admin_user_id,
                "module_id": module_id,
                "is_enabled": True,
                "is_active": True,
                "created_at": t,
                "updated_at": t,
            })

        if company:
            company["used_hectares"] = float(company.get("used_hectares") or 0) + assigned_hectares
            company["updated_at"] = t

        write_db(db)

    return {
        "data": {
            "user": public_user(user),
            "admin_user": enrich(db, "admin_users", admin_row),
            "manual_password": True,
            "email_sent": False,
            "email_message": None,
        },
        "error": None,
    }


@router.post("/reports/templates")
def save_report_template(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Crea o actualiza una plantilla de reporte. Solo para admins de empresa.

    Es la vía oficial de guardado del builder: a diferencia del insert genérico,
    exige `require_admin_context`, de modo que un usuario normal no puede alterar
    el formulario que usa el resto de su empresa. La plantilla queda anclada a la
    empresa del admin (salvo el superadmin, que puede fijar otra o dejarla como
    plantilla de sistema).
    """
    with LOCK:
        db = read_db()
        ctx = require_admin_context(authorization, db)
        admin = ctx["admin"]
        is_super = admin.get("admin_role") == "superadmin"

        key = str(payload.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="La plantilla necesita una clave (key)")

        # Un admin de empresa solo puede tocar plantillas de su empresa; el
        # superadmin puede fijar cualquier company_id o dejarlo nulo (sistema).
        company_id = payload.get("company_id") if is_super else admin.get("company_id")

        rows = table(db, "report_templates")
        template_id = payload.get("id")
        t = now()
        record = {
            "key": key,
            "name": str(payload.get("name") or key),
            "version": int(payload.get("version") or 1),
            "is_system": bool(payload.get("is_system", False)),
            "schema": payload.get("schema") or {},
            "catalogs": payload.get("catalogs") or {},
            "company_id": company_id,
            "updated_at": t,
        }

        existing = next((r for r in rows if template_id and r.get("id") == template_id), None)
        if existing:
            if not is_super and str(existing.get("company_id") or "") != str(admin.get("company_id") or ""):
                raise HTTPException(status_code=403, detail="No puedes editar plantillas de otra empresa")
            existing.update(record)
            saved = existing
        else:
            record["id"] = template_id or str(uuid.uuid4())
            record["created_at"] = t
            rows.append(record)
            saved = record

        write_db(db)
        return {"data": saved, "error": None}


@router.post("/tables/{table_name}/query")
def query(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    db = read_db()
    raw_rows = scoped_table_rows(db, table_name, user)
    if table_name == "parcels":
        raw_rows = dedupe_user_parcels(raw_rows)
    rows = [enrich(db, table_name, r) for r in raw_rows]
    rows = apply_filters(rows, payload.get("filters") or [])
    count = len(rows)
    for spec in reversed(payload.get("order") or []):
        rows.sort(key=lambda r: (r.get(spec.get("column")) is None, str(r.get(spec.get("column"), ""))), reverse=not spec.get("ascending", True))
    if payload.get("limit") is not None:
        rows = rows[: int(payload["limit"])]
    rows = [pick(r, payload.get("select")) for r in rows]
    if payload.get("single"):
        if not rows:
            if payload.get("maybe_single"):
                return {"data": None, "error": None, "count": count}
            raise HTTPException(status_code=406, detail="No rows found")
        return {"data": rows[0], "error": None, "count": count}
    return {"data": rows, "error": None, "count": count}


@router.post("/tables/{table_name}/insert")
def insert(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    incoming = payload.get("data", payload)
    items = incoming if isinstance(incoming, list) else [incoming]
    with LOCK:
        db = read_db()
        rows = table(db, table_name)
        inserted = []
        for item in items:
            row = add_defaults(table_name, item, user.get("id") if user else None)
            if table_name == "parcels" and user:
                row["user_id"] = user["id"]
                target = find_existing_user_parcel(rows, row, user["id"])
                if target:
                    target.update(row)
                    target["updated_at"] = now()
                    inserted.append(enrich(db, table_name, target))
                    continue
            rows.append(row)
            inserted.append(enrich(db, table_name, row))
        write_db(db)
    return {"data": inserted if isinstance(incoming, list) else inserted[0], "error": None, "count": len(inserted)}


@router.post("/tables/{table_name}/upsert")
def upsert(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    incoming = payload.get("data", payload)
    items = incoming if isinstance(incoming, list) else [incoming]
    on_conflict = payload.get("onConflict") or payload.get("on_conflict") or ""
    conflict_cols = [c.strip() for c in on_conflict.split(",") if c.strip()]
    if not conflict_cols:
        if table_name == "laborapp_empleados_foto":
            conflict_cols = ["empleado_codigo"]
        elif table_name == "laborapp_registros":
            conflict_cols = ["id_registro", "lote_codigo"]
    changed = []
    with LOCK:
        db = read_db()
        rows = table(db, table_name)
        for item in items:
            row = add_defaults(table_name, item, user.get("id") if user else None)
            if table_name == "parcels" and user:
                row["user_id"] = user["id"]
            target = None
            if conflict_cols:
                target = next(
                    (
                        r
                        for r in rows
                        if all(r.get(c) == row.get(c) for c in conflict_cols)
                        and (not user or table_name not in USER_SCOPED_TABLES or str(r.get("user_id") or "") == str(user.get("id") or ""))
                    ),
                    None,
                )
            if not target and row.get("id"):
                target = next(
                    (
                        r
                        for r in rows
                        if r.get("id") == row.get("id")
                        and (not user or table_name not in USER_SCOPED_TABLES or str(r.get("user_id") or "") == str(user.get("id") or ""))
                    ),
                    None,
                )
            if table_name == "parcels" and user and not target:
                target = find_existing_user_parcel(rows, row, user["id"])
            if target:
                target.update(row)
                target["updated_at"] = now()
                changed.append(enrich(db, table_name, target))
            else:
                rows.append(row)
                changed.append(enrich(db, table_name, row))
        write_db(db)
    return {"data": changed if isinstance(incoming, list) else changed[0], "error": None, "count": len(changed)}


@router.post("/tables/{table_name}/update")
def update(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    with LOCK:
        db = read_db()
        rows = scoped_table_rows(db, table_name, user)
        targets = apply_filters(rows, payload.get("filters") or [])
        for row in targets:
            row.update(normalize_record_geometries(table_name, payload.get("data") or {}))
            if table_name in USER_SCOPED_TABLES and user and "user_id" in row:
                row["user_id"] = user["id"]
            row["updated_at"] = now()
            row.update(normalize_record_geometries(table_name, row))
        result = [enrich(db, table_name, r) for r in targets]
        write_db(db)
    return {"data": result, "error": None, "count": len(result)}


@router.post("/tables/{table_name}/delete")
def delete(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    with LOCK:
        db = read_db()
        rows = table(db, table_name)
        scoped_rows = scoped_table_rows(db, table_name, user)
        targets = apply_filters(scoped_rows, payload.get("filters") or [])
        ids = {id(r) for r in targets}
        db["tables"][table_name] = [r for r in rows if id(r) not in ids]
        write_db(db)
    return {"data": targets, "error": None, "count": len(targets)}


@router.post("/rpc/{fn_name}")
def rpc(fn_name: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    db = read_db()
    if fn_name == "get_analysis_filter_options":
        user_id = payload.get("p_user_id") or payload.get("user_id")
        sessions = [s for s in table(db, "analysis_sessions") if not user_id or s.get("user_id") == user_id]
        uniq = lambda k: sorted({s.get(k) for s in sessions if s.get(k) not in (None, "")})
        return {"data": {"zafras": uniq("zafra"), "labores": uniq("labor"), "responsables": uniq("responsable"), "maquinarias": uniq("maquinaria")}, "error": None}
    if fn_name == "check_duplicate_analysis":
        sessions = table(db, "analysis_sessions")
        keys = ["user_id", "parcel_id", "zafra", "labor", "responsable", "maquinaria"]
        found = next((s for s in sessions if all(payload.get(k) is None or s.get(k) == payload.get(k) for k in keys)), None)
        return {"data": found, "error": None}
    return {"data": None, "error": None}


@router.post("/functions/{fn_name}")
def invoke(fn_name: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    body = payload.get("body") or payload
    action = body.get("action")
    if fn_name == "sentinel-hub":
        if action == "getAvailableDates":
            today = datetime.now(timezone.utc).date()
            return {"data": {"dates": [{"date": (today - timedelta(days=i * 12)).isoformat(), "cloudCoverage": min(6 + i * 8, 80)} for i in range(8)]}, "error": None}
        if action == "processImage":
            svg = "<svg xmlns='http://www.w3.org/2000/svg' width='1024' height='1024'><rect width='1024' height='1024' fill='#397a31'/><circle cx='512' cy='512' r='300' fill='#95bf47' opacity='.65'/><text x='512' y='510' text-anchor='middle' font-family='Arial' font-size='54' fill='white'>DATARIS</text><text x='512' y='575' text-anchor='middle' font-family='Arial' font-size='30' fill='white'>Imagen satelital simulada</text></svg>"
            return {"data": {"image": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode(), "statistics": {}, "cloudCoverage": 0}, "error": None}
    if fn_name == "process-parcel-images":
        return {"data": {"ok": True, "queued": True}, "error": None}
    return {"data": {"ok": True}, "error": None}


@router.post("/helicopter/analyze")
async def helicopter_analyze(
    file: UploadFile = File(...),
    swath_width: float = Form(16.0),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Debes subir un archivo .zip con Polygon, SprOn y SprOff")
    ensure_storage()
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    clean_name = Path(file.filename.replace("..", "_")).name
    tmp_path = tmp_dir / f"{uuid.uuid4()}-{clean_name}"
    try:
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
        result = await run_in_threadpool(process_helicopter_zip, tmp_path, float(swath_width or 16.0))
        return {"data": result, "error": None}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando vuelo de helicóptero: {exc}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/helicopter/copilot")
async def helicopter_copilot(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        result = await process_aerial_copilot(payload or {})
        return {"data": result, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error generando copiloto de aplicación aérea: {exc}")


@router.post("/parcels/upload")
async def upload_parcel_from_satellite(
    file: UploadFile = File(...),
    name: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not name.strip():
        raise HTTPException(status_code=400, detail="Nombre de parcela requerido")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Archivo requerido")

    ensure_storage()
    clean_original = Path(file.filename.replace("..", "_")).name
    storage_path = Path(user["id"]) / f"{int(datetime.now(timezone.utc).timestamp())}-{clean_original}"
    dest = FILES / "parcels" / storage_path
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        from app.services.telemetry.parcel_upload import parse_parcel_file

        lot_name = name.strip()
        parsed = parse_parcel_file(dest, clean_original, base_name=lot_name)
        parcels_data = parsed.get("parcels") or []
        if not parcels_data:
            raise ValueError("El archivo no contiene polígonos válidos")

        t = now()
        public_url = f"/api/compat/storage/public/parcels/{str(storage_path).replace(os.sep, '/') }"
        single = len(parcels_data) == 1
        created_rows: List[Dict[str, Any]] = []
        with LOCK:
            db = read_db()
            for item in parcels_data:
                row = normalize_record_geometries("parcels", {
                    "id": str(uuid.uuid4()),
                    "user_id": user["id"],
                    # Un solo polígono conserva el nombre que escribió el usuario;
                    # varios polígonos usan el nombre de cada parcela detectada en
                    # el shapefile/KML (o un correlativo si no trae atributos).
                    "name": lot_name if single else item.get("name") or lot_name,
                    "area": item.get("area"),
                    "geometry": item.get("geometry"),
                    "geometry_geojson": item.get("geometry_geojson"),
                    "geometry_bounds": item.get("geometry_bounds"),
                    "geometry_center": item.get("geometry_center"),
                    "bbox": item.get("bbox"),
                    "geometry_type": item.get("geometry_type"),
                    "geometry_feature_count": item.get("geometry_feature_count"),
                    "geometry_source_crs": item.get("geometry_source_crs"),
                    "finca": lot_name,
                    "file_url": public_url,
                    "created_at": t,
                    "updated_at": t,
                })
                created_rows.append(upsert_user_parcel(db, user["id"], row))
            write_db(db)
        return {"data": {"parcel": created_rows[0], "parcels": created_rows}, "error": None}
    except ValueError as exc:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raw_message = str(exc)
        if "TopologyException" in raw_message or "side location conflict" in raw_message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "El shapefile contiene una geometría inválida o auto-intersectada. "
                    "Intenta exportarlo nuevamente como ZIP con .shp, .shx, .dbf y .prj; "
                    "si el problema continúa, corrige/repara la geometría en QGIS antes de subirlo."
                ),
            )
        raise HTTPException(status_code=500, detail=f"Error subiendo parcela: {raw_message}")


_GOOGLE_MAPS_ALLOWED_HOSTS = {
    "maps.app.goo.gl",
    "goo.gl",
    "maps.google.com",
    "www.google.com",
    "google.com",
}
_GOOGLE_MAPS_COORD_PATTERNS = [
    re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"),
    re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)"),
    re.compile(r"[?&](?:q|query|ll|destination|center)=(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
]

def _is_allowed_google_maps_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return hostname in _GOOGLE_MAPS_ALLOWED_HOSTS or hostname.endswith(".google.com") or hostname.endswith(".goo.gl")

def _extract_google_maps_coordinates(value: str) -> Optional[Dict[str, float]]:
    decoded = unquote(str(value or ""))
    for pattern in _GOOGLE_MAPS_COORD_PATTERNS:
        match = pattern.search(decoded)
        if not match:
            continue
        lat = float(match.group(1))
        lng = float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return {"lat": lat, "lng": lng}
    try:
        query = parse_qs(urlparse(decoded).query)
    except Exception:
        query = {}
    for key in ("q", "query", "ll", "destination", "center"):
        raw = str((query.get(key) or [""])[0]).strip()
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", raw)
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return {"lat": lat, "lng": lng}
    return None

async def _resolve_google_maps_url(value: str) -> tuple[str, Optional[Dict[str, float]]]:
    current = str(value or "").strip()
    if not _is_allowed_google_maps_url(current):
        raise HTTPException(status_code=400, detail="Ingresa un enlace válido de Google Maps")
    direct = _extract_google_maps_coordinates(current)
    if direct:
        return current, direct

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False, headers={"User-Agent": "Dataris/1.0"}) as client:
        for _ in range(6):
            if not _is_allowed_google_maps_url(current):
                raise HTTPException(status_code=400, detail="El enlace redirige fuera de Google Maps")
            response = await client.get(current)
            location = response.headers.get("location")
            if location and response.status_code in {301, 302, 303, 307, 308}:
                current = urljoin(current, location)
                coords = _extract_google_maps_coordinates(current)
                if coords:
                    return current, coords
                continue
            coords = _extract_google_maps_coordinates(str(response.url)) or _extract_google_maps_coordinates(response.text)
            return str(response.url), coords
    return current, _extract_google_maps_coordinates(current)

@router.post("/maps/resolve")
async def resolve_google_maps_link(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Ingresa un enlace de Google Maps")
    resolved_url, coords = await _resolve_google_maps_url(url)
    if not coords:
        raise HTTPException(status_code=422, detail="No se encontraron coordenadas en el enlace. Abre la ubicación exacta en Google Maps y copia el enlace nuevamente.")
    return {"data": {"url": resolved_url, **coords}, "error": None}

@router.post("/parcels/create-manual")
def create_manual_parcel(
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    name = str(payload.get("name") or "").strip()
    geometry = payload.get("geometry")
    if not name:
        raise HTTPException(status_code=400, detail="Nombre de lote requerido")
    if not geometry:
        raise HTTPException(status_code=400, detail="Dibuja al menos tres puntos para crear el lote")

    t = now()
    row = normalize_record_geometries("parcels", {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": name,
        "geometry": geometry,
        "geometry_geojson": geometry,
        "source": "manual_map",
        "created_at": t,
        "updated_at": t,
    })
    if not row.get("geometry_geojson") or not row.get("area"):
        raise HTTPException(status_code=400, detail="El polígono dibujado no es válido. Revisa los puntos e intenta nuevamente.")
    with LOCK:
        db = read_db()
        row = upsert_user_parcel(db, user["id"], row)
        write_db(db)
    return {"data": {"parcel": row}, "error": None}


@router.post("/storage/{bucket}/upload")
async def storage_upload(bucket: str, path: str, file: UploadFile = File(...)):
    clean = Path(path.replace("..", "_").lstrip("/"))
    clean_str = str(clean).replace("\\", "/")

    if azure_blob_storage_disabled():
        ensure_storage()
        dest = FILES / bucket / clean
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    else:
        upload_compat_object(bucket, clean_str, file.file, file.content_type)

    return {"data": {"path": clean_str, "fullPath": f"{bucket}/{clean_str}"}, "error": None}


@router.get("/storage/{bucket}/list")
def storage_list(bucket: str, prefix: str = ""):
    clean_prefix = prefix.replace("..", "_").lstrip("/")

    if azure_blob_storage_disabled():
        base = (FILES / bucket / clean_prefix).resolve()
        root = (FILES / bucket).resolve()
        if not str(base).startswith(str(root)) or not base.exists():
            return {"data": [], "error": None}
        return {"data": [{"name": p.name, "id": p.name, "updated_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()} for p in base.iterdir() if p.is_file()], "error": None}

    items = list_compat_objects(bucket, clean_prefix)
    return {"data": [{"name": name, "id": name, "updated_at": updated_at.isoformat() if updated_at else None} for name, updated_at in items], "error": None}


@router.post("/storage/{bucket}/remove")
def storage_remove(bucket: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    paths = payload.get("paths") or []

    if azure_blob_storage_disabled():
        removed = []
        root = (FILES / bucket).resolve()
        for raw in paths:
            p = (FILES / bucket / str(raw).replace("..", "_").lstrip("/")).resolve()
            if str(p).startswith(str(root)) and p.exists():
                p.unlink()
                removed.append(str(raw))
        return {"data": removed, "error": None}

    clean_paths = [str(raw).replace("..", "_").lstrip("/") for raw in paths]
    removed = delete_compat_objects(bucket, clean_paths)
    return {"data": removed, "error": None}


@router.get("/storage/public/{bucket}/{file_path:path}")
def storage_public(bucket: str, file_path: str):
    clean = file_path.replace("..", "_").lstrip("/")

    if azure_blob_storage_disabled():
        path = (FILES / bucket / clean).resolve()
        root = (FILES / bucket).resolve()
        if not str(path).startswith(str(root)) or not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    try:
        content = read_compat_object(bucket, clean)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(clean)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@router.get("/health")
def health():
    ensure_storage()
    return {"status": "ok", "storage": str(ROOT)}


ensure_storage()
