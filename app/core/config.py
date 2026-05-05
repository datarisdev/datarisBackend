from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings


def _default_sqlite_url() -> str:
    # Vercel/serverless cannot write reliably inside the deployment folder.
    # This fallback only prevents import crashes when DATABASE_URL is not set.
    # For real production data, configure DATABASE_URL in Vercel.
    tmp_dir = Path(os.getenv("TMPDIR", "/tmp"))
    return f"sqlite:///{tmp_dir / 'dataris_local.db'}"


class Settings(BaseSettings):
    PROJECT_NAME: str = "backend-dataris"
    API_V1_STR: str = "/api"

    # IMPORTANT: In production set this in Vercel Environment Variables.
    JWT_SECRET_KEY: str = "change_me_only_for_local_dev"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # IMPORTANT: In production set DATABASE_URL to a real Postgres URL.
    # The SQLite fallback exists so the app can boot and the /api/compat layer can work.
    DATABASE_URL: str = _default_sqlite_url()
    BACKEND_CORS_ORIGINS: str = "*"

    # Optional Google Cloud Storage config.
    GCS_BUCKET_NAME: str = "dataris-user-avatars"
    GCS_SERVICE_ACCOUNT_JSON: str | None = None
    GOOGLE_CLOUD_PROJECT: str | None = None

    DATARIS_COMPAT_STORAGE_DIR: str | None = None

    # Graniot API integration. Keep the API key only in backend env vars.
    GRANIOT_BASE_URL: str = "https://app.graniot.com"
    GRANIOT_API_KEY: str | None = None
    # The public ReDoc spec does not declare a security scheme; make it configurable.
    # Common valid values are: X-API-Key, Api-Key, Authorization.
    GRANIOT_AUTH_HEADER: str = "Authorization"
    GRANIOT_AUTH_SCHEME: str = "Api-Key"
    GRANIOT_CLIENT_ID: str | None = None
    # Optional: set this when Graniot requires every parcel to belong to a specific farm.
    # If empty, the backend will try to use the first farm returned by /api/farms/.
    GRANIOT_DEFAULT_FARM_ID: str | None = None
    GRANIOT_DEFAULT_FARM_NAME: str = "Dataris"
    GRANIOT_DEFAULT_FARM_TYPE: str = "PRO"
    GRANIOT_TIMEOUT_SECONDS: int = 60

    # Graniot debug logging. Values are redacted before writing logs.
    # Disable in production once the integration is stable.
    GRANIOT_DEBUG_LOGS_ENABLED: bool = True
    GRANIOT_DEBUG_LOG_TO_FILE: bool = True
    GRANIOT_DEBUG_LOG_FILE: str | None = None
    GRANIOT_DEBUG_MAX_BODY_CHARS: int = 30000

    # OpenAI Copiloto de Aplicación Aérea.
    # OPENAI_API_KEY debe configurarse en producción; si no existe, el backend devuelve
    # un diagnóstico determinístico local para que la UI siga funcionando.
    OPENAI_API_KEY: str | None = None
    OPENAI_AERIAL_COPILOT_MODEL: str = "gpt-4.1-mini"
    OPENAI_AERIAL_COPILOT_MAX_OUTPUT_TOKENS: int = 1400
    OPENAI_AERIAL_COPILOT_TIMEOUT_SECONDS: int = 25

    model_config = {
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()
