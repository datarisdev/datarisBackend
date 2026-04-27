# app/models/satellite_image.py
import uuid
from datetime import datetime
from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    Float,
    Enum,
    JSON,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from enum import Enum as PyEnum

from app.models.base import Base


class ProcessingStatus(str, PyEnum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class SatelliteImage(Base):
    __tablename__ = "satellite_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    parcel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="CASCADE"),
        nullable=False,
    )

    image_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    index_type: Mapped[str] = mapped_column(String, nullable=False)
    # e.g. NDVI, EVI, NDWI, RGB, etc.

    image_object_path: Mapped[str] = mapped_column(String, nullable=False)

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus),
        default=ProcessingStatus.pending,
        nullable=False,
    )

    cloud_coverage: Mapped[float | None] = mapped_column(Float)
    bounds: Mapped[dict | None] = mapped_column(JSON)
    statistics: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # 🔗 Relationships
    parcel = relationship("Parcel", back_populates="satellite_images")
    user = relationship("User")
