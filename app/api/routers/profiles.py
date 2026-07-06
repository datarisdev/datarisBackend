from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from uuid import UUID
from app.api.deps import get_db
from app.models.profiles import Profile
from app.api.deps import get_current_user
from pydantic import BaseModel

from app.schemas.profile import ProfileIn, ProfileOut
from app.utils.storage_avatars import upload_avatar
import uuid

router = APIRouter(prefix="/profiles", tags=["profiles"])

# Azure Blob Storage gestiona los avatares mediante app.utils.storage_avatars.

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
# ----------------------
# Routes
# ----------------------
@router.get("/me", response_model=ProfileOut)
def get_my_profile(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile

@router.put("/me", response_model=ProfileOut)
def update_my_profile(data: ProfileIn, user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    for field, value in data.dict(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return profile

@router.post("/avatar")
async def upload_my_avatar(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # ---- Validation ----
    ext = file.filename.split(".")[-1].lower()
    if ext not in {"jpg", "jpeg", "png"}:
        raise HTTPException(status_code=400, detail="Formato no permitido")

    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Máx 2MB")

    file.file.seek(0)

    # ---- Upload ----
    avatar_url = upload_avatar(
        file=file,
        content_type=file.content_type,
        user_id=user_id,
    )

    # ---- Save in DB ----
    profile = db.query(Profile).filter(Profile.user_id == user_id).first()
    profile.avatar_url = avatar_url
    db.commit()

    return {"avatar_url": avatar_url}
