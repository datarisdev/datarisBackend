from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from fastapi import APIRouter, FastAPI

from app.api.routers import (
    admin_user,
    analysis_history,
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
from app.core.config import settings


class RouterRegistration(NamedTuple):
    router: APIRouter
    prefix: str


API_ROUTERS: tuple[RouterRegistration, ...] = (
    RouterRegistration(auth.router, settings.API_V1_STR),
    RouterRegistration(users.router, settings.API_V1_STR),
    RouterRegistration(profiles.router, settings.API_V1_STR),
    RouterRegistration(user_roles.router, settings.API_V1_STR),
    RouterRegistration(admin_user.router, settings.API_V1_STR),
    RouterRegistration(analysis_history.router, settings.API_V1_STR),
    RouterRegistration(analysis_history.legacy_router, settings.API_V1_STR),
    RouterRegistration(platform_modules.router, settings.API_V1_STR),
    RouterRegistration(parcels.router, settings.API_V1_STR),
    RouterRegistration(satellite_image.router, settings.API_V1_STR),
    RouterRegistration(field_notes.router, settings.API_V1_STR),
    RouterRegistration(parcel_crops.router, settings.API_V1_STR),
    RouterRegistration(weather.router, settings.API_V1_STR),
    RouterRegistration(compat.router, settings.API_V1_STR),
    RouterRegistration(compat_extensions.router, settings.API_V1_STR),
    RouterRegistration(graniot.router, settings.API_V1_STR),
    RouterRegistration(me_access.router, settings.API_V1_STR),
    RouterRegistration(dashboard.router, settings.API_V1_STR),
    RouterRegistration(sentinel2.router, settings.API_V1_STR),
)


def include_api_routers(app: FastAPI, registrations: Iterable[RouterRegistration] = API_ROUTERS) -> None:
    for registration in registrations:
        app.include_router(registration.router, prefix=registration.prefix)
