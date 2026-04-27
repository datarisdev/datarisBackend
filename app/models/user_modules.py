from sqlalchemy import UUID, Boolean, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base
import uuid

class UserModule(Base):
    __tablename__ = "user_modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    module_id: Mapped[str] = mapped_column(String, ForeignKey("platform_modules.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    user = relationship("User", back_populates="user_modules")
    module = relationship("PlatformModule", back_populates="user_modules")