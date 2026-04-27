# ----------------------
# Schemas
# ----------------------
from uuid import UUID

from pydantic import BaseModel


class ProfileIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    location: str | None = None
    phone: str | None = None
    company_name: str | None = None    
    hectareas: float | None = None    

class ProfileOut(ProfileIn):
    user_id: UUID

    class Config:
        orm_mode = True