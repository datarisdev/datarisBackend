from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.router_registry import include_api_routers
from app.core.config import settings

logger = logging.getLogger(__name__)

# Executor dedicado al pre-calentamiento del caché demo. Mantiene el pool
# separado para no bloquear workers de request normales durante el arranque.
_demo_prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="demo-prefetch")

# Índices más usados en el demo, ordenados de mayor a menor prioridad.
_DEMO_PRIORITY_INDICES = [
    "NDVI", "EVI", "GNDVI", "SAVI",         # 10 m, bandas VIS/NIR
    "NDRE", "CI_REDEDGE", "NDMI", "NBR",    # 20 m, Red Edge / SWIR
    "NDWI", "MSAVI2", "OSAVI", "TRUE_COLOR", # resto
]


def _prefetch_demo_index(index_key: str) -> None:
    """Genera y cachea en GCS la capa demo para un índice.

    Si el resultado ya existe en GCS (caché persistente), la llamada a
    generate_or_get_layer() termina en < 1 s sin re-procesar nada.
    """
    from app.api.routers.sentinel2 import _DEMO_PARCEL_GEOMETRY, _DEMO_USER_ID, _DEMO_PARCEL_ID
    from app.services.sentinel2.service import generate_or_get_layer
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        result = generate_or_get_layer(
            db,
            parcel_geometry=_DEMO_PARCEL_GEOMETRY,
            user_id=_DEMO_USER_ID,
            parcel_id=_DEMO_PARCEL_ID,
            index_key=index_key,
            target_date=None,
            max_cloud=80.0,
            width=2048,
            height=2048,
        )
        if result.get("available"):
            logger.info("Demo Sentinel-2 caché listo: %s (fecha=%s)", index_key, result.get("date"))
        else:
            logger.warning("Demo Sentinel-2 sin datos: %s — %s", index_key, result.get("reason"))
    except Exception as exc:
        logger.warning("Demo Sentinel-2 pre-caché falló en %s: %s", index_key, exc)
    finally:
        db.close()


async def _run_demo_prefetch() -> None:
    """Envía todos los índices al executor en cascada con un pequeño delay
    entre cada uno para no saturar el catálogo STAC ni los COG de S3 al inicio.
    """
    loop = asyncio.get_event_loop()
    for i, index_key in enumerate(_DEMO_PRIORITY_INDICES):
        if i > 0:
            await asyncio.sleep(3)  # escalonar para no saturar STAC/S3
        loop.run_in_executor(_demo_prefetch_executor, _prefetch_demo_index, index_key)


def _parse_cors_origins(raw: str | None) -> tuple[list[str], str | None]:
    """Return Starlette-compatible CORS origins.

    When credentials/auth headers are used, relying on allow_origins=["*"] can
    produce missing CORS headers on some error/preflight paths. Using a regex for
    the wildcard makes Starlette echo the request Origin instead, which is safer
    for authenticated API calls from app.dataris.es, staging domains, etc.
    """
    configured = (raw or "*").strip()
    explicit_regex = getattr(settings, "BACKEND_CORS_ORIGIN_REGEX", None)
    if explicit_regex:
        return [], explicit_regex
    if configured in {"", "*"}:
        return [], r"https?://.*"
    origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    return origins, None


cors_origins, cors_origin_regex = _parse_cors_origins(getattr(settings, "BACKEND_CORS_ORIGINS", "*"))

fastapi_app = FastAPI(title=settings.PROJECT_NAME)

# GeoJSON puede ser pesado. GZip reduce mucho el tiempo de transferencia al frontend.
fastapi_app.add_middleware(GZipMiddleware, minimum_size=1000)

include_api_routers(fastapi_app)


@fastapi_app.on_event("startup")
async def startup_create_tables() -> None:
    """Crea tablas nuevas que no existan todavía (idempotente gracias a checkfirst=True)."""
    from app.models.base import Base
    from app.db.session import engine
    import app.models  # noqa: registra todos los modelos incluyendo harvest
    Base.metadata.create_all(bind=engine, checkfirst=True)


@fastapi_app.on_event("startup")
async def startup_demo_prefetch() -> None:
    """Al arrancar el servidor lanza el pre-calentamiento del caché demo en background.

    Si GCS ya tiene los PNG generados la tarea termina en segundos sin re-procesar.
    Si es la primera vez (instancia nueva / bucket vacío) los genera silenciosamente
    sin bloquear ningún request de usuario.
    """
    asyncio.create_task(_run_demo_prefetch())


@fastapi_app.get("/health")
def health():
    return {"status": "ok"}


@fastapi_app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API error on %s", request.url.path)
    detail = "Error interno del servidor"
    if request.url.path.startswith(f"{settings.API_V1_STR}/satellite-free"):
        detail = str(exc) or exc.__class__.__name__
    return JSONResponse(status_code=500, content={"detail": detail})


# Global CORS enforcement: wrapping the entire ASGI app ensures CORS headers are
# still present on 4xx/5xx responses and on OPTIONS preflight requests.
app = CORSMiddleware(
    fastapi_app,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)
