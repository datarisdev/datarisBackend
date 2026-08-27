from __future__ import annotations

import base64
import hashlib
from typing import Any, Dict, Iterable, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

HARVEST_FORM_TYPE = "harvest"
PEST_WEED_FORM_TYPE = "pest_weed"
# La bitácora de campo son tres formularios de AgtechApps que se sincronizan
# igual que los dos históricos, cada uno con su propio cursor. Se declaran aquí
# —y no como vínculos con plantilla— porque su destino es una tabla propia con
# los cálculos de la hoja del CDT, no un reporte libre.
FIELD_LOG_FORM_TYPE = "field_log"
FIELD_LOG_CYCLE_FORM_TYPE = "field_log_cycle"
FIELD_LOG_PHENOLOGY_FORM_TYPE = "field_log_phenology"
SUPPORTED_FORM_TYPES = (
    HARVEST_FORM_TYPE,
    PEST_WEED_FORM_TYPE,
    FIELD_LOG_FORM_TYPE,
    FIELD_LOG_CYCLE_FORM_TYPE,
    FIELD_LOG_PHENOLOGY_FORM_TYPE,
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _fernet() -> Fernet:
    """Return the tenant credential cipher.

    Production should define DIGIFORMS_CREDENTIALS_ENCRYPTION_KEY. For local
    development and backward-compatible deployments, a deterministic Fernet key
    is derived from JWT_SECRET_KEY so the app can boot without exposing plain
    credentials in the compatibility state document.
    """
    configured = _text(settings.DIGIFORMS_CREDENTIALS_ENCRYPTION_KEY)
    if configured:
        try:
            return Fernet(configured.encode("utf-8"))
        except (ValueError, TypeError):
            # Accept a high-entropy passphrase as well as a raw Fernet key.
            digest = hashlib.sha256(configured.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
    fallback = _text(settings.JWT_SECRET_KEY) or "change_me_only_for_local_dev"
    digest = hashlib.sha256(f"dataris:digiforms:{fallback}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encryption_key_is_explicit() -> bool:
    return bool(_text(settings.DIGIFORMS_CREDENTIALS_ENCRYPTION_KEY))


def encrypt_secret(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError(
            "No se pudo descifrar la contraseña DigiForms de la empresa. "
            "Verifica DIGIFORMS_CREDENTIALS_ENCRYPTION_KEY."
        ) from exc


def connection_for_company(db: Dict[str, Any], company_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not company_id:
        return None
    rows = db.setdefault("tables", {}).setdefault("digiforms_connections", [])
    return next((row for row in rows if str(row.get("company_id") or "") == str(company_id)), None)


def mapping_for_company(db: Dict[str, Any], company_id: Optional[str], form_type: str) -> Optional[Dict[str, Any]]:
    if not company_id:
        return None
    rows = db.setdefault("tables", {}).setdefault("digiforms_form_mappings", [])
    return next(
        (
            row
            for row in rows
            if str(row.get("company_id") or "") == str(company_id)
            and str(row.get("form_type") or "") == str(form_type)
        ),
        None,
    )


def mappings_for_company(db: Dict[str, Any], company_id: Optional[str]) -> List[Dict[str, Any]]:
    if not company_id:
        return []
    rows = db.setdefault("tables", {}).setdefault("digiforms_form_mappings", [])
    return [row for row in rows if str(row.get("company_id") or "") == str(company_id)]


def configured_form_types(db: Dict[str, Any], company_id: Optional[str]) -> List[str]:
    configured: List[str] = []
    for form_type in SUPPORTED_FORM_TYPES:
        row = mapping_for_company(db, company_id, form_type)
        if row and row.get("is_enabled", True) is not False and _text(row.get("form_id")):
            configured.append(form_type)
    return configured


def runtime_credentials(connection: Optional[Dict[str, Any]]) -> Dict[str, str]:
    row = connection or {}
    return {
        "client_id": _text(row.get("client_id")),
        "api_user": _text(row.get("api_user")),
        "api_password": decrypt_secret(row.get("encrypted_api_password")) if row.get("encrypted_api_password") else "",
    }


def safe_connection(connection: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not connection:
        return None
    return {
        "id": connection.get("id"),
        "company_id": connection.get("company_id"),
        "client_id": connection.get("client_id"),
        "api_user": connection.get("api_user"),
        "has_password": bool(connection.get("encrypted_api_password")),
        "connection_status": connection.get("connection_status") or "not_tested",
        "last_connection_test_at": connection.get("last_connection_test_at"),
        "last_connection_error": connection.get("last_connection_error"),
        "auto_sync_enabled": connection.get("auto_sync_enabled", True) is not False,
        "created_at": connection.get("created_at"),
        "updated_at": connection.get("updated_at"),
    }


def safe_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "id": mapping.get("id"),
        "company_id": mapping.get("company_id"),
        "form_type": mapping.get("form_type"),
        "display_name": mapping.get("display_name"),
        "form_id": mapping.get("form_id"),
        "is_enabled": mapping.get("is_enabled", True) is not False,
        "initial_response_id": int(mapping.get("initial_response_id") or 0),
        "initial_sync_start_date": mapping.get("initial_sync_start_date"),
        "initial_sync_end_date": mapping.get("initial_sync_end_date"),
        "created_at": mapping.get("created_at"),
        "updated_at": mapping.get("updated_at"),
    }
    # Los vínculos con plantillas de Reportes de Campo viven en esta misma tabla
    # y llevan además a qué plantilla apuntan y cómo se traducen sus campos.
    if mapping.get("report_template_id"):
        payload.update({
            "report_template_id": mapping.get("report_template_id"),
            "report_template_key": mapping.get("report_template_key"),
            "field_map": dict(mapping.get("field_map") or {}),
        })
    return payload


def safe_mappings(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [safe_mapping(row) for row in rows]
