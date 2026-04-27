from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from app.api.deps import get_db
from app.models.user_roles import UserRole, AppRole
from app.api.deps import get_current_user

router = APIRouter(prefix="/roles", tags=["roles"])

# ----------------------
# Schemas
# ----------------------
from pydantic import BaseModel

class RoleIn(BaseModel):
    user_id: UUID
    role: AppRole

# ----------------------
# Routes
# ----------------------
@router.post("/")
def add_role(data: RoleIn, db: Session = Depends(get_db)):
    existing = db.query(UserRole).filter(
        UserRole.user_id == data.user_id, UserRole.role == data.role
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Role already assigned")
    role = UserRole(user_id=data.user_id, role=data.role)
    db.add(role)
    db.commit()
    return {"message": "Role assigned"}

@router.delete("/")
def remove_role(data: RoleIn, db: Session = Depends(get_db)):
    role = db.query(UserRole).filter(
        UserRole.user_id == data.user_id, UserRole.role == data.role
    ).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.delete(role)
    db.commit()
    return {"message": "Role removed"}

@router.get("/{user_id}")
def list_roles(user_id: UUID, db: Session = Depends(get_db)):
    roles = db.query(UserRole).filter(UserRole.user_id == user_id).all()
    return [r.role.value for r in roles]
