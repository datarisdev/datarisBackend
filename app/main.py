from __future__ import annotations

import importlib
import logging
from typing import Iterable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings

logger = logging.getLogger("dataris.startup")

app = FastAPI(title=settings.PROJECT_NAME)


def _cors_origins() -> list[str]:
    raw = getattr(settings, "BACKEND_CORS_ORIGINS", "*") or "*"
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def include_router(module_name: str, *, prefix: str = "", required: bool = False) -> None:
    """Importa routers de forma segura.

    En Vercel no se deben cargar módulos pesados de satélite/raster al iniciar,
    porque exceden límites de tamaño o dependen de librerías nativas. Este helper
    permite que auth/compat arranquen aunque un módulo opcional no esté disponible.
    """
    try:
        module = importlib.import_module(f"app.api.routers.{module_name}")
        app.include_router(module.router, prefix=prefix)
        logger.info("Router loaded: %s prefix=%s", module_name, prefix or "/")
    except ModuleNotFoundError as exc:
        if required:
            raise
        logger.warning("Router skipped: %s because dependency is missing: %s", module_name, exc)
    except Exception as exc:
        if required:
            raise
        logger.warning("Router skipped: %s because it failed during import: %s", module_name, exc)


API_PREFIX = settings.API_V1_STR

# Routers esenciales y ligeros.
for _router in (
    "auth",
    "users",
    "profiles",
    "user_roles",
    "admin_user",
    "platform_modules",
    "parcels",
    "field_notes",
    "parcel_crops",
):
    include_router(_router, prefix=API_PREFIX, required=False)

# Compat es el router que usa el frontend migrado desde Supabase.
# Se publica con /api/compat y también /compat para tolerar ambas VITE_API_URL.
include_router("compat", prefix=API_PREFIX, required=True)
include_router("compat", prefix="", required=True)

# Routers opcionales: si falta rasterio/geopandas/celery/redis, el backend sigue vivo.
for _router in ("satellite_image", "weather"):
    include_router(_router, prefix=API_PREFIX, required=False)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"status": "ok", "service": settings.PROJECT_NAME}


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return {"status": "ok"}


@app.get("/favicon.png", include_in_schema=False)
def favicon_png():
    return {"status": "ok"}
