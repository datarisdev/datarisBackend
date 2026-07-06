import uuid

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

    def test_user_without_roles_defaults_to_view_only(self, db_session):
        user_id = str(uuid.uuid4())
        caps = get_ml_capabilities(current_user={"id": user_id}, db=db_session)
        assert caps.can_view is True
        assert caps.can_manage is False

    def test_user_with_supervisor_role_from_db(self, db_session):
        user_id = str(uuid.uuid4())
        _add_role(db_session, user_id, AppRole.supervisor_campo)
        caps = get_ml_capabilities(current_user={"id": user_id}, db=db_session)
        assert caps.can_manage is True
        assert caps.can_admin is False
