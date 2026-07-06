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
    # Optional regex. Useful in Cloud Run when the frontend domain changes or
    # when BACKEND_CORS_ORIGINS is left as "*" with credentials enabled.
    BACKEND_CORS_ORIGIN_REGEX: str | None = None

    # Optional Google Cloud Storage config.
    GOOGLE_CLOUD_PROJECT: str | None = None

    DATARIS_COMPAT_STORAGE_DIR: str | None = None

    # Graniot API integration. Keep the API key only in backend env vars.
    GRANIOT_BASE_URL: str = "https://app.graniot.com"
    GRANIOT_API_KEY: str | None = None
    # The public ReDoc spec does not declare a security scheme; make it configurable.
    # Common valid values are: X-API-Key, Api-Key, Authorization.
    GRANIOT_AUTH_HEADER: str = "X-API-Key"
    GRANIOT_AUTH_SCHEME: str = ""
    GRANIOT_CLIENT_ID: str | None = None
    # Optional: set this when Graniot requires every parcel to belong to a specific farm.
    # If empty, the backend will try to use the first farm returned by /api/farms/.
    GRANIOT_DEFAULT_FARM_ID: str | None = None
    GRANIOT_DEFAULT_FARM_NAME: str = "Dataris"
    GRANIOT_DEFAULT_FARM_TYPE: str = "PRO"
    GRANIOT_TIMEOUT_SECONDS: int = 60

    # Graniot debug logging. Values are redacted before writing logs.
    # Disable in production once the integration is stable.
    GRANIOT_DEBUG_LOGS_ENABLED: bool = False
    GRANIOT_DEBUG_LOG_TO_FILE: bool = True
    GRANIOT_DEBUG_LOG_FILE: str | None = None
    GRANIOT_DEBUG_MAX_BODY_CHARS: int = 30000
    # These diagnostics are intentionally separate from full debug logs:
    # they only print important WMS/Graniot failures to Cloud Run stdout so a
    # browser 502 can be traced without enabling noisy file logs.
    GRANIOT_DEBUG_IMPORTANT_LOGS_TO_STDOUT: bool = True
    GRANIOT_WMS_DIAGNOSTIC_LOGS_ENABLED: bool = True
    # /api/wms/ is public in the Graniot OpenAPI. Keep auth fallback disabled
    # by default to avoid duplicate WMS requests/log spam. Enable only if a
    # private Graniot deployment requires auth for WMS images.
    GRANIOT_WMS_TRY_AUTH_FALLBACK: bool = False
    # Keep uppercase OGC WMS attempts disabled for Graniot /api/wms/. That
    # endpoint expects lowercase `layers`; uppercase `LAYERS` only creates
    # noisy 400 responses like {"layers": ["This field is required."]}.
    GRANIOT_WMS_TRY_STANDARD_FALLBACK: bool = False


    # DigiformsApp integration. Global provider URLs remain in environment.
    # Company credentials are stored encrypted per tenant from Extensiones → DigiForms.
    DIGIFORMS_BASE_URL: str = "https://d.interlinksoft.net/Digiforms/Api/user"
    DIGIFORMS_CLIENT_ID: str = "178"
    DIGIFORMS_API_USER: str = "api"
    DIGIFORMS_API_PASSWORD: str | None = None
    DIGIFORMS_AUTH_TIMEOUT_SECONDS: int = 30
    DIGIFORMS_PORTAL_URL: str = "https://d.interlinksoft.net/Digiforms/"
    DIGIFORMS_DEFAULT_PROFILE: str = "user"
    DIGIFORMS_PROVISIONING_ENABLED: bool = True

    # Official DigiForms REST/JSON Data API. This is independent from User API.
    # The API returns form submissions incrementally by ResponseId and image links.
    DIGIFORMS_DATA_BASE_URL: str = "https://d.interlinksoft.net/DigiformsData/api"
    DIGIFORMS_DATA_API_USER: str | None = None
    DIGIFORMS_DATA_API_PASSWORD: str | None = None
    DIGIFORMS_DATA_TIMEOUT_SECONDS: int = 45
    DIGIFORMS_HARVEST_FORM_ID: str | None = None
    DIGIFORMS_PEST_WEED_FORM_ID: str | None = None
    DIGIFORMS_SYNC_INITIAL_RESPONSE_ID: int = 0
    DIGIFORMS_SYNC_CRON_SECRET: str | None = None
    # Global encryption key used to store tenant-specific DigiForms credentials.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    DIGIFORMS_CREDENTIALS_ENCRYPTION_KEY: str | None = None
    # Optional: limit scheduled synchronization to one Dataris account. Useful
    # when a deployment has multiple users but a single DigiForms client.
    DIGIFORMS_SYNC_TARGET_USER_ID: str | None = None

    # OpenAI Copiloto de Aplicación Aérea.
    # OPENAI_API_KEY debe configurarse en producción; si no existe, el backend devuelve
    # un diagnóstico determinístico local para que la UI siga funcionando.
    OPENAI_API_KEY: str | None = None
    OPENAI_AERIAL_COPILOT_MODEL: str = "gpt-4.1-mini"
    OPENAI_AERIAL_COPILOT_MAX_OUTPUT_TOKENS: int = 1400
    OPENAI_AERIAL_COPILOT_TIMEOUT_SECONDS: int = 25

    # Copiloto contextual global. Analiza la ventana que el usuario está viendo
    # con texto visible, filtros, tablas y un resumen estructurado sin geometrías crudas.
    OPENAI_CONTEXTUAL_COPILOT_MODEL: str = "gpt-4o-mini"
    OPENAI_CONTEXTUAL_COPILOT_MAX_OUTPUT_TOKENS: int = 1400
    OPENAI_CONTEXTUAL_COPILOT_TIMEOUT_SECONDS: int = 35

    # Módulo de entrenamiento de modelos de visión por computadora (Laboratorio
    # de IA). ML_TRAINING_ENABLED controla si el módulo se expone en la API;
    # AZURE_ML_ENABLED (leído directamente por azure_ml_client.py, igual
    # patrón que AZURE_STORAGE_* en azure_blob.py) controla si se permite
    # enviar jobs reales a Azure ML. Ambos en false por defecto.
    ML_TRAINING_ENABLED: bool = False
    ML_TRAINING_DEFAULT_JOB_TIMEOUT_MINUTES: int = 120
    ML_TRAINING_DEFAULT_MAX_CONCURRENT_JOBS: int = 1
    ML_TRAINING_DEFAULT_MAX_DATASET_SIZE_GB: float = 5.0

    model_config = {
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()
