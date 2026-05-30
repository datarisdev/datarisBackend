from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.modules.work_area.service import build_work_area_layers_response

router = APIRouter(prefix="/work-area", tags=["Zona de trabajo"])
legacy_router = APIRouter(prefix="/analysis-history", tags=["Zona de trabajo"])


@router.get("/layers")
@legacy_router.get("/layers", include_in_schema=False)
def work_area_layers(
    sources: Optional[str] = Query(None, description="Comma-separated sources: aerial,mapeo,satellite"),
    limit: int = Query(80, ge=1, le=300),
    max_points_per_layer: int = Query(900, ge=50, le=5000),
    current_user: Any = Depends(get_current_user),
):
    return build_work_area_layers_response(
        current_user=current_user,
        sources=sources,
        limit=limit,
        max_points_per_layer=max_points_per_layer,
    )
