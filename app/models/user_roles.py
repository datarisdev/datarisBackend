from sqlalchemy import Enum, DateTime, ForeignKey
from datetime import datetime
import uuid
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

from enum import Enum as PyEnum

class AppRole(str, PyEnum):
    tecnico = "tecnico"
    supervisor_campo = "supervisor_campo"
    visualizador = "visualizador"
    admin = "admin"

class UserRole(Base):
    __tablename__ = "user_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    role: Mapped[AppRole] = mapped_column(Enum(AppRole), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="roles")
