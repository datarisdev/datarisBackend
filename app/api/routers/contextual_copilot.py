from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import get_current_user
from app.services.contextual_copilot import process_contextual_copilot

router = APIRouter(prefix="/copilot", tags=["Copiloto contextual"])


@router.post("/contextual-analysis")
async def contextual_analysis(
    payload: Dict[str, Any] = Body(default_factory=dict),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    try:
        result = await process_contextual_copilot(payload or {})
        return {"data": result, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error generando análisis contextual: {exc}")
