from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError, ExpiredSignatureError
from app.core.config import settings
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.user import User
from app.models.user_admin import AdminUser
from app.models.user_roles import UserRole, AppRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
oauth2_scheme_admin = OAuth2PasswordBearer(tokenUrl="/api/admin_users/login")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _session_expired() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="SESSION_EXPIRED",
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
    )


def _ensure_mirrored_user(db: Session, compat_user: dict[str, Any]) -> None:
    """Los usuarios compat solo viven en el JSON de app/storage, pero varias
    tablas (harvest_sessions, parcels, ml_*, satellite_*) tienen FK real a
    `users`. Se refleja acá una fila mínima para que esos inserts no truenen
    con ForeignKeyViolation la primera vez que un usuario compat las usa.
    """
    from app.services.compat_mirror import ensure_mirrored_user

    ensure_mirrored_user(db, compat_user)


def get_current_admin(token: str = Depends(oauth2_scheme_admin), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        admin_id = payload.get("sub")
        role = payload.get("role")
        token_type = payload.get("type")

        if not admin_id or token_type != "admin":
            raise HTTPException(status_code=401, detail="Invalid token")

        admin = db.query(AdminUser).filter(AdminUser.id == admin_id, AdminUser.is_active == True).first()
        if not admin:
            raise _invalid_credentials()

        return {"id": admin_id, "role": role}

    except ExpiredSignatureError:
        raise _session_expired()
    except JWTError:
        raise _invalid_credentials()
    
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        user_id: str = payload.get("sub")
        role = payload.get("role")
        token_type = payload.get("type")
        if user_id is None or token_type not in {"user", "compat_user", None}:
            raise _invalid_credentials()

        if token_type == "compat_user":
            from app.api.routers.compat import read_db

            compat_db = read_db()
            compat_user = next((u for u in compat_db.get("users", []) if u.get("id") == user_id), None)
            if not compat_user or compat_user.get("is_active", True) is False:
                raise _invalid_credentials()
            _ensure_mirrored_user(db, compat_user)
        else:
            user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
            if not user:
                raise _invalid_credentials()
        return {"id": user_id, "role": role} 
    except ExpiredSignatureError:
        raise _session_expired()
    except JWTError:
        raise _invalid_credentials()


def require_role(role: AppRole):
    def role_checker(current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)):
        user_roles = db.query(UserRole).filter(UserRole.user_id == current_user["id"]).all()
        if not any(r.role == role for r in user_roles):
            raise HTTPException(status_code=403, detail="Forbidden")
        return current_user
    return role_checker
