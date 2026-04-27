from uuid import UUID
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.schemas.field_note import (
    FieldNoteCreate,
    FieldNoteUpdate,
    FieldNoteResponse,
)
from app.models.field_note import FieldNote
from app.api.deps import get_current_user, get_db
from app.models.user import User

router = APIRouter(prefix="/parcels/{parcel_id}/notes", tags=["Field Notes"])

@router.post("", response_model=FieldNoteResponse)
def create_note(
    parcel_id: UUID= Path(...),
    payload: FieldNoteCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = FieldNote(
        **payload.model_dump(exclude={"parcel_id"}),
        parcel_id=parcel_id,
        user_id=current_user["id"]
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note

@router.get("", response_model=list[FieldNoteResponse])
def get_notes(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(FieldNote)
        .filter(FieldNote.parcel_id == parcel_id)
        .order_by(FieldNote.created_at.desc())
        .all()
    )

@router.patch("/{note_id}", response_model=FieldNoteResponse)
def update_note(
    parcel_id: UUID= Path(...),
    note_id: UUID= Path(...),
    payload: FieldNoteUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = (
        db.query(FieldNote)
        .filter(
            FieldNote.id == note_id,
            FieldNote.parcel_id == parcel_id,
        )
        .first()
    )

    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(note, key, value)

    db.commit()
    db.refresh(note)
    return note

@router.delete("/{note_id}")
def delete_note(
    parcel_id: UUID= Path(...),
    note_id: UUID= Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = db.query(FieldNote).filter(
        FieldNote.id == note_id,
        FieldNote.parcel_id == parcel_id,
    ).first()

    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    db.delete(note)
    db.commit()
    return {"ok": True}

@router.delete("")
def delete_all_notes(
    parcel_id: UUID= Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(FieldNote).filter(FieldNote.parcel_id == parcel_id).delete()
    db.commit()
    return {"ok": True}
