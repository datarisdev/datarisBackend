from uuid import UUID
from datetime import date, datetime
from pydantic import BaseModel
from typing import Optional


class ParcelCropBase(BaseModel):
    crop_type: str
    variety: Optional[str] = None
    planting_date: Optional[date] = None
    harvest_date: Optional[date] = None
    estimated_yield: Optional[float] = None


class ParcelCropCreate(ParcelCropBase):
    pass


class ParcelCropUpdate(BaseModel):
    crop_type: str | None = None
    variety: str | None = None
    planting_date: date | None = None
    harvest_date: date | None = None
    estimated_yield: float | None = None


class ParcelCropResponse(ParcelCropBase):
    id: UUID
    parcel_id: UUID
    user_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ParcelCropOut(BaseModel):
    id: UUID
    crop_type: str
    variety: Optional[str]
    planting_date: Optional[date]
    harvest_date: Optional[date]
    estimated_yield: Optional[float]

    class Config:
        from_attributes = True