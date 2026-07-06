"""Capacidades del módulo de entrenamiento de modelos, derivadas de AppRole.

Dataris no tiene un sistema de permisos granular (no existe RBAC tipo
ML_TRAINING_VIEW/MANAGE/ADMIN en ningún otro módulo). En vez de crear un
sistema paralelo, este archivo traduce el enum AppRole ya existente
(app/models/user_roles.py) a las tres capacidades pedidas para este módulo.
"""
from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user_roles import AppRole, UserRole


# tecnico/visualizador: solo lectura. supervisor_campo: gestiona sus propios
# proyectos/datasets/jobs. admin: además administra límites, recetas y puede
# cancelar/gestionar jobs de otros usuarios.
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


def get_ml_capabilities(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MLTrainingCapabilities:
    roles = _user_roles(db, current_user["id"])
    # Un usuario sin fila en user_roles conserva acceso de solo lectura por
    # defecto (mismo comportamiento implícito que el resto de la plataforma:
    # el rol solo restringe capacidades de escritura/administración).
    if not roles:
        roles = {AppRole.visualizador}
    return MLTrainingCapabilities(roles)


def require_ml_view(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    if not caps.can_view:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps


def require_ml_manage(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    if not caps.can_manage:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps


def require_ml_admin(
    caps: MLTrainingCapabilities = Depends(get_ml_capabilities),
) -> MLTrainingCapabilities:
    if not caps.can_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return caps
