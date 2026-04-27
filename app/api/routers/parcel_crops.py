from uuid import UUID
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.parcel_crop import ParcelCrop
from app.models.parcel import Parcel
from app.schemas.parcel_crop import (
    ParcelCropCreate,
    ParcelCropUpdate,
    ParcelCropResponse,
)

router = APIRouter(prefix="/parcels/{parcel_id}/crop", tags=["Parcel Crops"])

def assert_parcel_access(db: Session, parcel_id: UUID, current_user: dict, allow_user: bool):
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()

    if not parcel:
        raise HTTPException(status_code=404, detail="Parcel not found")

    if str(parcel.user_id) != current_user["id"] and current_user["role"] != "admin" and not allow_user:
        raise HTTPException(status_code=403, detail="Not authorized")

    return parcel

@router.post("", response_model=ParcelCropResponse)
def create_or_replace_crop(
    parcel_id: UUID= Path(...),
    payload: ParcelCropCreate= Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    assert_parcel_access(db, parcel_id, current_user, False)

    existing = (
        db.query(ParcelCrop)
        .filter(ParcelCrop.parcel_id == parcel_id)
        .first()
    )

    if existing:
        for key, value in payload.model_dump(exclude={"parcel_id"}).items():
            setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing

    crop = ParcelCrop(
        **payload.model_dump(exclude={"parcel_id"}),
        parcel_id=parcel_id,
        user_id=current_user["id"],
    )

    db.add(crop)
    db.commit()
    db.refresh(crop)
    return crop

@router.get("", response_model=ParcelCropResponse)
def get_crop(
    parcel_id: UUID= Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    assert_parcel_access(db, parcel_id, current_user, True)

    crop = (
        db.query(ParcelCrop)
        .filter(ParcelCrop.parcel_id == parcel_id)
        .first()
    )

    if not crop:
        raise HTTPException(status_code=404, detail="No crop data for parcel")

    return crop

@router.patch("", response_model=ParcelCropResponse)
def update_crop(
    parcel_id: UUID= Path(...),
    payload: ParcelCropUpdate= Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    assert_parcel_access(db, parcel_id, current_user, True)

    crop = (
        db.query(ParcelCrop)
        .filter(ParcelCrop.parcel_id == parcel_id)
        .first()
    )

    if not crop:
        raise HTTPException(status_code=404, detail="Crop not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(crop, key, value)

    db.commit()
    db.refresh(crop)
    return crop

@router.delete("")
def delete_crop(
    parcel_id: UUID= Path(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    assert_parcel_access(db, parcel_id, current_user, False)

    crop = (
        db.query(ParcelCrop)
        .filter(ParcelCrop.parcel_id == parcel_id)
        .first()
    )

    if not crop:
        raise HTTPException(status_code=404, detail="Crop not found")

    db.delete(crop)
    db.commit()
    return {"ok": True}
