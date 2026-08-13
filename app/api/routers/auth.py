from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from app.core.security import hash_password, verify_password, create_access_token
from app.api.deps import get_current_admin, get_current_user, get_db
from app.models.profiles import Profile
from app.models.user_modules import UserModule
from app.schemas.user import UserCreate
from app.models.user import User
from app.models.user_roles import UserRole, AppRole
from app.schemas.user_admin import AdminCreateUser, AdminOutUser, AdminUserCreate, AdminUserOut
import logging
logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/auth", tags=["auth"])

# ----------------------
# Schemas
# ----------------------
class UserRegister(BaseModel):
    email: EmailStr
    password: str
    first_name: str | None = None
    last_name: str | None = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

# ----------------------
# Routes
# ----------------------
# This one register users from other users
@router.post("/register")
def register(user_in: UserCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    logging.info(f"current_user payload: {current_user}")
    logging.info(f"current_user role: {current_user.get('role')}")
    # Solo los administradores pueden crear usuarios. `role` puede venir vacío o
    # como None (por ejemplo, con un token compat que no lleva claim de rol): en
    # ese caso NO es admin y se rechaza, en lugar de reventar con un TypeError que
    # devolvería un 500 en vez de un 403.
    creator_roles = current_user.get("role") or ""
    if "admin" not in creator_roles:
        raise HTTPException(status_code=403, detail="Not authorized")

    logging.info(f"Fetching admin user")
    # Fetch the current user from DB
    creator = db.query(User).filter(User.id == current_user["id"]).first()
    if not creator:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check user limit
    profile = creator.profile
    created_count = (
        db.query(User)
        .filter(User.created_by_id == current_user["id"])
        .count()
    )
    logging.info(f"Checking max users")
    if profile.max_users <= created_count:
        raise HTTPException(
            status_code=400,
            detail="User limit reached",
        )

    # Email uniqueness
    if db.query(User).filter(User.email == user_in.email).first():
        raise HTTPException(status_code=400, detail="Email already exists")

    # Sin escalada de privilegios: un administrador de cliente no puede crear
    # otros administradores. Sólo los superadministradores de plataforma dan de
    # alta administradores, por la vía dedicada de /api/auth/ (create_user).
    if str(user_in.role) == "admin":
        raise HTTPException(status_code=403, detail="Cannot assign admin role")
    logging.info(f"user_in: {user_in}")
    # Create user
    new_user = User(
        email=user_in.email,
        password_hash=hash_password(user_in.password),
        is_active=True,
        created_by_id=current_user["id"],  
    )
    db.add(new_user)
    db.flush()

    # Create profile
    profile = Profile(
        user_id=new_user.id,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        company_name=user_in.company_name,
        hectareas=user_in.hectareas,
        max_users=0,  
    )
    db.add(profile)

    # 🎭 Assign SINGLE role
    db.add(UserRole(user_id=new_user.id, role=AppRole(user_in.role)))

    # Get the modules that the creator has access to
    creator_module_ids = {
        um.module_id
        for um in creator.user_modules
        if um.is_active
    }
    logging.info(f"current_user role: {creator_module_ids}")
    logging.info(f"user_in.modules: {user_in.modules}")
    for module_id in user_in.modules or []:
        module_id_str = str(module_id)  # convert UUID to string
        if module_id_str not in creator_module_ids:
            raise HTTPException(
                status_code=403,
                detail="Cannot assign modules you don't own",
            )
        db.add(UserModule(user_id=new_user.id, module_id=module_id, is_active=True))

    db.commit()

    return {
        "id": new_user.id,
        "email": new_user.email,
        "is_active": new_user.is_active,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "company_name": profile.company_name,
        "hectareas": profile.hectareas,
        "roles": [user_in.role],
        "modules": user_in.modules or [],
        "created_by_id": new_user.created_by_id,
    }

# This one register users from user admin
@router.post("/", response_model=AdminOutUser)
def create_user(
    data: AdminCreateUser,
    db: Session = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    if admin.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Not authorized")

    # 1️⃣ Check email uniqueness
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")

    # 2️⃣ Create user
    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        is_active=True,
        created_by_id=None,
    )
    db.add(user)
    db.flush()  # get user.id

    # 3️⃣ Create profile
    profile = Profile(
        user_id=user.id,
        first_name=data.first_name,
        last_name=data.last_name,
        company_name=data.company_name,
        hectareas=data.hectareas,
        max_users=data.max_users
    )
    db.add(profile)

    # 4️⃣ Assign roles
    for role in data.roles:
        db.add(UserRole(user_id=user.id, role=AppRole(role)))

    # 5️⃣ Assign modules
    if data.modules:
        for module_id in data.modules:
            db.add(UserModule(user_id=user.id, module_id=module_id, is_active=True))

    db.commit()
    db.refresh(user)

    return {
        "id": user.id,
        "email": user.email,
        "is_active": user.is_active,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "company_name": profile.company_name,
        "hectareas": profile.hectareas,
        "roles": data.roles,
        "modules": data.modules or [],
        "max_users": profile.max_users, 
        "created_by_id": user.created_by_id
    }

@router.post("/login")
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive")

    # Get user roles
    roles = [r.role.value for r in user.roles]  

    # Create access token
    token = create_access_token(subject=str(user.id), role=roles, token_type="user")

    # Build modules info
    modules = [
        {
            "id": um.module.id,
            "name": um.module.name,
            "description": um.module.description,
            "icon": um.module.icon,
            "is_active": um.module.is_active
        }
        for um in user.user_modules
    ]

    profile = user.profile

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "is_active": user.is_active,
            "first_name": profile.first_name if profile else None,
            "last_name": profile.last_name if profile else None,
            "company_name": profile.company_name if profile else None,
            "assigned_hectares": profile.hectareas if profile else None,
            "roles": roles,
            "modules": modules,
            "max_users": profile.max_users if profile else None,
            "created_by_id": user.created_by_id
        }
    }
