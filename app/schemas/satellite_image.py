# app/schemas/satellite_image.py
from datetime import date, datetime
from uuid import UUID
from pydantic import BaseModel
from typing import Optional, Dict
from app.models.satellite_image import ProcessingStatus


class SatelliteImageBase(BaseModel):
    image_date: datetime
    index_type: str
    cloud_coverage: Optional[float] = None
    bounds: Optional[Dict] = None
    statistics: Optional[Dict] = None

class SatelliteImageCreate(SatelliteImageBase):
    parcel_id: UUID

class SatelliteImageUpdate(BaseModel):
    image_url: Optional[str] = None
    cloud_coverage: Optional[float] = None
    bounds: Optional[Dict] = None
    statistics: Optional[Dict] = None
    processing_status: Optional[ProcessingStatus] = None

class SatelliteImageOut(SatelliteImageBase):
    id: UUID
    user_id: UUID
    image_url: str
    processing_status: ProcessingStatus
    cloud_coverage: Optional[float]
    bounds: Optional[Dict]
    statistics: Optional[Dict]
    created_at: datetime

    class Config:
        from_attributes = True

class SatelliteProcessRequest(BaseModel):
    parcel_id: UUID
    #start_date: date
    #end_date: date
    #indices: list[str]
    #max_cloud: float | None = None

