from datetime import datetime
import uuid
from sqlalchemy import String, ForeignKey, DateTime, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base

class Parcel(Base):
    __tablename__ = "parcels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[float | None] = mapped_column(Float, nullable=True)# hectares can be derived from area if needed later
    finca: Mapped[str | None] = mapped_column(String, index=True)
    lote: Mapped[str | None] = mapped_column(String, index=True)
    codigo: Mapped[str | None] = mapped_column(String, index=True)
    external_id: Mapped[str | None] = mapped_column(String, index=True)
    
    geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    file_url: Mapped[str | None] = mapped_column(String, nullable=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    updated_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    jobs: Mapped[list["SatelliteJob"]] = relationship(
        "SatelliteJob",
        back_populates="parcel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


    # relationships
    user = relationship("User", back_populates="parcels")
    satellite_images = relationship(
        "SatelliteImage",
        back_populates="parcel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    field_notes = relationship(
        "FieldNote",
        back_populates="parcel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    crop = relationship(
        "ParcelCrop",
        back_populates="parcel",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


