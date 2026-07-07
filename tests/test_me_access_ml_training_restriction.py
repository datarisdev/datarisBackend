"""ML Training ("Laboratorio de IA") es una herramienta interna de Dataris:
GET /me/access nunca debe incluirla para una empresa cliente ni para la
cuenta demo, sin importar cómo esté configurado platform_modules/
company_modules — ver INTERNAL_ONLY_MODULE_IDS en app/api/routers/me_access.py.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import me_access

SUPERADMIN_ID = "11111111-1111-1111-1111-111111111111"
COMPANY_ADMIN_ID = "22222222-2222-2222-2222-222222222222"
DEMO_ID = "33333333-3333-3333-3333-333333333333"
COMPANY_ID = "company-1"
DEMO_COMPANY_ID = "demo-company"


def _fake_db():
    return {
        "users": [],
        "tables": {
            "platform_modules": [
                {"id": "dashboard", "name": "Dashboard", "is_active": True},
                {"id": "ml-training", "name": "Laboratorio de IA", "is_active": True},
            ],
            "admin_users": [
                {"user_id": SUPERADMIN_ID, "admin_role": "superadmin", "company_id": COMPANY_ID, "is_active": True},
                {"user_id": COMPANY_ADMIN_ID, "admin_role": "company_admin", "company_id": COMPANY_ID, "is_active": True},
                {"user_id": DEMO_ID, "admin_role": "company_admin", "company_id": DEMO_COMPANY_ID, "is_active": True},
            ],
            # Peor caso: alguien asignó "ml-training" a una empresa cliente por
            # error vía el panel de módulos — igual no debe verse.
            "company_modules": [
                {"company_id": COMPANY_ID, "module_id": "ml-training", "is_active": True},
            ],
        },
    }


@pytest.fixture()
def client(monkeypatch):
    fake_db = _fake_db()
    monkeypatch.setattr(me_access, "read_db", lambda *a, **k: fake_db)
    monkeypatch.setattr(me_access, "write_db", lambda db: None)
    app = FastAPI()
    app.include_router(me_access.router, prefix="/api")
    return TestClient(app)


def _as_user(monkeypatch, user_id, is_demo=False):
    monkeypatch.setattr(me_access, "bearer_user", lambda authorization: {"id": user_id, "email": f"{user_id}@example.com"})
    monkeypatch.setattr(me_access, "is_commercial_demo_user", lambda user: is_demo)


class TestMlTrainingIsInternalOnly:
    def test_superadmin_sees_ml_training(self, client, monkeypatch):
        _as_user(monkeypatch, SUPERADMIN_ID)
        resp = client.get("/api/me/access", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert "ml-training" in data["moduleIds"]
        assert any(m["id"] == "ml-training" for m in data["modules"])

    def test_company_admin_never_sees_ml_training_even_if_assigned(self, client, monkeypatch):
        _as_user(monkeypatch, COMPANY_ADMIN_ID)
        resp = client.get("/api/me/access", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert "ml-training" not in data["moduleIds"]
        assert not any(m["id"] == "ml-training" for m in data["modules"])

    def test_demo_never_sees_ml_training(self, client, monkeypatch):
        _as_user(monkeypatch, DEMO_ID, is_demo=True)
        resp = client.get("/api/me/access", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert "ml-training" not in data["moduleIds"]
        assert not any(m["id"] == "ml-training" for m in data["modules"])
        # El demo sí conserva el resto del catálogo completo (comportamiento
        # ya existente e intencional para el tour comercial).
        assert "dashboard" in data["moduleIds"]
