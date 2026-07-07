import uuid

import pytest
from fastapi import HTTPException

from app.models.user_roles import AppRole, UserRole
from app.modules.ml_training.policies import MLTrainingCapabilities, get_ml_capabilities


def _add_role(db_session, user_id: str, role: AppRole):
    db_session.add(UserRole(user_id=user_id, role=role))
    db_session.commit()


class TestMLTrainingCapabilities:
    def test_visualizador_can_only_view(self):
        caps = MLTrainingCapabilities({AppRole.visualizador})
        assert caps.can_view is True
        assert caps.can_manage is False
        assert caps.can_admin is False

    def test_tecnico_can_only_view(self):
        caps = MLTrainingCapabilities({AppRole.tecnico})
        assert caps.can_view is True
        assert caps.can_manage is False

    def test_supervisor_campo_can_manage(self):
        caps = MLTrainingCapabilities({AppRole.supervisor_campo})
        assert caps.can_view is True
        assert caps.can_manage is True
        assert caps.can_admin is False

    def test_admin_can_do_everything(self):
        caps = MLTrainingCapabilities({AppRole.admin})
        assert caps.can_view is True
        assert caps.can_manage is True
        assert caps.can_admin is True

    def test_dataris_superadmin_without_explicit_role_defaults_to_full_admin(self, db_session):
        # El gate real de este módulo es "es el superadmin de Dataris" (mockeado
        # a True por el fixture autouse de conftest.py) — sin fila explícita en
        # user_roles, esa cuenta obtiene control total sobre su propia
        # herramienta interna, no acceso de solo lectura.
        user_id = str(uuid.uuid4())
        caps = get_ml_capabilities(current_user={"id": user_id}, db=db_session)
        assert caps.can_view is True
        assert caps.can_manage is True
        assert caps.can_admin is True

    def test_user_with_supervisor_role_from_db(self, db_session):
        user_id = str(uuid.uuid4())
        _add_role(db_session, user_id, AppRole.supervisor_campo)
        caps = get_ml_capabilities(current_user={"id": user_id}, db=db_session)
        assert caps.can_manage is True
        assert caps.can_admin is False


class TestDatarisSuperadminGate:
    def test_non_dataris_superadmin_gets_403(self, db_session, monkeypatch):
        monkeypatch.setattr("app.modules.ml_training.policies._is_dataris_superadmin", lambda user_id: False)
        user_id = str(uuid.uuid4())
        _add_role(db_session, user_id, AppRole.admin)  # incluso con AppRole.admin en SQL...
        with pytest.raises(HTTPException) as exc_info:
            get_ml_capabilities(current_user={"id": user_id}, db=db_session)
        assert exc_info.value.status_code == 403  # ...no es la cuenta admin real de Dataris

    def test_commercial_demo_user_never_passes_even_with_superadmin_role_row(self, monkeypatch):
        from app.modules.ml_training.policies import _is_dataris_superadmin
        from app.services.commercial_demo_seed import DEMO_USER_ID

        # Incluso si por error el registro admin_users del demo tuviera
        # admin_role="superadmin", is_commercial_demo_user debe bloquearlo igual.
        monkeypatch.setattr(
            "app.api.routers.compat_extensions.admin_record_for",
            lambda db, user_id: {"admin_role": "superadmin"},
        )
        monkeypatch.setattr(
            "app.services.commercial_demo_seed.is_commercial_demo_user",
            lambda user_id: user_id == DEMO_USER_ID,
        )
        assert _is_dataris_superadmin(DEMO_USER_ID) is False
