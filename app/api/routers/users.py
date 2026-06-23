import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from app.api.deps import get_current_admin, get_db
from app.models.parcel import Parcel
from app.models.platform_module import PlatformModule
from app.models.user import User
from app.models.profiles import Profile
from app.models.user_modules import UserModule
from app.models.user_roles import UserRole, AppRole
from app.api.deps import get_current_user
from app.schemas.user_admin import AdminUserOut
from app.schemas.user import UserOut, UserUpdate
from app.utils.storage_avatars import delete_user_avatars, resolve_avatar_url
from app.utils.storage_parcels import delete_parcel_files

router = APIRouter(prefix="/users", tags=["users"])

# Routes
# ----------------------
@router.get("/me", response_model=dict)
def get_me(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id["id"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    profile = user.profile
    roles = [r.role.value for r in user.roles]
    modules = [
            {
                "id": um.module.id,
                "name": um.module.name,
                "description": um.module.description,
                "icon": um.module.icon,
                "is_active": um.module.is_active
            }
            for um in user.user_modules
        ] if user.user_modules else []
    
    # Fetch creator info if exists
    creator_first_name = None
    creator_last_name = None
    if user.created_by_id:
        creator = db.query(User).filter(User.id == user.created_by_id).first()
        if creator and creator.profile:
            creator_first_name = creator.profile.first_name
            creator_last_name = creator.profile.last_name

    return {
        "id": user.id,
        "email": user.email,
        "is_active": user.is_active,
        "first_name": profile.first_name if profile else None,
        "last_name": profile.last_name if profile else None,
        "company_name": profile.company_name if profile else None,
        "assigned_hectares": profile.hectareas if profile else None,
        "phone": profile.phone if profile else None,
        "location": profile.location if profile else None,
        "avatar_url": resolve_avatar_url(profile.avatar_url) if profile else None,
        "roles": roles,
        "modules":modules,
        "max_users": profile.max_users if profile else None,
        "created_by_id": {
            "id": user.created_by_id,
            "creator_first_name": creator_first_name,
            "creator_last_name": creator_last_name,
        }
    }

@router.get("/", response_model=list[dict])
def list_users(db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    if admin.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    users = db.query(User).all()
    result = []
    for u in users:
        roles = [r.role.value for r in u.roles]
        modules = [
            {
                "id": um.module.id,
                "name": um.module.name,
                "description": um.module.description,
                "icon": um.module.icon,
                "is_active": um.module.is_active
            }
            for um in u.user_modules
        ]
        profile = u.profile
        result.append({
            "id": u.id,
            "email": u.email,
            "is_active": u.is_active,
            "first_name": profile.first_name if profile else None,
            "last_name": profile.last_name if profile else None,
            "companies": profile.company_name if profile else None,
            "assigned_hectares": profile.hectareas if profile else None,
            "user_role": roles,
            "modules": modules,
            "created_at": u.created_at, 
            "max_users": profile.max_users if profile else None
        })
    return result

@router.delete("/{user_id}")
def delete_user(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    try:
        delete_user_avatars(user_id)
    except Exception as e:
        # log error, but don't block deletion
        print(f"Failed to delete avatars for user {user_id}: {e}")

   # 🔹 Check if user has admin role
    is_admin = any(role.role == AppRole.admin for role in user.roles)
    
    if is_admin:
        print(f"User {user_id} is admin, deleting all parcels")
        # Fetch parcels
        parcels = db.query(Parcel).filter(Parcel.user_id == user_id).all()
        for parcel in parcels:
            try:
                delete_parcel_files(str(user.id), str(parcel.id))
            except Exception as e:
                print(f"Failed to delete parcel files for {parcel.id}: {e}")
            db.delete(parcel)  # Delete from DB

    db.delete(user)
    db.commit()
    return {"message": "User deleted"}

@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: str, user_update: UserUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    profile = user.profile

    if user_update.email is not None:
        user.email = user_update.email
    if user_update.is_active is not None:
        user.is_active = user_update.is_active

    if profile:
        if user_update.first_name is not None:
            profile.first_name = user_update.first_name
        if user_update.last_name is not None:
            profile.last_name = user_update.last_name
        if user_update.company_name is not None:
            profile.company_name = user_update.company_name
        if user_update.hectareas is not None:
            profile.hectareas = user_update.hectareas

    if user_update.roles is not None:
        db.query(UserRole).filter(UserRole.user_id == user_id).delete()
        for role_name in user_update.roles:
            db.add(UserRole(user_id=user_id, role=AppRole(role_name)))
    
    # --- Update modules ---
    if user_update.modules is not None:
        # Remove all existing UserModule entries for this user
        db.query(UserModule).filter(UserModule.user_id == user_id).delete()

        # Add new entries
        for module_id in user_update.modules:
            # optional: validate module exists
            module = db.query(PlatformModule).filter(PlatformModule.id == module_id).first()
            if module:
                db.add(UserModule(user_id=user_id, module_id=module.id))

    db.commit()
    db.refresh(user)

    roles = [r.role.value for r in user.roles]
    modules = [um.module_id for um in user.user_modules]
    return {
        "id": user.id,
        "email": user.email,
        "is_active": user.is_active,
        "first_name": profile.first_name if profile else None,
        "last_name": profile.last_name if profile else None,
        "company_name": profile.company_name if profile else None,
        "hectareas": profile.hectareas if profile else None,
        "roles": roles,
        "modules": modules,
        "max_users": profile.max_users if profile else None
    }

@router.get("/created")
def get_users_created_by_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    users = (
        db.query(User)
        .filter(User.created_by_id == current_user["id"])
        .all()
    )

    result = []
    for user in users:
        role = user.roles[0].role.value if user.roles else None
        modules = [
            {
                "id": um.module.id,
                "name": um.module.name,
                "is_active": um.is_active,
            }
            for um in user.user_modules
        ]

        profile = user.profile

        result.append({
            "id": str(user.id),
            "email": user.email,
            "is_active": user.is_active,
            "role": role,
            "modules": modules,
            "first_name": profile.first_name if profile else None,
            "last_name": profile.last_name if profile else None,
            "company_name": profile.company_name if profile else None,
            "created_at": user.created_at
        })

    return {
        "count": len(result),
        "users": result,
    }
