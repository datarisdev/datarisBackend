from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from typing import Optional, Any

from app.schemas.parcel_crop import ParcelCropOut


class ParcelBase(BaseModel):
    name: str
    geometry: Any  # GeoJSON or similar
    area: Optional[float] = None
    file_url: Optional[str] = None


class ParcelCreate(ParcelBase):
    pass


class ParcelUpdate(BaseModel):
    name: Optional[str] = None
    geometry: Optional[Any] = None
    area: Optional[float] = None
    file_url: Optional[str] = None


class ParcelOut(ParcelBase):
    id: UUID
    user_id: UUID
    name: str
    geometry: Any
    area: float
    file_url: Optional[str]
    created_at: datetime
    finca: str
    lote: str
    codigo: str
    external_id:str
    crop: Optional[ParcelCropOut] = None  
    
    class Config:
        from_attributes = True
