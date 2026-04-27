# app/schemas/platform_module.py
from datetime import datetime
from pydantic import BaseModel
from typing import Optional

class PlatformModuleBase(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    is_active: Optional[bool] = True

class PlatformModuleCreate(PlatformModuleBase):
    pass

class PlatformModuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    is_active: Optional[bool] = None

class PlatformModuleOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    is_active: bool
    created_at: datetime 

    class Config:
        orm_mode = True
