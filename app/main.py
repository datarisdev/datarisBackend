from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import settings

logger = logging.getLogger(__name__)
from app.api.routers import (
    admin_user,
    auth,
    compat,
    compat_extensions,
    dashboard,
    field_notes,
    graniot,
    me_access,
    parcel_crops,
    parcels,
    platform_modules,
    profiles,
    satellite_image,
    sentinel2,
    user_roles,
    users,
    weather,
)


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

# Include routers
fastapi_app.include_router(auth.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(users.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(profiles.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(user_roles.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(admin_user.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(platform_modules.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(parcels.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(satellite_image.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(field_notes.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(parcel_crops.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(weather.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(compat.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(compat_extensions.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(graniot.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(me_access.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(dashboard.router, prefix=settings.API_V1_STR)
fastapi_app.include_router(sentinel2.router, prefix=settings.API_V1_STR)


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
