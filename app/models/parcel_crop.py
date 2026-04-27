import uuid
from datetime import datetime, date

from sqlalchemy import Date, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class ParcelCrop(Base):
    __tablename__ = "parcel_crops"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    parcel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # 👈 1-to-1 relationship
        index=True,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    crop_type: Mapped[str] = mapped_column(String, nullable=False)
    variety: Mapped[str | None] = mapped_column(String, nullable=True)

    planting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    harvest_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    estimated_yield: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # 🔗 Relationships
    parcel = relationship("Parcel", back_populates="crop")
    user = relationship("User")
