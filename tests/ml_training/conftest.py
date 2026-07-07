import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_db
from app.api.routers.ml_training import router as ml_training_router
from app.core.config import settings


class _CurrentUserHolder:
    def __init__(self):
        self.user_id: str | None = None

    def __call__(self):
        return {"id": self.user_id, "role": "user"}


@pytest.fixture()
def current_user_holder():
    return _CurrentUserHolder()


@pytest.fixture(autouse=True)
def _ml_training_module_enabled():
    """Las policies exigen settings.ML_TRAINING_ENABLED=True (403 en caso
    contrario); estos tests validan el comportamiento del módulo habilitado."""
    original = settings.ML_TRAINING_ENABLED
    settings.ML_TRAINING_ENABLED = True
    yield
    settings.ML_TRAINING_ENABLED = original


@pytest.fixture(autouse=True)
def _ml_training_default_dataris_superadmin(monkeypatch):
    """ML Training es una herramienta interna: policies.get_ml_capabilities
    exige admin_role=="superadmin" en el sistema de cuentas multi-tenant
    (compat), separado del AppRole por-usuario que estos tests ejercitan.
    Se mockean las dos dependencias externas de _is_dataris_superadmin
    (nunca su propia lógica) para que, por defecto, cualquier usuario de
    estos tests se resuelva como el superadmin real de Dataris — así los
    tests de aislamiento/roles (ortogonales a este gate) siguen probando lo
    que probaban, sin tocar la base de datos real del store "compat". Los
    tests que verifican el gate en sí mismo sobreescriben estos mocks."""
    monkeypatch.setattr("app.api.routers.compat.read_db", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.api.routers.compat_extensions.admin_record_for",
        lambda db, user_id: {"admin_role": "superadmin"},
    )
    monkeypatch.setattr("app.services.commercial_demo_seed.is_commercial_demo_user", lambda user_id: False)


@pytest.fixture()
def api_client(db_session, current_user_holder):
    app = FastAPI()
    app.include_router(ml_training_router, prefix=settings.API_V1_STR)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = current_user_holder

    with TestClient(app) as client:
        yield client
