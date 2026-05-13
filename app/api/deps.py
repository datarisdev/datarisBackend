from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError, ExpiredSignatureError
from app.core.config import settings
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.user_roles import UserRole, AppRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
oauth2_scheme_admin = OAuth2PasswordBearer(tokenUrl="/api/admin_users/login")

def get_current_admin(token: str = Depends(oauth2_scheme_admin)):
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        admin_id = payload.get("sub")
        role = payload.get("role")
        token_type = payload.get("type")

        if not admin_id or token_type != "admin":
            raise HTTPException(status_code=401, detail="Invalid token")

        return {"id": admin_id, "role": role}

    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SESSION_EXPIRED",
        )
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    
def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        user_id: str = payload.get("sub")
        role = payload.get("role")
        if user_id is None:
            raise HTTPException(status_code=401)
        return {"id": user_id, "role": role} 
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SESSION_EXPIRED",
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_role(role: AppRole):
    def role_checker(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
        user_roles = db.query(UserRole).filter(UserRole.user_id == user_id).all()
        if not any(r.role == role for r in user_roles):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user_id
    return role_checker