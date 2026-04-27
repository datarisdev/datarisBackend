from datetime import datetime
import uuid
from sqlalchemy import ForeignKey, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 🔹 who created this user
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    # 🔹 Relationships
    created_by = relationship(
        "User",
        remote_side=[id],
        back_populates="created_users",
    )

    created_users = relationship(
        "User",
        back_populates="created_by",
        cascade="all, delete",
    )

    profile = relationship("Profile", uselist=False, back_populates="user", cascade="all, delete")
    roles = relationship("UserRole", back_populates="user", cascade="all, delete") 
    user_modules = relationship("UserModule", back_populates="user", cascade="all, delete-orphan")
    parcels = relationship(
        "Parcel",
        back_populates="user",
        cascade="all, delete-orphan",
    )
      

