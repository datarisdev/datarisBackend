import httpx
import pytest

from app.models.ml_training import TrainingJobStatus
from app.modules.ml_training.training_job_client import (
    TrainingJobClientError,
    TrainingJobSpec,
    build_blob_io_env,
    cancel_job,
    azure_ml_disabled,
    get_job,
    submit_command_job,
    translate_azure_status,
)


class TestStatusTranslation:
    @pytest.mark.parametrize(
        "azure_status,expected",
        [
            ("Running", TrainingJobStatus.RUNNING),
            ("Processing", TrainingJobStatus.PROVISIONING_COMPUTE),
            ("Succeeded", TrainingJobStatus.COMPLETED),
            ("Failed", TrainingJobStatus.FAILED),
            ("Stopped", TrainingJobStatus.CANCELLED),
            (None, TrainingJobStatus.QUEUED),
            ("SomeUnknownFutureStatus", TrainingJobStatus.RUNNING),
        ],
    )
    def test_translate(self, azure_status, expected):
        assert translate_azure_status(azure_status) == expected


class TestTrainingJobDisabledByDefault:
    def test_disabled_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("TRAINING_JOB_ENABLED", raising=False)
        assert azure_ml_disabled() is True

    def test_enabled_when_env_true(self, monkeypatch):
        monkeypatch.setenv("TRAINING_JOB_ENABLED", "true")
        assert azure_ml_disabled() is False


def _spec() -> TrainingJobSpec:
    return TrainingJobSpec(
        job_name="ml-test",
        command=["python", "train.py"],
        args=["--dataset-path", "/mnt/dataset"],
        environment_variables={"FOO": "bar"},
    )


class TestSubmitCommandJobGuard:
    def test_refuses_to_submit_when_disabled(self, monkeypatch):
        monkeypatch.delenv("TRAINING_JOB_ENABLED", raising=False)
        with pytest.raises(TrainingJobClientError, match="deshabilitado"):
            submit_command_job(_spec())


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _training_job_env(monkeypatch):
    monkeypatch.setenv("TRAINING_JOB_ENABLED", "true")
    monkeypatch.setenv("TRAINING_JOB_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("TRAINING_JOB_RESOURCE_GROUP", "rg-1")
    monkeypatch.setenv("TRAINING_JOB_NAME", "job-1")
    monkeypatch.setenv("TRAINING_JOB_IMAGE", "acr.azurecr.io/dataris-ml-training:test")
    monkeypatch.setattr(
        "app.modules.ml_training.training_job_client._auth_headers",
        lambda: {"Authorization": "Bearer fake"},
    )


class TestSubmitCommandJob:
    def test_wraps_real_command_with_entrypoint(self, monkeypatch):
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse(200, {"name": "job-1-abc123"})

        monkeypatch.setattr(httpx, "request", fake_request)

        execution_name = submit_command_job(_spec())

        assert execution_name == "job-1-abc123"
        assert captured["method"] == "POST"
        assert "/jobs/job-1/start" in captured["url"]
        container = captured["json"]["containers"][0]
        assert container["image"] == "acr.azurecr.io/dataris-ml-training:test"
        assert container["command"] == ["python", "entrypoint.py"]
        assert container["args"] == ["--", "python", "train.py", "--dataset-path", "/mnt/dataset"]
        assert {"name": "FOO", "value": "bar"} in container["env"]
        assert container["resources"] == {"cpu": 2.0, "memory": "4Gi"}

    def test_raises_when_image_not_configured(self, monkeypatch):
        monkeypatch.delenv("TRAINING_JOB_IMAGE", raising=False)
        with pytest.raises(TrainingJobClientError, match="TRAINING_JOB_IMAGE"):
            submit_command_job(_spec())

    def test_uses_custom_resources_from_env(self, monkeypatch):
        monkeypatch.setenv("TRAINING_JOB_CPU", "1.5")
        monkeypatch.setenv("TRAINING_JOB_MEMORY", "3Gi")
        captured = {}
        monkeypatch.setattr(
            httpx,
            "request",
            lambda method, url, headers=None, json=None, timeout=None: (
                captured.update(json=json) or _FakeResponse(200, {"name": "job-1-abc123"})
            ),
        )
        submit_command_job(_spec())
        assert captured["json"]["containers"][0]["resources"] == {"cpu": 1.5, "memory": "3Gi"}

    def test_raises_when_azure_returns_error(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "request", lambda *a, **k: _FakeResponse(403, {"error": "Forbidden"})
        )
        with pytest.raises(TrainingJobClientError, match="403"):
            submit_command_job(_spec())

    def test_raises_when_azure_omits_execution_name(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(200, {}))
        with pytest.raises(TrainingJobClientError, match="nombre de la execution"):
            submit_command_job(_spec())


class TestGetJob:
    def test_parses_execution_status(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "request",
            lambda *a, **k: _FakeResponse(
                200,
                {
                    "name": "job-1-abc123",
                    "id": "/subscriptions/sub-1/.../executions/job-1-abc123",
                    "properties": {"status": "Running"},
                },
            ),
        )
        info = get_job("job-1-abc123")
        assert info["status"] == "Running"
        assert info["internal_status"] == TrainingJobStatus.RUNNING
        assert info["studio_url"] == "https://portal.azure.com/#@/resource/subscriptions/sub-1/.../executions/job-1-abc123"


class TestCancelJob:
    def test_stop_call_succeeds(self, monkeypatch):
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None):
            captured["url"] = url
            return _FakeResponse(200, {})

        monkeypatch.setattr(httpx, "request", fake_request)
        cancel_job("job-1-abc123")
        assert "/stop/job-1-abc123" in captured["url"]

    def test_409_on_already_finished_execution_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(409, {"error": "Conflict"}))
        cancel_job("job-1-abc123")  # no debe lanzar

    def test_other_errors_propagate(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(500, {"error": "boom"}))
        with pytest.raises(TrainingJobClientError):
            cancel_job("job-1-abc123")


class TestBuildBlobIoEnv:
    def test_includes_inputs_and_output_prefix(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://acct.blob.core.windows.net")
        monkeypatch.setenv("TRAINING_JOB_IDENTITY_CLIENT_ID", "client-id-1")
        monkeypatch.delenv("AZURE_ML_TRAINING_STORAGE_CONTAINER", raising=False)
        monkeypatch.setenv("AZURE_STORAGE_CONTAINER_NAME", "assets")

        env = build_blob_io_env(inputs={"dataset": "ml/datasets/u1/d1/raw/"}, output_prefix="ml/jobs/u1/j1/")

        assert env["AZURE_STORAGE_ACCOUNT_URL"] == "https://acct.blob.core.windows.net"
        assert env["AZURE_CLIENT_ID"] == "client-id-1"
        assert env["BLOB_OUTPUT_PREFIX"] == "ml/jobs/u1/j1/"
        assert '"dataset"' in env["BLOB_INPUTS_JSON"]
        assert env["BLOB_CONTAINER_NAME"] == "assets"
