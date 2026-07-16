import logging
from datetime import datetime, timedelta
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:72]
    return pwd_context.hash(truncated)

def verify_password(password: str, hashed: str) -> bool:
    # Un hash almacenado ausente o en un formato no reconocido (p. ej. cuentas
    # sembradas sin bcrypt) hacía que passlib lanzara UnknownHashError y el login
    # devolviera 500 en vez de un 401 limpio. Se trata como credencial inválida.
    if not hashed:
        return False
    truncated = password.encode("utf-8")[:72]
    try:
        return pwd_context.verify(truncated, hashed)
    except (ValueError, TypeError) as exc:
        # passlib lanza UnknownHashError/MalformedHashError (subclases de
        # ValueError) cuando el hash almacenado no es bcrypt reconocible.
        logger.warning("verify_password: hash no verificable (%s)", exc)
        return False


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
