import pytest

from app.models.ml_training import TrainingJobStatus
from app.modules.ml_training.azure_ml_client import (
    AzureMLClientError,
    AzureMLJobSpec,
    azure_ml_disabled,
    submit_command_job,
    translate_azure_status,
)


class TestStatusTranslation:
    @pytest.mark.parametrize(
        "azure_status,expected",
        [
            ("Running", TrainingJobStatus.RUNNING),
            ("Completed", TrainingJobStatus.COMPLETED),
            ("Failed", TrainingJobStatus.FAILED),
            ("Canceled", TrainingJobStatus.CANCELLED),
            ("Queued", TrainingJobStatus.QUEUED),
            ("Provisioning", TrainingJobStatus.PROVISIONING_COMPUTE),
            ("Finalizing", TrainingJobStatus.FINALIZING),
            (None, TrainingJobStatus.QUEUED),
            ("SomeUnknownFutureStatus", TrainingJobStatus.RUNNING),
        ],
    )
    def test_translate(self, azure_status, expected):
        assert translate_azure_status(azure_status) == expected


class TestAzureMlDisabledByDefault:
    def test_disabled_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("AZURE_ML_ENABLED", raising=False)
        assert azure_ml_disabled() is True

    def test_enabled_when_env_true(self, monkeypatch):
        monkeypatch.setenv("AZURE_ML_ENABLED", "true")
        assert azure_ml_disabled() is False


class TestSubmitCommandJobGuard:
    def test_refuses_to_submit_when_disabled(self, monkeypatch):
        monkeypatch.delenv("AZURE_ML_ENABLED", raising=False)
        spec = AzureMLJobSpec(
            job_name="ml-test",
            display_name="test",
            command_line="python train.py",
            docker_image="acr.azurecr.io/ml-training:latest",
            compute_target="gpu-cluster",
            inputs={"dataset": "azureml://datastores/x/paths/y"},
            output_uri="azureml://datastores/x/paths/z",
            timeout_minutes=60,
        )
        with pytest.raises(AzureMLClientError, match="deshabilitado"):
            submit_command_job(spec)
