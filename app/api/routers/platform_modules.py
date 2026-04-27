# app/api/routes/platform_modules.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.api.deps import get_db
from app.models.platform_module import PlatformModule
from app.models.user_modules import UserModule
from app.schemas.platform_module import PlatformModuleCreate, PlatformModuleUpdate, PlatformModuleOut
from app.api.deps import get_current_admin

router = APIRouter(prefix="/platform_modules", tags=["Platform Modules"])

# List all modules
@router.get("/", response_model=List[PlatformModuleOut])
def list_platform_modules(db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    modules = db.query(PlatformModule).all()
    return modules

# Get a single module by ID
@router.get("/{module_id}", response_model=PlatformModuleOut)
def get_platform_module(module_id: str, db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    module = db.query(PlatformModule).filter(PlatformModule.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    return module

# Create a new module
@router.post("/", response_model=PlatformModuleOut)
def create_platform_module(module_in: PlatformModuleCreate, db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    module = PlatformModule(**module_in.dict())
    db.add(module)
    db.commit()
    db.refresh(module)
    return module

# Update a module
@router.put("/{module_id}", response_model=PlatformModuleOut)
def update_platform_module(module_id: str, module_in: PlatformModuleUpdate, db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    module = db.query(PlatformModule).filter(PlatformModule.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    data = module_in.dict(exclude_unset=True)

    # Check if `is_active` is being updated
    is_active_updated = "is_active" in data and data["is_active"] != module.is_active

    for field, value in module_in.dict(exclude_unset=True).items():
        setattr(module, field, value)
    
    # If is_active changed, update all related user_modules
    if is_active_updated:
        db.query(UserModule).filter(UserModule.module_id == module_id).update(
            {"is_active": module.is_active}
        )
        db.commit()
        
    db.commit()
    db.refresh(module)
    return module

# Delete a module
@router.delete("/{module_id}", status_code=204)
def delete_platform_module(module_id: str, db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    module = db.query(PlatformModule).filter(PlatformModule.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    db.delete(module)
    db.commit()

# When deactivating a PlatformModule, soft deactivate all user associations
@router.patch("/{module_id}/toggle") # DEPRECATED
def toggle_module(module_id: str, db: Session = Depends(get_db)):
    module = db.query(PlatformModule).filter(PlatformModule.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    module.is_active = not module.is_active
    db.commit()

    # Update all user_modules linked to this module
    db.query(UserModule).filter(UserModule.module_id == module_id).update({"is_active": module.is_active})
    db.commit()

    return {"id": module.id, "is_active": module.is_active}