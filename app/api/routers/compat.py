
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from jose import JWTError, jwt
from pydantic import BaseModel

from app.core.config import settings
from app.services.telemetry.helicopter_processor import process_helicopter_zip
from app.services.telemetry.parcel_upload import parse_parcel_file

router = APIRouter(prefix="/compat", tags=["Frontend Compatibility"])

ROOT = Path(os.getenv("DATARIS_COMPAT_STORAGE_DIR", "app/storage")).resolve()
DB_FILE = ROOT / "compat_db.json"
FILES = ROOT / "compat_files"
LOCK = RLock()

TABLES = [
    "profiles", "user_roles", "admin_users", "companies", "platform_modules",
    "company_modules", "user_modules", "parcels", "satellite_images",
    "field_notes", "parcel_crops", "aerial_analyses", "analysis_sessions",
    "analysis_data_points", "laborapp_registros", "laborapp_empleados_foto",
]

DEFAULT_MODULES = [
    ("dashboard", "Dashboard", "Panel principal", "LayoutDashboard"),
    ("satelite", "Monitoreo Satelital", "Análisis satelital", "Satellite"),
    ("mapeo", "Mapeo", "Mapeo y análisis geoespacial", "Map"),
    ("telemetria", "Telemetría", "Indicadores y métricas", "Activity"),
    ("sig-agricola", "SIG Agrícola", "Análisis agrícola", "Sprout"),
    ("aplicaciones-aereas", "Aplicaciones Aéreas", "Control de aplicaciones", "Plane"),
    ("tareas", "Tareas", "Tablero Kanban", "Kanban"),
    ("personal", "Personal de Campo", "Control biométrico y georreferenciado", "Users"),
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
            "field_notes": [],
            "parcel_crops": [],
            "aerial_analyses": [],
            "analysis_sessions": [],
            "analysis_data_points": [],
            "laborapp_registros": [],
            "laborapp_empleados_foto": [],
        },
    }


