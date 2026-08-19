
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
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, Header, HTTPException, UploadFile
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
from app.services import module_access, module_catalog
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
    # Catálogo de formularios que cada empresa tiene en AgtechApps: es
    # configuración de un cliente y no debe verse desde otro.
    "digiforms_forms",
    "digiforms_form_mappings",
}

PARCEL_CHILD_TABLES = {
    "satellite_images",
    "satellite_comparisons",
    "satellite_jobs",
    "field_notes",
    "parcel_crops",
    "analysis_sessions",
}

# El catálogo de módulos lo define el producto (app/services/module_catalog.py),
# no una fila que alguien pueda inventar desde el panel: cada módulo es una ruta
# y un guardián `requiredModuleId` en el frontend.
DEFAULT_MODULES = module_catalog.default_catalog_rows()

# Módulos descontinuados (Analytics, Tareas y Reportes de campo — José pidió
# eliminarlo el 17 ago 2026 porque no se usará). Se filtran de las tablas en
# normalize_db() para que desaparezcan también de cualquier ambiente que ya
# los tuviera sembrados, sin necesitar una migración de datos aparte.
RETIRED_MODULE_IDS = {"analytics", "tareas", "reportes"}

EXTENSION_MODULES = [
    (spec.id, spec.name, spec.description, spec.icon)
    for spec in module_catalog.extension_specs()
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
            # El superadmin ve la plataforma completa por su rol: no necesita
            # filas en `user_modules` (que ahora son overrides por usuario y
            # aparecerían en el panel como "ajustes propios" inexistentes).
            "user_modules": [],
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

    sync_module_catalog_metadata(tables.get("platform_modules", []), t)
    backfill_user_module_overrides(db, t)

    ensure_commercial_demo(db, password_hash=password_hash, reset=False)
    # Lotes subidos antes del split por parcela (PR #89) guardan todas las
    # parcelas en una sola fila; se dividen aquí para que la vista satelital
    # seleccione/compare parcela por parcela. ensure_storage() persiste el
    # resultado al detectar el cambio; después es un no-op.
    split_multi_feature_parcels(db, timestamp=t)
    return db

def sync_module_catalog_metadata(modules: List[Dict[str, Any]], timestamp: str) -> None:
    """Alinea nombre/descripción/icono con el catálogo del producto.

    Lo único que decide el operador es `is_active`; el resto de la ficha vive en
    el código, así que se refresca aquí para que el panel no muestre etiquetas de
    una versión anterior del producto.
    """
    for row in modules:
        spec = module_catalog.spec_for(row.get("id") or row.get("name"))
        if not spec:
            continue
        if (row.get("name"), row.get("description"), row.get("icon")) == (spec.name, spec.description, spec.icon):
            continue
        row["name"] = spec.name
        row["description"] = spec.description
        row["icon"] = spec.icon
        row["updated_at"] = timestamp


USER_MODULE_OVERRIDES_MIGRATION = "user_module_overrides_v1"


def backfill_user_module_overrides(db: Dict[str, Any], timestamp: str) -> Dict[str, Any]:
    """Convierte las filas de `user_modules` en overrides explícitos.

    Hasta ahora `user_modules` funcionaba como lista blanca: si un usuario tenía
    aunque fuera UNA fila, esa lista sustituía por completo a los módulos de su
    empresa. Eso producía dos fallos serios:

    - apagar todos los módulos de un usuario borraba todas sus filas y el cálculo
      caía al fallback de la empresa, devolviéndoselos todos; y
    - aprobar una extensión (DigiformsApp/Graniot) creaba la primera fila del
      usuario y, de golpe, le quitaba todo lo que heredaba de su empresa.

    El modelo nuevo es override por módulo: la empresa manda y la fila del
    usuario decide solo sobre SU módulo (`is_enabled` explícito). Para que nadie
    gane accesos con el despliegue, este backfill escribe el `false` explícito de
    los módulos core que el usuario no tenía. Se salta a los usuarios cuyas
    únicas filas eran de extensión: ahí nunca hubo una restricción deliberada
    (heredaban de la empresa hasta que la aprobación creó la fila), así que
    recuperan la herencia — que es justamente el fallo que se está corrigiendo.
    """
    migrations = db.setdefault("migrations", {})
    if migrations.get(USER_MODULE_OVERRIDES_MIGRATION):
        return {"applied": False, "rows_added": 0}

    rows = table(db, "user_modules")
    core_ids = [spec.id for spec in module_catalog.core_specs() if spec.assignable]

    company_enabled: Dict[str, set] = {}
    for row in table(db, "company_modules"):
        company_id = row.get("company_id")
        if not company_id:
            continue
        if not module_access.row_is_enabled(row):
            continue
        company_enabled.setdefault(str(company_id), set()).add(module_catalog.canonical_module_id(row.get("module_id")))

    admin_by_user = {
        str(row.get("user_id")): row
        for row in table(db, "admin_users")
        if row.get("user_id") and row.get("is_active", True) is not False
    }

    positives_by_user: Dict[str, set] = {}
    for row in rows:
        user_id = row.get("user_id") or (admin_by_user_id(db, row.get("admin_user_id")) or {}).get("user_id")
        if not user_id:
            continue
        if not module_access.row_is_enabled(row):
            continue
        positives_by_user.setdefault(str(user_id), set()).add(module_catalog.canonical_module_id(row.get("module_id")))

    existing_pairs = {
        (
            str(row.get("user_id") or (admin_by_user_id(db, row.get("admin_user_id")) or {}).get("user_id") or ""),
            module_catalog.canonical_module_id(row.get("module_id")),
        )
        for row in rows
    }

    added = 0
    for user_id, granted in positives_by_user.items():
        granted_core = {mid for mid in granted if mid in core_ids}
        if not granted_core:
            # Solo tenía extensiones: no había restricción que preservar.
            continue
        admin_row = admin_by_user.get(user_id) or {}
        company_id = str(admin_row.get("company_id") or profile_company_id(db, user_id) or "")
        inherited = company_enabled.get(company_id, set()) if company_id else set()
        for module_id in core_ids:
            if module_id in granted_core or module_id not in inherited:
                continue
            if (user_id, module_id) in existing_pairs:
                continue
            existing_pairs.add((user_id, module_id))
            rows.append({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "admin_user_id": admin_row.get("id"),
                "module_id": module_id,
                "is_enabled": False,
                "is_active": True,
                "source": USER_MODULE_OVERRIDES_MIGRATION,
                "created_at": timestamp,
                "updated_at": timestamp,
            })
            added += 1

    migrations[USER_MODULE_OVERRIDES_MIGRATION] = timestamp
    return {"applied": True, "rows_added": added}


def admin_by_user_id(db: Dict[str, Any], admin_user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not admin_user_id:
        return None
    return next((row for row in table(db, "admin_users") if row.get("id") == admin_user_id), None)


def profile_company_id(db: Dict[str, Any], user_id: str) -> Optional[str]:
    profile = next(
        (row for row in table(db, "profiles") if str(row.get("user_id") or row.get("id") or "") == str(user_id)),
        None,
    )
    return (profile or {}).get("company_id")


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
        # Menor privilegio por defecto: si una fila de administrador llega sin
        # rol, NUNCA se asume "company_admin". Antes ese default convertía en
        # administrador de empresa a cualquier fila insertada por la API genérica
        # (a la que guard_admin_users_write le había quitado el rol), permitiendo
        # que un no-superadmin fabricara otro admin.
        row.setdefault("admin_role", "company_user")
        row.setdefault("is_active", True)
    row = normalize_record_geometries(table_name, row)
    return row


# Países soportados por la plataforma (ISO 3166-1 alfa-2). El país del perfil
# decide con qué marca comercial ve el cliente la plataforma: México ve
# Innovagro y el resto ve Dataris. Se guarda normalizado para que el frontend
# no tenga que interpretar textos libres.
COUNTRY_CODES = {
    "MX", "GT", "SV", "HN", "NI", "CR", "PA", "CO", "EC", "PE",
    "BO", "CL", "AR", "UY", "PY", "BR", "DO", "CU", "ES", "US",
}

COUNTRY_NAME_TO_CODE = {
    "mexico": "MX",
    "guatemala": "GT",
    "el salvador": "SV",
    "honduras": "HN",
    "nicaragua": "NI",
    "costa rica": "CR",
    "panama": "PA",
    "colombia": "CO",
    "ecuador": "EC",
    "peru": "PE",
    "bolivia": "BO",
    "chile": "CL",
    "argentina": "AR",
    "uruguay": "UY",
    "paraguay": "PY",
    "brasil": "BR",
    "brazil": "BR",
    "republica dominicana": "DO",
    "cuba": "CU",
    "espana": "ES",
    "estados unidos": "US",
}


def normalize_country(value: Any) -> Optional[str]:
    """Devuelve el código ISO del país, aceptando el código o el nombre."""
    text = str(value or "").strip()
    if not text:
        return None
    upper = text.upper()
    if upper in COUNTRY_CODES:
        return upper
    folded = "".join(
        char for char in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(char) != "Mn"
    ).strip()
    return COUNTRY_NAME_TO_CODE.get(folded)


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


def _is_platform_superadmin(db: Dict[str, Any], user_id: str) -> bool:
    """Superadmin de la plataforma (personal de DATARIS), no de una empresa."""
    return any(
        row.get("user_id") == user_id
        and row.get("is_active", True) is not False
        and row.get("admin_role") == "superadmin"
        for row in table(db, "admin_users")
    )


# Los lotes ya no los carga el cliente desde su perfil: los da de alta el equipo
# de Dataris (desarrollo/comercial) desde el panel de administración. El permiso
# vive en la fila de `admin_users`, de modo que un comercial (admin_role
# "company_user") puede recibirlo sin convertirse en administrador de nada más.
PARCEL_MANAGER_FIELD = "can_manage_parcels"
PARCEL_MANAGER_ALL_FIELD = "can_manage_all_parcels"
# Permiso para dar de alta clientes nuevos (empresa + su administrador) sin ser
# superadministrador. Lo reciben los perfiles comerciales para poder hacer el
# onboarding de cuentas sin tener control sobre el resto de la plataforma.
CLIENT_ONBOARDER_FIELD = "can_onboard_clients"
# Módulos que se activan por defecto para un cliente recién dado de alta. Es un
# punto de partida razonable; el equipo puede ampliarlo o recortarlo después.
CLIENT_DEFAULT_MODULE_IDS = [
    "dashboard",
    "satelite",
    "mapeo",
    "telemetria",
    "sig-agricola",
    "ortofoto-analysis",
    "aplicaciones-aereas",
    "personal",
]

# Lista blanca del panel de administración (/admin). Solo estas cuentas pueden
# entrar al panel y ejecutar acciones administrativas; el resto de filas de
# `admin_users` conserva su efecto sobre el acceso a módulos de la app, pero ya
# no abre el panel ni sus endpoints. Se ajusta sin tocar código con la variable
# de entorno DATARIS_ADMIN_PANEL_EMAILS (emails separados por comas; admite
# "*" para desactivar la restricción y "*@dominio" como comodín de dominio,
# pensados para desarrollo y tests).
DEFAULT_ADMIN_PANEL_EMAILS = "admin@dataris.local,admin@dataris.es,gmateo@dataris.es"


def admin_panel_allowed_emails() -> set[str]:
    raw = os.getenv("DATARIS_ADMIN_PANEL_EMAILS") or DEFAULT_ADMIN_PANEL_EMAILS
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def panel_email_allowed(user: Optional[Dict[str, Any]]) -> bool:
    email = str((user or {}).get("email") or "").strip().lower()
    if not email:
        return False
    allowed = admin_panel_allowed_emails()
    if "*" in allowed or email in allowed:
        return True
    return any(entry.startswith("*") and email.endswith(entry[1:]) for entry in allowed)


def active_admin_row(db: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (
            row
            for row in table(db, "admin_users")
            if str(row.get("user_id") or "") == str(user_id or "") and row.get("is_active", True) is not False
        ),
        None,
    )


def parcel_manager_permission(db: Dict[str, Any], user_id: Optional[str]) -> Dict[str, Any]:
    """Alcance con el que un usuario puede administrar lotes ajenos.

    - `superadmin`: todos los usuarios de la plataforma.
    - `company_admin`: los usuarios de su empresa (o todos si se le marcó el
      permiso global).
    - cualquier otra fila de admin con `can_manage_parcels`: comerciales a los
      que el administrador dio el permiso.
    """
    result: Dict[str, Any] = {
        "allowed": False,
        "scope": None,
        "company_id": None,
        "admin_role": None,
        "admin_user_id": None,
    }
    if not user_id:
        return result
    admin = active_admin_row(db, str(user_id))
    if not admin:
        return result

    role = admin.get("admin_role")
    company_id = admin.get("company_id")
    global_scope = bool(admin.get(PARCEL_MANAGER_ALL_FIELD))
    result.update({"admin_role": role, "company_id": company_id, "admin_user_id": admin.get("id")})

    if role == "superadmin":
        result.update({"allowed": True, "scope": "all"})
        return result
    if role == "company_admin" or admin.get(PARCEL_MANAGER_FIELD):
        result.update({"allowed": True, "scope": "all" if global_scope else "company"})
        return result
    return result


def can_manage_parcels(db: Dict[str, Any], user_id: Optional[str]) -> bool:
    return bool(parcel_manager_permission(db, user_id).get("allowed"))


def can_onboard_clients(db: Dict[str, Any], user_id: Optional[str]) -> bool:
    """¿Puede este usuario dar de alta clientes nuevos (empresa + su admin)?

    Lo pueden hacer los superadministradores y los comerciales a los que se les
    marcó `can_onboard_clients`. Nunca convierte a nadie en superadmin: sólo crea
    administradores de la empresa recién creada.
    """
    if not user_id:
        return False
    admin = active_admin_row(db, str(user_id))
    if not admin:
        return False
    return admin.get("admin_role") == "superadmin" or bool(admin.get(CLIENT_ONBOARDER_FIELD))


def parcel_manager_covers_user(
    db: Dict[str, Any],
    permission: Dict[str, Any],
    target_user_id: str,
) -> bool:
    """¿El alcance del gestor incluye al usuario dueño de los lotes?"""
    if not permission.get("allowed"):
        return False
    if permission.get("scope") == "all":
        return True
    company_id = permission.get("company_id")
    if not company_id:
        return False
    return str(_company_for_user(db, str(target_user_id)) or "") == str(company_id)


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
        #
        # El superadmin de la plataforma es la excepción: administra los
        # formularios de cualquier cliente, igual que ya ve todos los módulos
        # (ver me_access.py). Sin esto, alguien de DATARIS abría Reportes de
        # Campo y encontraba el listado vacío porque las plantillas existentes
        # pertenecían a otras empresas. Los ENVÍOS no siguen esta regla: los
        # datos que el cliente llena en campo se quedan en su empresa.
        if _is_platform_superadmin(db, user_id):
            return rows
        cid = str(_company_for_user(db, user_id) or "")
        return [row for row in rows if not row.get("company_id") or str(row.get("company_id")) == cid]
    if table_name in {"digiforms_forms", "digiforms_form_mappings"}:
        # Configuración de la integración: se comparte dentro de la empresa y no
        # sale de ella. Sin empresa resuelta no se ve nada.
        cid = str(_company_for_user(db, user_id) or "")
        return [row for row in rows if cid and str(row.get("company_id") or "") == cid]
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


def _parcel_bbox(row: Dict[str, Any]) -> Optional[List[float]]:
    box = row.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        try:
            return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
        except (TypeError, ValueError):
            return None
    return None


def _parcel_geometry_shape(row: Dict[str, Any]):
    """Geometría del lote como polígono de shapely, o None si no se puede."""
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    geometry = row.get("geometry_geojson") or row.get("geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        if geometry.get("type") == "FeatureCollection":
            parts = [
                _shape(f["geometry"])
                for f in (geometry.get("features") or [])
                if isinstance(f, dict) and isinstance(f.get("geometry"), dict)
            ]
            if not parts:
                return None
            return unary_union(parts).buffer(0)
        if geometry.get("type") == "Feature" and isinstance(geometry.get("geometry"), dict):
            return _shape(geometry["geometry"]).buffer(0)
        if geometry.get("type") in {"Polygon", "MultiPolygon"}:
            return _shape(geometry).buffer(0)
    except Exception:
        return None
    return None


def _find_geometric_duplicate(
    rows: List[Dict[str, Any]], row: Dict[str, Any], user_id: str, iou_threshold: float = 0.95
) -> Optional[Dict[str, Any]]:
    """Lote del usuario con geometría casi idéntica al entrante (re-subida).

    Deduplica por GEOMETRÍA cuando el nombre no coincide (el mismo lote resubido
    con otro nombre, p. ej. `1190` y `1190.`), que era la vía por la que se
    acumulaban polígonos duplicados. El prefiltro por bbox usa el dato ya
    guardado, así que solo se construye la geometría de los pocos candidatos que
    se solapan: no penaliza a cuentas con miles de lotes.
    """
    incoming_box = _parcel_bbox(row)
    if incoming_box is None:
        return None
    incoming_shape = None
    for existing in rows:
        if str(existing.get("user_id") or "") != user_id:
            continue
        existing_box = _parcel_bbox(existing)
        if existing_box is None:
            continue
        if incoming_box[2] < existing_box[0] or existing_box[2] < incoming_box[0]:
            continue
        if incoming_box[3] < existing_box[1] or existing_box[3] < incoming_box[1]:
            continue
        if incoming_shape is None:
            incoming_shape = _parcel_geometry_shape(row)
            if incoming_shape is None or incoming_shape.is_empty or incoming_shape.area <= 0:
                return None
        existing_shape = _parcel_geometry_shape(existing)
        if existing_shape is None or existing_shape.is_empty or existing_shape.area <= 0:
            continue
        intersection = incoming_shape.intersection(existing_shape).area
        if intersection <= 0:
            continue
        union = incoming_shape.area + existing_shape.area - intersection
        if union and intersection / union >= iou_threshold:
            return existing
    return None


def find_existing_user_parcel(rows: List[Dict[str, Any]], row: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    row_key = parcel_lot_key(row)
    for existing in rows:
        if str(existing.get("user_id") or "") != user_id:
            continue
        if row.get("id") and str(existing.get("id")) == str(row.get("id")):
            return existing
        if row_key and parcel_lot_key(existing) == row_key:
            return existing
    # Sin coincidencia por nombre/id: buscar un duplicado por geometría (mismo
    # lote resubido con otro nombre). Al encontrarlo, upsert_user_parcel lo
    # actualiza en sitio y conserva su vínculo con Graniot, en vez de crear otra
    # parcela que dejaría la anterior huérfana en el portal.
    return _find_geometric_duplicate(rows, row, user_id)


def upsert_user_parcel(db: Dict[str, Any], user_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a parcel without deleting the user's other lots.

    If a lot with the same normalized name/code already exists, update it in-place
    so child records keep referencing the same parcel id.
    """
    parcel_rows = table(db, "parcels")
    existing = find_existing_user_parcel(parcel_rows, row, user_id)
    if existing is not None:
        preserved_id = existing.get("id") or row.get("id")
        # El lote sigue siendo el mismo, así que conserva su vínculo con Graniot:
        # sin esto, volver a subir el archivo perdería el id remoto y la
        # sincronización crearía una parcela duplicada en el portal del usuario,
        # dejando la anterior huérfana.
        preserved_graniot = {
            key: value
            for key, value in existing.items()
            if key.startswith("graniot_") and key not in row
        }
        existing.clear()
        existing.update(row)
        existing.update(preserved_graniot)
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
        # El rol NUNCA se toma del cuerpo de la petición: el alta pública crea
        # siempre un usuario normal. La condición de administrador se concede
        # aparte, por la vía protegida de administración (admin/users/manual o la
        # tabla admin_users con guardián). Antes el default era "admin" y bastaba
        # un registro anónimo para nacer con rol de administrador.
        table(db, "user_roles").append({"id": str(uuid.uuid4()), "user_id": user_id, "role": "user", "created_at": t})
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
def delete_auth_user(user_id: str, authorization: Optional[str] = Header(default=None)):
    # Borrar un usuario cierra su sesión y arrastra todas sus filas. Antes estaba
    # abierto sin autenticación: cualquiera podía eliminar a cualquier usuario
    # (una de las formas de "expulsar" a otro de su sesión). Sólo el superadmin.
    with LOCK:
        db = read_db()
        ctx = require_admin_context(authorization, db)
        if ctx["admin"].get("admin_role") != "superadmin":
            raise HTTPException(status_code=403, detail="Solo un superadministrador puede eliminar usuarios")
        db["users"] = [u for u in db["users"] if u.get("id") != user_id]
        for name, rows in db["tables"].items():
            db["tables"][name] = [r for r in rows if r.get("user_id") != user_id and r.get("id") != user_id]
        write_db(db)
    return {"data": {"ok": True}, "error": None}


def require_admin_context(authorization: Optional[str], db: Dict[str, Any]) -> Dict[str, Any]:
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    if not panel_email_allowed(user):
        raise HTTPException(
            status_code=403,
            detail="El panel de administración está restringido a las cuentas autorizadas de Dataris",
        )
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

        if company_id:
            # El paquete de la empresa es el techo, también para el superadmin:
            # conceder por usuario algo que su empresa no tiene contratado
            # dejaba accesos imposibles de encontrar después. Si hace falta, se
            # añade primero a la empresa.
            enabled_company_modules = module_access.company_enabled_module_ids(
                table(db, "company_modules"), company_id
            )
            selected_modules = [
                module_id
                for module_id in selected_modules
                if module_catalog.canonical_module_id(module_id) in enabled_company_modules
            ]

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
            "country": normalize_country(payload.get("country")),
            "hectareas": assigned_hectares,
            "max_users": 0,
            "created_at": t,
            "updated_at": t,
        })

        app_role = "admin" if admin_role in {"superadmin", "company_admin"} else "user"
        table(db, "user_roles").append({"id": str(uuid.uuid4()), "user_id": user_id, "role": app_role, "created_at": t})

        # Permiso para cargar lotes en nombre de otros usuarios (equipo comercial
        # y de desarrollo). El alcance global solo lo concede un superadmin.
        can_manage_parcels_flag = bool(payload.get(PARCEL_MANAGER_FIELD))
        can_manage_all_parcels_flag = bool(payload.get(PARCEL_MANAGER_ALL_FIELD)) and is_super_admin

        admin_user_id = str(uuid.uuid4())
        admin_row = {
            "id": admin_user_id,
            "user_id": user_id,
            "company_id": company_id,
            "admin_role": admin_role,
            "assigned_hectares": assigned_hectares,
            "created_by": current_admin.get("id"),
            "is_active": is_active,
            PARCEL_MANAGER_FIELD: can_manage_parcels_flag,
            PARCEL_MANAGER_ALL_FIELD: can_manage_all_parcels_flag,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "admin_users").append(admin_row)

        valid_modules = {m.get("id") for m in table(db, "platform_modules") if m.get("is_active", True)}
        chosen = {module_catalog.canonical_module_id(m) for m in selected_modules if m in valid_modules}
        inherited = module_access.company_enabled_module_ids(table(db, "company_modules"), company_id)

        # Solo se guarda lo que difiere del paquete de la empresa: conceder por
        # usuario lo que ya hereda lo dejaría anclado a la foto de hoy, y quitar
        # algo exige la negativa explícita para que el cálculo no lo herede.
        for module_id in sorted(chosen | inherited):
            if module_id not in valid_modules:
                continue
            enabled = module_id in chosen
            if enabled and module_id in inherited:
                continue
            table(db, "user_modules").append({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "admin_user_id": admin_user_id,
                "module_id": module_id,
                "is_enabled": enabled,
                "is_active": enabled,
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


@router.get("/admin/panel-access")
def admin_panel_access(authorization: Optional[str] = Header(default=None)):
    """¿Puede la cuenta del bearer entrar al panel /admin?

    Es la fuente de verdad que consulta el frontend en /admin/login y en el
    layout del panel: exige estar en la lista blanca del panel Y conservar una
    fila activa de `admin_users` con algún privilegio (rol de administrador,
    gestión de lotes u onboarding de clientes). No revela nada más.
    """
    db = read_db()
    user = bearer_user(authorization)
    allowed = False
    if user and panel_email_allowed(user):
        admin = active_admin_row(db, str(user.get("id") or ""))
        role = (admin or {}).get("admin_role")
        allowed = bool(admin) and (
            role in {"superadmin", "company_admin"}
            or bool((admin or {}).get(PARCEL_MANAGER_FIELD))
            or bool((admin or {}).get(CLIENT_ONBOARDER_FIELD))
        )
    return {"data": {"allowed": allowed}, "error": None}


@router.get("/admin/clients/context")
def client_onboarding_context(authorization: Optional[str] = Header(default=None)):
    """Permiso del usuario actual para dar de alta clientes.

    Lo consume el panel para decidir si muestra la pantalla de alta de clientes.
    """
    user = bearer_user(authorization)
    db = read_db()
    if not user:
        return {"data": {"allowed": False, "is_superadmin": False}, "error": None}
    admin = active_admin_row(db, str(user.get("id") or ""))
    return {
        "data": {
            "allowed": panel_email_allowed(user) and can_onboard_clients(db, str(user.get("id") or "")),
            "is_superadmin": bool(admin and admin.get("admin_role") == "superadmin"),
        },
        "error": None,
    }


@router.post("/admin/clients/onboard")
def onboard_client(payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    """Alta de un cliente nuevo: crea la empresa y su administrador de empresa.

    Pensado para el onboarding comercial: quien tenga el permiso
    `can_onboard_clients` (o sea superadmin) puede crear una cuenta cliente
    completa sin necesidad de acceso administrativo general. NUNCA crea
    superadministradores: el usuario creado es siempre `company_admin` de la
    empresa recién creada, de modo que no hay forma de escalar privilegios por
    esta vía.
    """
    with LOCK:
        db = read_db()
        user = bearer_user(authorization)
        if not user or not panel_email_allowed(user) or not can_onboard_clients(db, str(user.get("id") or "")):
            raise HTTPException(status_code=403, detail="No autorizado para dar de alta clientes")

        company_name = str(payload.get("company_name") or "").strip()
        if not company_name:
            raise HTTPException(status_code=400, detail="El nombre de la empresa es obligatorio")

        email = clean_email(payload.get("email"))
        if not email:
            raise HTTPException(status_code=400, detail="El correo del administrador es obligatorio")
        if any(str(u.get("email", "")).lower() == email for u in db.get("users", [])):
            raise HTTPException(status_code=400, detail="Ya existe un usuario con ese correo")

        password = clean_password(payload.get("password"))
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")

        first_name = str(payload.get("first_name") or "").strip() or None
        last_name = str(payload.get("last_name") or "").strip() or None
        max_hectares = float(payload.get("max_hectares") or 0)
        country = normalize_country(payload.get("country"))

        t = now()

        # 1) Empresa nueva y aislada.
        company_id = str(uuid.uuid4())
        company = {
            "id": company_id,
            "name": company_name,
            "max_hectares": max_hectares,
            "used_hectares": 0,
            "is_active": True,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "companies").append(company)

        # 2) Módulos por defecto de la empresa (los que existan en el catálogo).
        valid_modules = {m.get("id") for m in table(db, "platform_modules") if m.get("is_active", True)}
        requested_modules = payload.get("modules")
        module_ids = [m for m in requested_modules if isinstance(requested_modules, list) and m in valid_modules] if isinstance(requested_modules, list) else []
        if not module_ids:
            module_ids = [m for m in CLIENT_DEFAULT_MODULE_IDS if m in valid_modules]
        for module_id in module_ids:
            table(db, "company_modules").append({
                "id": str(uuid.uuid4()),
                "company_id": company_id,
                "module_id": module_id,
                "is_enabled": True,
                "created_at": t,
                "updated_at": t,
            })

        # 3) Usuario administrador de ESA empresa (nunca superadmin).
        user_id = str(uuid.uuid4())
        new_user = {
            "id": user_id,
            "email": email,
            "password_hash": password_hash(password),
            "is_active": True,
            "created_at": t,
            "updated_at": t,
            "user_metadata": {"first_name": first_name, "last_name": last_name, "manual_password": True},
        }
        db.setdefault("users", []).append(new_user)

        table(db, "profiles").append({
            "id": user_id,
            "user_id": user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": company_name,
            "company_id": company_id,
            "country": country,
            "max_users": 0,
            "created_at": t,
            "updated_at": t,
        })
        table(db, "user_roles").append({"id": str(uuid.uuid4()), "user_id": user_id, "role": "admin", "created_at": t})

        admin_user_id = str(uuid.uuid4())
        admin_row = {
            "id": admin_user_id,
            "user_id": user_id,
            "company_id": company_id,
            "admin_role": "company_admin",
            "assigned_hectares": 0,
            "created_by": user.get("id"),
            "is_active": True,
            PARCEL_MANAGER_FIELD: False,
            PARCEL_MANAGER_ALL_FIELD: False,
            "created_at": t,
            "updated_at": t,
        }
        table(db, "admin_users").append(admin_row)

        # El administrador del cliente hereda el paquete de su empresa: no se
        # duplica en `user_modules`. Una fila por usuario es un override y solo
        # debe existir cuando alguien decide algo distinto para esa persona.

        write_db(db)

    return {
        "data": {
            "company": company,
            "user": public_user(new_user),
            "modules": module_ids,
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


def guard_admin_users_write(
    db: Dict[str, Any],
    table_name: str,
    user: Optional[Dict[str, Any]],
    data: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Solo un administrador toca la tabla de administradores.

    Importa porque ahí vive `can_manage_parcels`: sin esta comprobación,
    cualquier usuario podría concederse a sí mismo el permiso de cargar lotes
    que este módulo acaba de restringir. Un `company_admin` no puede repartir
    roles de superadmin ni el alcance global de lotes.
    """
    if table_name != "admin_users":
        return None
    actor = active_admin_row(db, str((user or {}).get("id") or "")) if user else None
    if not actor or actor.get("admin_role") not in {"superadmin", "company_admin"} or not panel_email_allowed(user):
        raise HTTPException(status_code=403, detail="No autorizado para administrar usuarios")
    if actor.get("admin_role") != "superadmin" and isinstance(data, dict):
        # Un no-superadmin no reparte roles ni alcance global de lotes. No basta
        # con borrar el campo (add_defaults lo repondría): se fija de forma
        # explícita al mínimo privilegio y se ancla la fila a la empresa del
        # actor, para que no pueda crear administradores ni sembrar filas en la
        # empresa de otro cliente.
        data["admin_role"] = "company_user"
        data[PARCEL_MANAGER_ALL_FIELD] = False
        data["company_id"] = actor.get("company_id")
    return actor


def admin_users_rows_in_scope(
    db: Dict[str, Any],
    table_name: str,
    actor: Optional[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Un `company_admin` solo modifica administradores de su propia empresa."""
    if table_name != "admin_users" or not actor or actor.get("admin_role") == "superadmin":
        return rows
    company_id = actor.get("company_id")
    return [row for row in rows if str(row.get("company_id") or "") == str(company_id or "")]


def guard_parcel_table_write(db: Dict[str, Any], table_name: str, user: Optional[Dict[str, Any]]) -> None:
    """Escribir en `parcels` desde el API genérico exige permiso de gestión.

    El cliente ya no da de alta ni borra sus lotes: lo hace el equipo de Dataris
    desde el panel de administración.
    """
    if table_name != "parcels":
        return
    if user and can_manage_parcels(db, str(user.get("id") or "")):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "La gestión de lotes la realiza el equipo de Dataris. "
            "Solicita los cambios a tu contacto comercial o de soporte."
        ),
    )


# Tablas cuya escritura concede acceso, módulos o define el catálogo de empresas
# y de la plataforma. Nunca deben escribirse desde una petición anónima ni desde
# un usuario sin el rol adecuado, porque son la puerta real de escalada (por
# ejemplo, `user_modules` gobierna qué módulos ve cada usuario).
# El catálogo de módulos de la plataforma es exclusivo del personal de Dataris.
SUPERADMIN_WRITE_TABLES = {"platform_modules"}
# El resto de tablas que conceden acceso, módulos o definen empresas: reservadas
# a administradores (superadmin o company_admin). Lo importante es que ni un
# usuario normal ni una petición anónima puedan tocarlas; `company_modules`,
# `companies` y `user_modules` siguen usándolas los administradores de empresa
# para gestionar a los suyos.
ADMIN_WRITE_TABLES = {"user_roles", "user_modules", "companies", "company_modules"}


def guard_privileged_table_write(db: Dict[str, Any], table_name: str, user: Optional[Dict[str, Any]]) -> None:
    """Restringe la escritura de las tablas que gobiernan acceso y roles.

    - `platform_modules`: sólo el superadmin de la plataforma (personal de Dataris).
    - `user_roles`, `user_modules`, `companies`, `company_modules`: superadmin o
      `company_admin`.

    La tabla `admin_users` tiene su propio guardián (guard_admin_users_write) y
    `parcels` el suyo (guard_parcel_table_write); `profiles` se deja para que cada
    usuario edite el suyo desde su perfil.
    """
    if table_name not in SUPERADMIN_WRITE_TABLES and table_name not in ADMIN_WRITE_TABLES:
        return
    actor = active_admin_row(db, str((user or {}).get("id") or "")) if user else None
    role = actor.get("admin_role") if actor else None
    if table_name in SUPERADMIN_WRITE_TABLES:
        if role != "superadmin" or not panel_email_allowed(user):
            raise HTTPException(status_code=403, detail="Solo un superadministrador puede modificar esta información")
        return
    if role not in {"superadmin", "company_admin"} or not panel_email_allowed(user):
        raise HTTPException(status_code=403, detail="No autorizado para modificar accesos o roles")


def guard_module_catalog_write(table_name: str, operation: str) -> None:
    """El catálogo de módulos no se crea ni se borra desde el panel.

    Un módulo es código (una ruta y su guardián en el frontend). Las filas que se
    creaban a mano nacían con un `id` UUID sin ruta detrás — se activaban y no
    aparecía nada en la plataforma — y las que se borraban volvían solas en el
    siguiente arranque porque `normalize_db()` resiembra el catálogo. Lo único
    que el operador decide es si el módulo está activo (y su ficha la manda
    app/services/module_catalog.py).
    """
    if table_name != "platform_modules" or operation not in {"insert", "upsert", "delete"}:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "El catálogo de módulos lo define el producto: se pueden activar o desactivar, "
            "pero no crear ni eliminar módulos desde el panel."
        ),
    )


@router.post("/tables/{table_name}/insert")
def insert(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict), authorization: Optional[str] = Header(default=None)):
    user = bearer_user(authorization)
    incoming = payload.get("data", payload)
    items = incoming if isinstance(incoming, list) else [incoming]
    with LOCK:
        db = read_db()
        guard_parcel_table_write(db, table_name, user)
        guard_privileged_table_write(db, table_name, user)
        guard_module_catalog_write(table_name, "insert")
        for item in items:
            guard_admin_users_write(db, table_name, user, item if isinstance(item, dict) else None)
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
        guard_parcel_table_write(db, table_name, user)
        guard_privileged_table_write(db, table_name, user)
        guard_module_catalog_write(table_name, "upsert")
        for item in items:
            guard_admin_users_write(db, table_name, user, item if isinstance(item, dict) else None)
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
        guard_parcel_table_write(db, table_name, user)
        guard_privileged_table_write(db, table_name, user)
        actor = guard_admin_users_write(db, table_name, user, payload.get("data") if isinstance(payload.get("data"), dict) else None)
        rows = scoped_table_rows(db, table_name, user)
        targets = apply_filters(rows, payload.get("filters") or [])
        targets = admin_users_rows_in_scope(db, table_name, actor, targets)
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
def delete(
    table_name: str,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    with LOCK:
        db = read_db()
        guard_parcel_table_write(db, table_name, user)
        guard_privileged_table_write(db, table_name, user)
        guard_module_catalog_write(table_name, "delete")
        actor = guard_admin_users_write(db, table_name, user)
        rows = table(db, table_name)
        scoped_rows = scoped_table_rows(db, table_name, user)
        targets = apply_filters(scoped_rows, payload.get("filters") or [])
        targets = admin_users_rows_in_scope(db, table_name, actor, targets)
        ids = {id(r) for r in targets}
        db["tables"][table_name] = [r for r in rows if id(r) not in ids]
        write_db(db)
    if table_name == "parcels":
        # The lot is gone locally; remove its parcels from the user's Graniot
        # account too so both sides stay in sync.
        schedule_graniot_parcel_delete(background_tasks, user, targets)
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


# ---------------------------------------------------------------------------
# Graniot mirror for lots
# ---------------------------------------------------------------------------
# The Satélite module embeds each user's own Graniot portal, so a lot created in
# Dataris must also exist in that user's Graniot account (and disappear from it
# when the lot is deleted). Both operations go through Graniot's API, which is
# what removes the dependency on Graniot's commercial team.
#
# Everything here is best-effort: Graniot being slow or unreachable must never
# break creating or deleting a lot in Dataris. Failures are recorded in the
# lot's `graniot_sync_error` field (see the graniot router) and in the logs.


def _graniot_log(event: str, **fields: Any) -> None:
    try:
        from app.services.graniot_debug import log_event

        log_event({"event": event, **fields})
    except Exception:
        pass


async def _graniot_sync_parcels_task(user: Dict[str, Any], parcel_ids: List[str]) -> None:
    from app.api.routers.graniot import sync_local_parcel_to_graniot

    for parcel_id in parcel_ids:
        try:
            await sync_local_parcel_to_graniot(
                user,
                parcel_id,
                {"metadata": {"origin": "dataris-autosync"}},
                require_account=True,
                # Re-subir el mismo archivo actualiza la fila local en su sitio:
                # si ya tiene parcelas en Graniot hay que actualizarlas, no
                # crear un duplicado en el portal del usuario.
                prefer_update=True,
            )
            _graniot_log(
                "dataris.compat.parcel_autosync.ok",
                operation="parcel-autosync",
                local_parcel_id=parcel_id,
            )
        except HTTPException as exc:
            # 409 = the user has no Graniot account with that email. Expected for
            # Dataris-only users; nothing is pushed anywhere.
            _graniot_log(
                "dataris.compat.parcel_autosync.skipped" if exc.status_code == 409 else "dataris.compat.parcel_autosync.failed",
                operation="parcel-autosync",
                local_parcel_id=parcel_id,
                status_code=exc.status_code,
                message=str(exc.detail),
            )
        except Exception as exc:  # noqa: BLE001 — a background task must never bubble up
            _graniot_log(
                "dataris.compat.parcel_autosync.failed",
                operation="parcel-autosync",
                local_parcel_id=parcel_id,
                exception_type=type(exc).__name__,
                message=str(exc),
            )


async def _graniot_delete_parcels_task(user: Dict[str, Any], snapshots: List[Dict[str, Any]]) -> None:
    from app.api.routers.graniot import delete_parcel_from_graniot

    for snapshot in snapshots:
        try:
            # The local row is already gone, so there is nothing left to clear.
            result = await delete_parcel_from_graniot(user, snapshot, clear_local=False)
            _graniot_log(
                "dataris.compat.parcel_autodelete.failed" if result.get("failed") else "dataris.compat.parcel_autodelete.ok",
                operation="parcel-autodelete",
                local_parcel_id=snapshot.get("id"),
                deleted=result.get("deleted"),
                missing=result.get("missing"),
                failed=result.get("failed"),
            )
        except Exception as exc:  # noqa: BLE001 — a background task must never bubble up
            _graniot_log(
                "dataris.compat.parcel_autodelete.failed",
                operation="parcel-autodelete",
                local_parcel_id=snapshot.get("id"),
                exception_type=type(exc).__name__,
                message=str(exc),
            )


def schedule_graniot_parcel_sync(
    background: Optional[BackgroundTasks],
    user: Optional[Dict[str, Any]],
    parcels: Iterable[Dict[str, Any]],
) -> None:
    if background is None or not user or not settings.GRANIOT_PARCEL_AUTOSYNC_ENABLED:
        return
    if not settings.GRANIOT_PARCEL_SYNC_PER_USER_ENABLED:
        return
    parcel_ids = [str(row.get("id")) for row in parcels if isinstance(row, dict) and row.get("id")]
    if not parcel_ids:
        return
    background.add_task(_graniot_sync_parcels_task, dict(user), parcel_ids)


def schedule_graniot_parcel_delete(
    background: Optional[BackgroundTasks],
    user: Optional[Dict[str, Any]],
    parcels: Iterable[Dict[str, Any]],
) -> None:
    if background is None or not user or not settings.GRANIOT_PARCEL_AUTODELETE_ENABLED:
        return
    snapshots = [
        dict(row)
        for row in parcels
        if isinstance(row, dict) and (row.get("graniot_parcel_id") or row.get("graniot_parcels") or row.get("graniot_raw"))
    ]
    if not snapshots:
        return
    background.add_task(_graniot_delete_parcels_task, dict(user), snapshots)


def require_parcel_write_access(user: Dict[str, Any]) -> None:
    """Los lotes solo los cargan las cuentas con permiso de gestión.

    La carga desde el perfil del cliente quedó desactivada: la hace el equipo de
    Dataris desde el panel de administración (`/compat/admin/parcels`).
    """
    db = read_db()
    if can_manage_parcels(db, str(user.get("id") or "")):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "La carga de lotes la realiza el equipo de Dataris. "
            "Solicita el alta de tus lotes a tu contacto comercial o de soporte."
        ),
    )


async def store_parcel_file_for_user(
    owner: Dict[str, Any],
    file: UploadFile,
    name: str,
) -> List[Dict[str, Any]]:
    """Guarda el archivo geográfico y registra sus lotes a nombre de `owner`."""
    if not name.strip():
        raise HTTPException(status_code=400, detail="Nombre de parcela requerido")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Archivo requerido")

    ensure_storage()
    clean_original = Path(file.filename.replace("..", "_")).name
    storage_path = Path(owner["id"]) / f"{int(datetime.now(timezone.utc).timestamp())}-{clean_original}"
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
                    "user_id": owner["id"],
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
                created_rows.append(upsert_user_parcel(db, owner["id"], row))
            write_db(db)
        return created_rows
    except ValueError as exc:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise
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


def create_manual_parcel_for_user(owner: Dict[str, Any], name: str, geometry: Any) -> Dict[str, Any]:
    """Registra a nombre de `owner` un lote dibujado sobre el mapa."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Nombre de lote requerido")
    if not geometry:
        raise HTTPException(status_code=400, detail="Dibuja al menos tres puntos para crear el lote")

    t = now()
    row = normalize_record_geometries("parcels", {
        "id": str(uuid.uuid4()),
        "user_id": owner["id"],
        "name": clean_name,
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
        row = upsert_user_parcel(db, owner["id"], row)
        write_db(db)
    return row


@router.post("/parcels/upload")
async def upload_parcel_from_satellite(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    require_parcel_write_access(user)

    created_rows = await store_parcel_file_for_user(user, file, name)
    schedule_graniot_parcel_sync(background_tasks, user, created_rows)
    return {"data": {"parcel": created_rows[0], "parcels": created_rows}, "error": None}


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
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(default=None),
):
    user = bearer_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    require_parcel_write_access(user)

    row = create_manual_parcel_for_user(user, payload.get("name"), payload.get("geometry"))
    schedule_graniot_parcel_sync(background_tasks, user, [row])
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
