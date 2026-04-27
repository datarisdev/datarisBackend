import uuid
from pydantic import BaseModel
from uuid import UUID
from typing import Optional, List

class UserCreate(BaseModel):
    email: str
    password: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    hectareas: Optional[float] = None
    role: str                  # 👈 SINGLE role
    modules: list[uuid.UUID] = []  # 👈 MULTIPLE modules
    created_by_id: Optional[str] = None
    max_users: Optional[float] = None

class UserUpdate(BaseModel):
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    hectareas: Optional[float] = None
    is_active: Optional[bool] = None
    roles: Optional[List[str]] = None
    modules: Optional[List[str]] = None
    created_by_id: Optional[str] = None
    max_users: Optional[float] = None

class UserOut(BaseModel):
    id: UUID
    email: str
    is_active: bool
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    hectareas: Optional[float] = None
    roles: List[str] = []
    modules: Optional[List[str]] = None
    created_by_id: Optional[str] = None
    max_users: Optional[float] = None
    
    class Config:
        orm_mode = True
