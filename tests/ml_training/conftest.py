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


@pytest.fixture()
def api_client(db_session, current_user_holder):
    app = FastAPI()
    app.include_router(ml_training_router, prefix=settings.API_V1_STR)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = current_user_holder

    with TestClient(app) as client:
        yield client
