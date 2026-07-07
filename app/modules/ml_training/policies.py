"""Capacidades del módulo de entrenamiento de modelos.

"Laboratorio de IA" es una herramienta INTERNA de Dataris (entrena modelos
sobre datos propios de la plataforma) — a diferencia del resto de módulos,
no es algo que una empresa cliente pueda tener asignado. Por eso el gate real
no es el AppRole por-usuario (app/models/user_roles.py, que no tiene ningún
concepto de empresa/tenant — ver el comentario de repository.py), sino el
`admin_role` del sistema de cuentas multi-tenant ("compat", el mismo que usa
GET /me/access): solo admin_role == "superadmin" es la cuenta admin real de
Dataris. Los admins de empresas cliente son "company_admin", y la cuenta demo
comercial también es "company_admin" (ver commercial_demo_seed.py) — ninguno
de los dos debe pasar este gate aunque tenga fila AppRole.admin en la tabla
SQL user_roles.
"""
from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.user_roles import AppRole, UserRole


# tecnico/visualizador: solo lectura. supervisor_campo: gestiona sus propios
# proyectos/datasets/jobs. admin: además administra límites, recetas y puede
# cancelar/gestionar jobs de otros usuarios. Estos matices solo aplican ya
# dentro del propio equipo de Dataris — ver _is_dataris_superadmin abajo.
_VIEW_ROLES = {AppRole.tecnico, AppRole.visualizador, AppRole.supervisor_campo, AppRole.admin}
_MANAGE_ROLES = {AppRole.supervisor_campo, AppRole.admin}
_ADMIN_ROLES = {AppRole.admin}


class MLTrainingCapabilities:
    def __init__(self, roles: set[AppRole]):
        self.roles = roles
        self.can_view = bool(roles & _VIEW_ROLES)
        self.can_manage = bool(roles & _MANAGE_ROLES)
        self.can_admin = bool(roles & _ADMIN_ROLES)


def _user_roles(db: Session, user_id: str) -> set[AppRole]:
    rows = db.query(UserRole).filter(UserRole.user_id == user_id).all()
    return {r.role for r in rows}


def _is_dataris_superadmin(user_id: str) -> bool:
    from app.api.routers.compat import LOCK, read_db
    from app.api.routers.compat_extensions import admin_record_for
    from app.services.commercial_demo_seed import is_commercial_demo_user

    if is_commercial_demo_user(user_id):
        return False
    with LOCK:
        db = read_db()
        admin = admin_record_for(db, user_id) or {}
    return admin.get("admin_role") == "superadmin"


def get_ml_capabilities(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MLTrainingCapabilities:
    if not _is_dataris_superadmin(current_user["id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    roles = _user_roles(db, current_user["id"])
    # Un usuario sin fila en user_roles conserva acceso de solo lectura por
    # defecto (mismo comportamiento implícito que el resto de la plataforma:
    # el rol solo restringe capacidades de escritura/administración). En la
    # práctica el superadmin de Dataris también debería poder gestionar, no
    # solo ver, su propia herramienta interna.
    if not roles:
        roles = {AppRole.admin}
    return MLTrainingCapabilities(roles)


def _ensure_module_enabled() -> None:
    # Deliberadamente NO se aplica dentro de get_ml_capabilities: /ml/status
    # depende de esa función directamente para poder informar
    # "module_enabled: false" al frontend en vez de devolver un 403 sin contexto.
    if not settings.ML_TRAINING_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="El módulo de entrenamiento de IA no está habilitado")


def require_ml_view(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    _ensure_module_enabled()
    if not caps.can_view:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps


def require_ml_manage(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    _ensure_module_enabled()
    if not caps.can_manage:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps


def require_ml_admin(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    _ensure_module_enabled()
    if not caps.can_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps
