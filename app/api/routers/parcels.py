from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session, joinedload
from uuid import UUID, uuid4
import json
from app.models.parcel import Parcel
from app.schemas.parcel import ParcelCreate, ParcelOut
from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.utils.storage_parcels import delete_parcel_files, upload_parcel_file
from app.utils.geometry import calculate_area_hectares, extract_area_from_properties_or_geometry, extract_main_properties

import logging
logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/parcels", tags=["Parcels"])

@router.post("/", response_model=ParcelOut)
def create_parcel(
    name: str = Form(...),
    geometry: str = Form(...), 
    area: float = Form(0),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    logging.info(f"name: {name}")
    logging.info(f"area: {area}")
    parcel_id = uuid4()
    logging.info(f"parcel_id: {parcel_id}")
    '''file_url = upload_parcel_file(
        file=file,
        content_type=file.content_type,
        user_id=str(current_user["id"]),
        parcel_id=str(parcel_id),
    )'''

    # 2️⃣ Parse geometry JSON
    geometry_dict = json.loads(geometry)

    # 1️⃣ Try extracting from SHP properties
    area_from_props = extract_area_from_properties_or_geometry(geometry_dict)

    if area_from_props:
        area = area_from_props
        logging.info(f"area from Shape_Area: {area} ha")

    # 2️⃣ Fallback to geometry calculation
    elif not area or area <= 0:
        area = calculate_area_hectares(geometry_dict)
        logging.info(f"area calculated from geometry: {area} ha")

    props = extract_main_properties(geometry_dict)

    # 3️⃣ Create DB record
    parcel = Parcel(
        id=parcel_id,
        name=name,
        area=area,
        geometry=geometry_dict,
        file_url="",
        user_id=current_user["id"],
        **props,
    )

    db.add(parcel)
    db.commit()
    db.refresh(parcel)

    return parcel

@router.get("/", response_model=list[ParcelOut])
def get_parcels(
    user_id: Optional[str] = None, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        db.query(Parcel)
        .options(joinedload(Parcel.crop))  
    )

    # If user_id is provided, filter by it
    if user_id:
        query = query.filter(Parcel.user_id == user_id)
    else:
        # Default: return parcels for current_user
        query = query.filter(Parcel.user_id == current_user["id"])

    return query.order_by(Parcel.created_at.desc()).all()

@router.delete("/{parcel_id}")
def delete_parcel(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    parcel = (
        db.query(Parcel)
        .filter(Parcel.id == parcel_id, Parcel.user_id == current_user["id"])
        .first()
    )

    if not parcel:
        raise HTTPException(status_code=404, detail="Parcel not found")

    try:
        delete_parcel_files(
            user_id=str(current_user.id),
            parcel_id=str(parcel.id),
        )
    except Exception as e:
        print(f"Failed to delete parcel files: {e}")

    db.delete(parcel)
    db.commit()

    return {"ok": True}

