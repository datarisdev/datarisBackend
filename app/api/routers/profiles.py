from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.profiles import Profile
from app.schemas.profile import ProfileIn, ProfileOut
from app.utils.storage_avatars import resolve_avatar_url, upload_avatar

router = APIRouter(prefix="/profiles", tags=["profiles"])

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
MAX_FILE_SIZE = 2 * 1024 * 1024


def _profile_response(profile: Profile) -> dict:
    return {
        "user_id": profile.user_id,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "avatar_url": resolve_avatar_url(profile.avatar_url),
        "location": profile.location,
        "phone": profile.phone,
        "company_name": profile.company_name,
        "hectareas": profile.hectareas,
    }


@router.get("/me", response_model=ProfileOut)
def get_my_profile(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == current_user["id"]).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(profile)


@router.put("/me", response_model=ProfileOut)
def update_my_profile(
    data: ProfileIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(Profile).filter(Profile.user_id == current_user["id"]).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return _profile_response(profile)


@router.post("/avatar")
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Formato no permitido")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Máx 2MB")
    file.file.seek(0)

    user_id = str(current_user["id"])
    avatar_reference = upload_avatar(
        file=file,
        content_type=file.content_type,
        user_id=user_id,
    )

    profile = db.query(Profile).filter(Profile.user_id == current_user["id"]).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Store a durable Azure Blob reference. Never persist an expiring SAS URL.
    profile.avatar_url = avatar_reference
    db.commit()

    return {"avatar_url": resolve_avatar_url(avatar_reference)}
