from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.core.security import create_access_token
from app.api.deps import get_current_admin, get_db
from app.models.user_admin import AdminUser
from app.schemas.user_admin import AdminUserCreate, AdminUserLogin, AdminUserOut
from passlib.context import CryptContext


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
router = APIRouter(prefix="/admin_users", tags=["Admin Users"])

# Create admin user (run once manually)
@router.post("/", response_model=AdminUserOut)
def create_admin_user(admin: AdminUserCreate, db: Session = Depends(get_db)):
    existing = db.query(AdminUser).filter(AdminUser.email == admin.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Admin already exists")
    
    hashed_password = pwd_context.hash(admin.password)
    new_admin = AdminUser(email=admin.email, password_hash=hashed_password, admin_role="superadmin")
    db.add(new_admin)
    db.commit()
    db.refresh(new_admin)
    return new_admin

# Login for admin panel
@router.post("/login")
def login(admin: AdminUserLogin, db: Session = Depends(get_db)):
    db_admin = db.query(AdminUser).filter(AdminUser.email == admin.email).first()
    if not db_admin or not pwd_context.verify(admin.password, db_admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not db_admin.is_active:
        raise HTTPException(status_code=401, detail="User inactive")
    
    # Use the new role-aware token
    token = create_access_token(subject=str(db_admin.id), role=db_admin.admin_role, token_type="admin")
    return {"access_token": token, "token_type": "bearer"}

@router.get("/me", response_model=AdminUserOut)
def get_current_admin_info(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Returns the current admin info based on the JWT token
    """
    db_admin = db.query(AdminUser).filter(AdminUser.id == admin["id"], AdminUser.is_active == True).first()
    if not db_admin:
        raise HTTPException(status_code=401, detail="Not authorized")
    
    return {
        "id": db_admin.id,
        "email": db_admin.email,
        "admin_role": db_admin.admin_role,
        "is_active": db_admin.is_active
    }
