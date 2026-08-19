"""Migración de `user_modules` a overrides explícitos.

El modelo pasó de "las filas del usuario sustituyen al paquete de su empresa" a
"la empresa manda y el usuario sobrescribe módulo a módulo". Para que nadie gane
accesos con el despliegue, el backfill escribe el `false` explícito de lo que el
usuario no tenía... salvo cuando sus únicas filas eran de una extensión, porque
ahí nunca hubo restricción deliberada: heredaba de su empresa hasta que la
aprobación creó la fila y se lo llevó todo por delante.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ.setdefault("DATARIS_COMPAT_PERSISTENCE", "file")
os.environ.setdefault("DATARIS_COMPAT_STORAGE_DIR", tempfile.mkdtemp(prefix="dataris-compat-backfill-"))

from app.api.routers import compat  # noqa: E402

COMPANY = "empresa-1"
RESTRINGIDO = "usuario-restringido"
SOLO_EXTENSION = "usuario-extension"
SIN_FILAS = "usuario-sin-filas"


def _db():
    return {
        "users": [],
        "tables": {
            "companies": [{"id": COMPANY, "name": "Agro"}],
            "admin_users": [
                {"id": "a1", "user_id": RESTRINGIDO, "company_id": COMPANY, "admin_role": "company_user", "is_active": True},
                {"id": "a2", "user_id": SOLO_EXTENSION, "company_id": COMPANY, "admin_role": "company_user", "is_active": True},
                {"id": "a3", "user_id": SIN_FILAS, "company_id": COMPANY, "admin_role": "company_user", "is_active": True},
            ],
            "company_modules": [
                {"company_id": COMPANY, "module_id": "satelite", "is_enabled": True},
                {"company_id": COMPANY, "module_id": "telemetria", "is_enabled": True},
                {"company_id": COMPANY, "module_id": "personal", "is_enabled": True},
            ],
            "user_modules": [
                {"user_id": RESTRINGIDO, "module_id": "satelite", "is_enabled": True},
                {"user_id": SOLO_EXTENSION, "module_id": "digiforms", "is_enabled": True},
            ],
            "profiles": [],
        },
    }


def _overrides(db, user_id):
    return {
        row["module_id"]: row.get("is_enabled")
        for row in compat.table(db, "user_modules")
        if row.get("user_id") == user_id
    }


def test_preserva_la_restriccion_deliberada():
    db = _db()
    resultado = compat.backfill_user_module_overrides(db, "2026-08-18T00:00:00Z")
    assert resultado["applied"] is True

    overrides = _overrides(db, RESTRINGIDO)
    assert overrides["satelite"] is True
    assert overrides["telemetria"] is False
    assert overrides["personal"] is False


def test_el_usuario_con_solo_una_extension_recupera_lo_heredado():
    db = _db()
    compat.backfill_user_module_overrides(db, "2026-08-18T00:00:00Z")
    overrides = _overrides(db, SOLO_EXTENSION)
    # Solo conserva su extensión: sin negativas, vuelve a heredar de la empresa.
    assert overrides == {"digiforms": True}


def test_no_toca_a_quien_no_tenia_filas():
    db = _db()
    compat.backfill_user_module_overrides(db, "2026-08-18T00:00:00Z")
    assert _overrides(db, SIN_FILAS) == {}


def test_solo_se_aplica_una_vez():
    db = _db()
    compat.backfill_user_module_overrides(db, "2026-08-18T00:00:00Z")
    filas = len(compat.table(db, "user_modules"))
    segunda = compat.backfill_user_module_overrides(db, "2026-08-19T00:00:00Z")
    assert segunda["applied"] is False
    assert len(compat.table(db, "user_modules")) == filas
