from datetime import datetime, timedelta
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:72]
    return pwd_context.hash(truncated)

def verify_password(password: str, hashed: str) -> bool:
    truncated = password.encode("utf-8")[:72]
    return pwd_context.verify(truncated, hashed)


def create_access_token(subject: str, role: str, token_type: str = "admin") -> str:
    """
    Create JWT token including:
    - subject: user or admin ID
    - role: role string (e.g., "superadmin")
    - type: "user" or "admin"
    """
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "exp": expire
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
