import uuid
from datetime import datetime, timezone

import pytest

from app.models.ml_training import DatasetStatus, MLDataset, TrainingJob, TrainingJobStatus
from app.models.user_roles import AppRole, UserRole
from app.modules.ml_training import service


def _grant_manage_role(db_session, user_id: str):
    db_session.add(UserRole(user_id=user_id, role=AppRole.supervisor_campo))
    db_session.commit()


class TestProjectIsolation:
    def test_visualizador_cannot_create_project(self, api_client, current_user_holder, user_a_id):
        current_user_holder.user_id = user_a_id
        resp = api_client.post("/api/ml/projects", json={"name": "P1", "task_type": "detection"})
        assert resp.status_code == 403

    def test_owner_can_create_and_list_own_project(self, api_client, current_user_holder, db_session, user_a_id):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id

        resp = api_client.post("/api/ml/projects", json={"name": "P1", "task_type": "detection"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]

        resp = api_client.get("/api/ml/projects")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == project_id

    def test_other_user_cannot_see_or_fetch_project(self, api_client, current_user_holder, db_session, user_a_id, user_b_id):
        _grant_manage_role(db_session, user_a_id)
        _grant_manage_role(db_session, user_b_id)

        current_user_holder.user_id = user_a_id
        resp = api_client.post("/api/ml/projects", json={"name": "Owned by A", "task_type": "detection"})
        project_id = resp.json()["id"]

        current_user_holder.user_id = user_b_id
        resp = api_client.get("/api/ml/projects")
        assert resp.json() == []

        resp = api_client.get(f"/api/ml/projects/{project_id}")
        assert resp.status_code == 404


class TestDatasetUploadIntent:
    def test_upload_intent_generates_scoped_blob_path(self, api_client, current_user_holder, db_session, user_a_id, monkeypatch):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id

        monkeypatch.setattr(
            "app.modules.ml_training.upload_service.generate_blob_write_url",
            lambda **kwargs: "https://fake.blob.core.windows.net/fake-sas",
        )
        monkeypatch.setattr("app.modules.ml_training.upload_service.ml_training_container_name", lambda: "ml-training")

        resp = api_client.post(
            "/api/ml/datasets/upload-intent",
            json={
                "name": "Mi dataset",
                "task_type": "detection",
                "file_name": "dataset.zip",
                "content_type": "application/zip",
                "size_bytes": 1024,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["blob_path"].startswith(f"ml/datasets/{user_a_id}/")
        assert body["upload_url"] == "https://fake.blob.core.windows.net/fake-sas"

    def test_upload_intent_rejects_path_traversal_filename(self, api_client, current_user_holder, db_session, user_a_id):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id
        resp = api_client.post(
            "/api/ml/datasets/upload-intent",
            json={
                "name": "Mi dataset",
                "task_type": "detection",
                "file_name": "../../etc/passwd",
                "size_bytes": 1024,
            },
        )
        assert resp.status_code == 422

    def test_upload_intent_rejects_oversized_file(self, api_client, current_user_holder, db_session, user_a_id):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id
        resp = api_client.post(
            "/api/ml/datasets/upload-intent",
            json={
                "name": "Mi dataset",
                "task_type": "detection",
                "file_name": "dataset.zip",
                "size_bytes": 999_999_999_999,
            },
        )
        assert resp.status_code == 400


class TestTrainingJobCreation:
    def test_requires_explicit_confirmation(self, api_client, current_user_holder, db_session, user_a_id):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id
        resp = api_client.post(
            "/api/ml/jobs",
            json={
                "project_id": str(uuid.uuid4()),
                "dataset_id": str(uuid.uuid4()),
                "recipe": "ultralytics_yolo_detection",
                "model_base": "yolo11n.pt",
                "model_name": "modelo-1",
                "confirm": False,
            },
        )
        assert resp.status_code == 422

    def test_blocks_job_when_dataset_not_ready(self, api_client, current_user_holder, db_session, user_a_id):
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id

        resp = api_client.post("/api/ml/projects", json={"name": "P1", "task_type": "detection"})
        project_id = resp.json()["id"]

        dataset = MLDataset(
            id=uuid.uuid4(),
            user_id=user_a_id,
            project_id=uuid.UUID(project_id),
            name="ds",
            source="upload",
            status=DatasetStatus.UPLOADING,
            storage_prefix="ml/datasets/x/raw/x.zip",
            task_type="detection",
        )
        db_session.add(dataset)
        db_session.commit()

        resp = api_client.post(
            "/api/ml/jobs",
            json={
                "project_id": project_id,
                "dataset_id": str(dataset.id),
                "recipe": "ultralytics_yolo_detection",
                "model_base": "yolo11n.pt",
                "model_name": "modelo-1",
                "confirm": True,
            },
        )
        assert resp.status_code == 400
        assert "validado" in resp.json()["detail"]

    def test_job_creation_fails_cleanly_when_azure_ml_disabled(self, api_client, current_user_holder, db_session, user_a_id, monkeypatch):
        monkeypatch.delenv("AZURE_ML_ENABLED", raising=False)
        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id

        resp = api_client.post("/api/ml/projects", json={"name": "P1", "task_type": "detection"})
        project_id = resp.json()["id"]

        dataset = MLDataset(
            id=uuid.uuid4(),
            user_id=user_a_id,
            project_id=uuid.UUID(project_id),
            name="ds",
            source="upload",
            status=DatasetStatus.READY,
            storage_prefix="ml/datasets/x/raw/x.zip",
            task_type="detection",
            class_names=["weed"],
            class_count=1,
            image_count=20,
        )
        db_session.add(dataset)
        db_session.commit()

        resp = api_client.post(
            "/api/ml/jobs",
            json={
                "project_id": project_id,
                "dataset_id": str(dataset.id),
                "recipe": "ultralytics_yolo_detection",
                "model_base": "yolo11n.pt",
                "model_name": "modelo-1",
                "confirm": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # El job se registra igualmente, con el motivo exacto por el que no
        # se pudo enviar a Azure ML (módulo deshabilitado por defecto).
        assert body["status"] == "failed"
        assert body["error_code"] == "azure_ml_unavailable"


class TestJobCancellation:
    def test_cannot_cancel_already_finished_job(self, db_session, user_a_id):
        job = TrainingJob(
            id=uuid.uuid4(),
            user_id=user_a_id,
            project_id=uuid.uuid4(),
            dataset_id=uuid.uuid4(),
            recipe="ultralytics_yolo_detection",
            task_type="detection",
            model_base="yolo11n.pt",
            model_name="m1",
            config={},
            status=TrainingJobStatus.COMPLETED,
            output_storage_prefix="ml/jobs/x/y/z/",
            finished_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()

        with pytest.raises(service.MLTrainingError, match="finalizó"):
            service.cancel_training_job(db_session, user_a_id, job.id, is_admin=False)

    def test_owner_can_cancel_active_job(self, db_session, user_a_id, monkeypatch):
        job = TrainingJob(
            id=uuid.uuid4(),
            user_id=user_a_id,
            project_id=uuid.uuid4(),
            dataset_id=uuid.uuid4(),
            recipe="ultralytics_yolo_detection",
            task_type="detection",
            model_base="yolo11n.pt",
            model_name="m1",
            config={},
            status=TrainingJobStatus.RUNNING,
            azure_ml_job_id="ml-fake-job",
            output_storage_prefix="ml/jobs/x/y/z/",
        )
        db_session.add(job)
        db_session.commit()

        monkeypatch.setattr("app.modules.ml_training.service.azure_cancel_job", lambda *_: None)

        result = service.cancel_training_job(db_session, user_a_id, job.id, is_admin=False)
        assert result.status == TrainingJobStatus.CANCELLED
        assert result.cancelled_at is not None

    def test_other_user_without_admin_cannot_cancel(self, db_session, user_a_id, user_b_id):
        job = TrainingJob(
            id=uuid.uuid4(),
            user_id=user_a_id,
            project_id=uuid.uuid4(),
            dataset_id=uuid.uuid4(),
            recipe="ultralytics_yolo_detection",
            task_type="detection",
            model_base="yolo11n.pt",
            model_name="m1",
            config={},
            status=TrainingJobStatus.RUNNING,
            output_storage_prefix="ml/jobs/x/y/z/",
        )
        db_session.add(job)
        db_session.commit()

        with pytest.raises(service.MLTrainingError, match="no encontrado"):
            service.cancel_training_job(db_session, user_b_id, job.id, is_admin=False)


class TestAuditLog:
    def test_project_creation_is_audited(self, api_client, current_user_holder, db_session, user_a_id):
        from app.models.ml_training import MLAuditLog

        _grant_manage_role(db_session, user_a_id)
        current_user_holder.user_id = user_a_id
        api_client.post("/api/ml/projects", json={"name": "P1", "task_type": "detection"})

        logs = db_session.query(MLAuditLog).filter(MLAuditLog.user_id == user_a_id).all()
        assert any(l.action == "project_created" for l in logs)
