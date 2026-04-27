# app/models/platform_module.py
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.models.base import Base
from sqlalchemy.orm import relationship

import uuid

class PlatformModule(Base):
    __tablename__ = "platform_modules"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=True)
    icon = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user_modules = relationship("UserModule", back_populates="module")  # link back to UserModule

