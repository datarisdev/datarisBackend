from fastapi import FastAPI
from app.core.config import settings
from app.api.routers import auth, users, profiles, user_roles, admin_user, parcels, satellite_image, platform_modules, field_notes, parcel_crops, weather, compat
from app.models.base import Base
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title=settings.PROJECT_NAME)

# Allow frontend URL
origins = [origin.strip() for origin in getattr(settings, "BACKEND_CORS_ORIGINS", "*").split(",") if origin.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,       # domains allowed to access
    allow_credentials=True,      # needed for cookies/auth headers
    allow_methods=["*"],         # GET, POST, PUT, DELETE...
    allow_headers=["*"],         # headers allowed
)

# Include routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(users.router, prefix=settings.API_V1_STR)
app.include_router(profiles.router, prefix=settings.API_V1_STR)
app.include_router(user_roles.router, prefix=settings.API_V1_STR)
app.include_router(admin_user.router, prefix=settings.API_V1_STR)
app.include_router(platform_modules.router, prefix=settings.API_V1_STR)
app.include_router(parcels.router, prefix=settings.API_V1_STR)
app.include_router(satellite_image.router, prefix=settings.API_V1_STR)
app.include_router(field_notes.router, prefix=settings.API_V1_STR)
app.include_router(parcel_crops.router, prefix=settings.API_V1_STR)
app.include_router(weather.router, prefix=settings.API_V1_STR)
app.include_router(compat.router, prefix=settings.API_V1_STR)

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
