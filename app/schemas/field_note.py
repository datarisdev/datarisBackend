from uuid import UUID
from datetime import datetime, date
from pydantic import BaseModel
from typing import Optional, List, Dict

from app.models.field_note import NoteStatus


class FieldNoteBase(BaseModel):
    title: str
    content: str | None = None
    note_type: str
    index_type: str | None = None
    status: NoteStatus = NoteStatus.active
    tags: list[str] | None = None
    attachments: list[str] | None = None
    location: dict | None = None
    layer_date: date | None = None
    reference_date: date | None = None


class FieldNoteCreate(FieldNoteBase):
    pass


class FieldNoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    note_type: str | None = None
    index_type: str | None = None
    status: NoteStatus | None = None
    tags: list[str] | None = None
    attachments: list[str] | None = None
    location: dict | None = None
    layer_date: date | None = None
    reference_date: date | None = None


class FieldNoteResponse(FieldNoteBase):
    id: UUID
    user_id: UUID
    parcel_id: Optional[UUID]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