def read_db() -> Dict[str, Any]:
    ensure_storage()
    with DB_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_db(db: Dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(DB_FILE)


def ensure_storage() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(parents=True, exist_ok=True)
    if not DB_FILE.exists():
        write_db(default_db())
        return
    try:
        with DB_FILE.open("r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        write_db(default_db())
        return
    changed = False
    db.setdefault("users", [])
    tables = db.setdefault("tables", {})
    for table in TABLES:
        if table not in tables:
            tables[table] = []
            changed = True
    t = now()
    if not tables["platform_modules"]:
        tables["platform_modules"] = [
            {"id": mid, "name": name, "description": desc, "icon": icon, "is_active": True, "created_at": t, "updated_at": t}
            for mid, name, desc, icon in DEFAULT_MODULES
        ]
        changed = True
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
                changed = True

    # If this backend is upgraded over an existing local compat_db.json, make sure
    # the new Personal de Campo module is also enabled for the default/admin company
    # and for the current users.
    for company in tables.get("companies", []):
        company_id = company.get("id")
        if not company_id:
            continue
        for mid, *_ in DEFAULT_MODULES:
            if not any(cm.get("company_id") == company_id and cm.get("module_id") == mid for cm in tables.get("company_modules", [])):
                tables.setdefault("company_modules", []).append({
                    "id": str(uuid.uuid4()),
                    "company_id": company_id,
                    "module_id": mid,
                    "is_enabled": True,
                    "created_at": t,
                    "updated_at": t,
                })
                changed = True

    for user in db.get("users", []):
        user_id = user.get("id")
        if not user_id:
            continue
        for mid, *_ in DEFAULT_MODULES:
            if not any(um.get("user_id") == user_id and um.get("module_id") == mid for um in tables.get("user_modules", [])):
                tables.setdefault("user_modules", []).append({
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "admin_user_id": None,
                    "module_id": mid,
                    "is_active": True,
                    "created_at": t,
                    "updated_at": t,
                })
                changed = True

    if changed:
        write_db(db)


def table(db: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return db.setdefault("tables", {}).setdefault(name, [])


def bearer_user(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    db = read_db()
    return next((u for u in db.get("users", []) if u.get("id") == payload.get("sub")), None)


def add_defaults(table_name: str, row: Dict[str, Any], user_id: Optional[str]) -> Dict[str, Any]:
    row = dict(row or {})
    t = now()
    row.setdefault("id", str(uuid.uuid4()))
    row.setdefault("created_at", t)
    row["updated_at"] = row.get("updated_at") or t
    if user_id and table_name in {"profiles", "user_roles", "parcels", "satellite_images", "field_notes", "parcel_crops", "aerial_analyses", "analysis_sessions", "laborapp_registros"}:
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
    if table_name == "admin_users":
        row.setdefault("admin_role", "company_admin")
        row.setdefault("is_active", True)
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
        if user:
            r.setdefault("email", user.get("email"))
        if profile:
            r.setdefault("first_name", profile.get("first_name"))
            r.setdefault("last_name", profile.get("last_name"))
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


@router.post("/tables/{table_name}/query")
def query(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    db = read_db()
    rows = [enrich(db, table_name, r) for r in table(db, table_name)]
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
            target = None
            if conflict_cols:
                target = next((r for r in rows if all(r.get(c) == row.get(c) for c in conflict_cols)), None)
            if not target and row.get("id"):
                target = next((r for r in rows if r.get("id") == row.get("id")), None)
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
def update(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    with LOCK:
        db = read_db()
        rows = table(db, table_name)
        targets = apply_filters(rows, payload.get("filters") or [])
        for row in targets:
            row.update(payload.get("data") or {})
            row["updated_at"] = now()
        result = [enrich(db, table_name, r) for r in targets]
        write_db(db)
    return {"data": result, "error": None, "count": len(result)}


@router.post("/tables/{table_name}/delete")
def delete(table_name: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    with LOCK:
        db = read_db()
        rows = table(db, table_name)
        targets = apply_filters(rows, payload.get("filters") or [])
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
        parsed = parse_parcel_file(dest, clean_original)
        t = now()
        public_url = f"/api/compat/storage/public/parcels/{str(storage_path).replace(os.sep, '/') }"
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "name": name.strip(),
            "area": parsed["area"],
            "geometry": parsed["geometry"],
            "file_url": public_url,
            "created_at": t,
            "updated_at": t,
        }
        with LOCK:
            db = read_db()
            table(db, "parcels").append(row)
            write_db(db)
        return {"data": {"parcel": row}, "error": None}
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
        raise HTTPException(status_code=500, detail=f"Error subiendo parcela: {exc}")


@router.post("/storage/{bucket}/upload")
async def storage_upload(bucket: str, path: str, file: UploadFile = File(...)):
    ensure_storage()
    clean = Path(path.replace("..", "_").lstrip("/"))
    dest = FILES / bucket / clean
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"data": {"path": str(clean).replace("\\", "/"), "fullPath": f"{bucket}/{clean}"}, "error": None}


@router.get("/storage/{bucket}/list")
def storage_list(bucket: str, prefix: str = ""):
    base = (FILES / bucket / prefix.replace("..", "_").lstrip("/")).resolve()
    root = (FILES / bucket).resolve()
    if not str(base).startswith(str(root)) or not base.exists():
        return {"data": [], "error": None}
    return {"data": [{"name": p.name, "id": p.name, "updated_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()} for p in base.iterdir() if p.is_file()], "error": None}


@router.post("/storage/{bucket}/remove")
def storage_remove(bucket: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    removed = []
    root = (FILES / bucket).resolve()
    for raw in payload.get("paths") or []:
        p = (FILES / bucket / str(raw).replace("..", "_").lstrip("/")).resolve()
        if str(p).startswith(str(root)) and p.exists():
            p.unlink()
            removed.append(str(raw))
    return {"data": removed, "error": None}


@router.get("/storage/public/{bucket}/{file_path:path}")
def storage_public(bucket: str, file_path: str):
    path = (FILES / bucket / file_path.replace("..", "_").lstrip("/")).resolve()
    root = (FILES / bucket).resolve()
    if not str(path).startswith(str(root)) or not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@router.get("/health")
def health():
    ensure_storage()
    return {"status": "ok", "storage": str(ROOT)}


ensure_storage()
