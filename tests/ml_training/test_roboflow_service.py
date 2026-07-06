import pytest

from app.modules.ml_training.roboflow_service import RoboflowService, RoboflowServiceError


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeHttpxClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, path, params=None):
        return self._responses.pop(0)


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ROBOFLOW_API_KEY", raising=False)
    with pytest.raises(RoboflowServiceError):
        RoboflowService(api_key=None)


def test_request_export_success_immediate(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    fake_client = FakeHttpxClient([FakeResponse(200, {"export": {"link": "https://example.com/x.zip", "size": 123, "progress": 1}})])
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    export = service.request_export("ws", "proj", "1", "yolov8")
    assert export.download_url == "https://example.com/x.zip"
    assert export.size_bytes == 123


def test_request_export_polls_until_ready(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    responses = [
        FakeResponse(200, {"export": {"link": None, "progress": 0.4}}),
        FakeResponse(200, {"export": {"link": "https://example.com/ready.zip", "progress": 1}}),
    ]
    fake_client = FakeHttpxClient(responses)
    monkeypatch.setattr(service, "_client", lambda: fake_client)
    monkeypatch.setattr("app.modules.ml_training.roboflow_service.time.sleep", lambda *_: None)

    export = service.request_export("ws", "proj", "1", "yolov8")
    assert export.download_url == "https://example.com/ready.zip"


def test_request_export_invalid_credentials(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    fake_client = FakeHttpxClient([FakeResponse(401, {})])
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    with pytest.raises(RoboflowServiceError, match="inválidas"):
        service.request_export("ws", "proj", "1", "yolov8")


def test_request_export_not_found(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    fake_client = FakeHttpxClient([FakeResponse(404, {})])
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    with pytest.raises(RoboflowServiceError, match="no encontrados"):
        service.request_export("ws", "proj", "1", "yolov8")


def test_request_export_unsupported_format(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    with pytest.raises(RoboflowServiceError, match="no soportado"):
        service.request_export("ws", "proj", "1", "not-a-format")


def test_request_export_rejects_path_injection(monkeypatch):
    service = RoboflowService(api_key="fake-key")
    with pytest.raises(RoboflowServiceError):
        service.request_export("ws/../evil", "proj", "1", "yolov8")
