import uuid
from datetime import datetime, date
from enum import Enum as PyEnum

from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    Enum,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class NoteStatus(str, PyEnum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class FieldNote(Base):
    __tablename__ = "field_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(String, nullable=True)

    note_type: Mapped[str] = mapped_column(String, nullable=False)
    index_type: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus, name="field_note_status"),
        default=NoteStatus.active,
        nullable=False,
    )

    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    attachments: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    layer_date: Mapped[date | None] = mapped_column(nullable=True)
    reference_date: Mapped[date | None] = mapped_column(nullable=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    parcel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

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
    user = relationship("User")
    parcel = relationship("Parcel", back_populates="field_notes")
