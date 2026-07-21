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
    # Account whose dedicated Graniot portal is embedded in the staging
    # satellite module. The URL itself is resolved at runtime; never store its
    # auth_id in source control or frontend build variables.
    GRANIOT_EMBED_ACCOUNT_EMAIL: str = "gmateo@ingeoproyectos.com"
    GRANIOT_EMBED_URL: str | None = None
    # The embedded portal (embed.graniot.com) authenticates with a SimpleJWT
    # *access* token passed as ?auth_id=. That token lives ~3h and Graniot does
    # NOT refresh the one exposed by /api/accounts/ (it goes stale, leaving the
    # iframe stuck on "loading"). To always serve a fresh token, the backend
    # mints one on demand against the embed host's token endpoints. Provide
    # EITHER a long-lived refresh token OR the embed account's credentials
    # (ideally a dedicated Graniot service user). Keep these in backend secrets.
    GRANIOT_EMBED_HOST: str = "embed.graniot.com"
    GRANIOT_EMBED_USERNAME: str | None = None
    GRANIOT_EMBED_PASSWORD: str | None = None
    GRANIOT_EMBED_REFRESH_TOKEN: str | None = None
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

    # Azure OpenAI para todos los copilotos. En Azure se usa Managed Identity por
    # defecto; AZURE_OPENAI_API_KEY solo existe como alternativa de compatibilidad.
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_DEPLOYMENT: str | None = None
    AZURE_OPENAI_CONTEXTUAL_DEPLOYMENT: str | None = None
    AZURE_OPENAI_AERIAL_DEPLOYMENT: str | None = None
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_TOKEN_SCOPE: str = "https://ai.azure.com/.default"
    AZURE_OPENAI_IMAGE_DETAIL: str = "original"

    # OpenAI público queda únicamente como fallback de desarrollo. Si ningún
    # proveedor está configurado, el backend mantiene el diagnóstico local.
    OPENAI_API_KEY: str | None = None
    OPENAI_AERIAL_COPILOT_MODEL: str = "gpt-4.1-mini"
    OPENAI_AERIAL_COPILOT_MAX_OUTPUT_TOKENS: int = 2400
    OPENAI_AERIAL_COPILOT_TIMEOUT_SECONDS: int = 60

    # Copiloto contextual global. Analiza la ventana que el usuario está viendo
    # con texto visible, filtros, tablas y un resumen estructurado sin geometrías crudas.
    OPENAI_CONTEXTUAL_COPILOT_MODEL: str = "gpt-4.1"
    OPENAI_CONTEXTUAL_COPILOT_MAX_OUTPUT_TOKENS: int = 3200
    OPENAI_CONTEXTUAL_COPILOT_TIMEOUT_SECONDS: int = 75

    # Módulo de entrenamiento de modelos de visión por computadora (Laboratorio
    # de IA). ML_TRAINING_ENABLED controla si el módulo se expone en la API;
    # TRAINING_JOB_ENABLED (leído directamente por training_job_client.py,
    # igual patrón que AZURE_STORAGE_* en azure_blob.py) controla si se
    # permite enviar jobs reales al Container App Job de entrenamiento. Ambos
    # en false por defecto.
    ML_TRAINING_ENABLED: bool = False
    ML_TRAINING_DEFAULT_JOB_TIMEOUT_MINUTES: int = 120
    ML_TRAINING_DEFAULT_MAX_CONCURRENT_JOBS: int = 1
    ML_TRAINING_DEFAULT_MAX_DATASET_SIZE_GB: float = 5.0

    # EOS Data Analytics (EOSDA) API Connect. Keep the API key only in backend
    # env vars / Azure Container App secrets — never in the frontend bundle.
    EOS_BASE_URL: str = "https://api-connect.eos.com/api"
    EOS_API_KEY: str | None = None
    # Default satellite dataset used for search/render/statistics.
    EOS_DATASET: str = "sentinel2"
    EOS_TIMEOUT_SECONDS: int = 60
    # Avoid downloading useless scenes.
    EOS_MAX_CLOUD: float = 80.0
    # How far back to look for scenes when no date range is provided (days).
    EOS_SEARCH_DAYS_BACK: int = 365
    # mt_stats is asynchronous (create -> poll). Bound the synchronous polling.
    EOS_STATS_POLL_SECONDS: float = 3.0
    EOS_STATS_MAX_POLLS: int = 8
    # Map-layer rendering: stitch XYZ render tiles over the parcel bbox.
    EOS_RENDER_MAX_TILES: int = 80
    EOS_RENDER_TARGET_PX: int = 1024
    # EOS limita el endpoint de render a ~10 solicitudes/minuto compartidas por
    # toda la app. Una capa se arma pegando varios tiles, así que el número de
    # tiles por render debe caber holgadamente en ese cupo o el render tarda
    # minutos y termina en 429 ("proveedor saturado"). Este presupuesto es el
    # tope efectivo de tiles por capa: para un lote grande se elige un zoom más
    # bajo (menos detalle) de modo que TODO el lote quepa en estos tiles y
    # cargue en segundos, en vez de intentar decenas de tiles y saturar la API.
    # EOS_RENDER_MAX_TILES sigue siendo el techo absoluto de seguridad.
    EOS_RENDER_TILE_BUDGET: int = 9
    # Resolución nativa aproximada de Sentinel-2 (~10 m/píxel). Solo por encima
    # de este umbral la vista se considera "reducida" (lote grande) y se avisa al
    # usuario; por debajo el detalle es full aunque se limiten los tiles.
    EOS_RENDER_NATIVE_M_PER_PX: float = 12.0
    # Zoom máximo que soporta el endpoint de render de EOS (Sentinel-2). Pedir un
    # zoom mayor devuelve HTTP 422 "Max zoom exceed"; como el fallo de todos los
    # tiles se acaba enmascarando como "proveedor saturado", este tope evita que
    # los lotes PEQUEÑOS (que de otro modo pedirían z17/z18) fallen siempre.
    # Verificado empíricamente contra la API: z16 responde 200, z17 da 422.
    EOS_RENDER_MAX_ZOOM: int = 16
    # EOS limita el render a ~10 req/min compartidas por toda la app. Cachear
    # el PNG final en memoria de proceso evita repetir tiles cuando varias
    # pantallas (Satélite, Comparación, Gráficos, Zonificación) piden la misma
    # escena/índice/lote casi al mismo tiempo.
    EOS_RENDER_CACHE_TTL_SECONDS: int = 6 * 60 * 60
    # Caché persistente en Azure Blob (L2) del PNG ya renderizado: la caché en
    # memoria (L1) se pierde al reiniciar/escalar y no se comparte entre réplicas
    # ni usuarios. Con el blob, el segundo click de CUALQUIER usuario sobre el
    # mismo lote/índice/fecha es casi instantáneo (una descarga en vez de buscar
    # escenas + bajar tiles a EOS). Best-effort: si el blob no está disponible
    # cae a solo memoria.
    EOS_RENDER_BLOB_CACHE_ENABLED: bool = True
    # Si la mejor escena no se puede renderizar (p. ej. sus tiles aún no están
    # procesados), se prueba con la siguiente. Se acota cuántas escenas se
    # intentan: cada intento consume tiles del cupo de render, así que probar
    # las 30 escenas ante un fallo por rate-limit solo empeoraría la saturación.
    EOS_RENDER_SCENE_FALLBACK_LIMIT: int = 3
    # Mosaico multi-escena/multi-fecha: cuántas escenas candidatas (ordenadas de
    # mejor a peor: fecha más cercana/reciente y menos nubes) se pasan al render.
    # Cada tile toma la PRIMERA escena con datos, de modo que un lote que cruza el
    # borde de dos granules o cuya mejor fecha solo cubre una parte se rellene
    # COMPLETO con las demás. La escena principal domina; las otras solo rellenan
    # huecos (no cuestan peticiones extra en los tiles ya cubiertos). Se acota
    # para no disparar el límite ~10 req/min de EOS en el primer render.
    EOS_MOSAIC_MAX_SCENES: int = 10

    model_config = {
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()
