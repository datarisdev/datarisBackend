import uuid
from enum import Enum
from datetime import date, datetime
from sqlalchemy import JSON, Date, DateTime, Float, Integer, Text, ForeignKey, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column
from app.models.base import Base


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class SatelliteJob(Base):
    __tablename__ = "satellite_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    indices: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    max_cloud: Mapped[float | None] = mapped_column(Float, nullable=True)

    parcel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, name="satellite_job_status"),
        nullable=False,
        default=JobStatus.PENDING,
        index=True,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # 🔗 Relationships
    parcel = relationship("Parcel", back_populates="jobs")
    #user = relationship("User", back_populates="jobs")
