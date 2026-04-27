from typing import List, Optional
from pydantic import BaseModel, EmailStr
from uuid import UUID

# This one create the superadmin users to manage the backend
class AdminUserCreate(BaseModel):
    email: str
    password: str  
    
class AdminUserLogin(BaseModel):
    email: str
    password: str

class AdminUserOut(BaseModel):
    id: UUID
    email: str
    admin_role: str
    is_active: bool

    class Config:
        orm_mode = True

# This one create the users (clients) from admin
class AdminCreateUser(BaseModel):
    email: EmailStr
    password: str

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    hectareas: Optional[float] = None
    max_users: Optional[float] = None

    roles: List[str]
    modules: Optional[List[str]] = []

class AdminOutUser(BaseModel):
    id: UUID
    email: str
    is_active: bool
    first_name: Optional[str]
    last_name: Optional[str]
    company_name: Optional[str]
    hectareas: Optional[float]
    roles: list[str]
    created_by_id: Optional[str] = None
    max_users: Optional[float] = None
