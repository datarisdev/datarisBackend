from google.cloud import storage
import uuid
import os
from datetime import timedelta

BUCKET_NAME = "dataris-user-avatars"
storage_client = storage.Client()

def upload_avatar(file, content_type: str, user_id: str) -> str:
    bucket = storage_client.bucket(BUCKET_NAME)

    ext = os.path.splitext(file.filename)[1].lower()
    filename = f"avatars/{user_id}/avatar{ext}"

    blob = bucket.blob(filename)

    blob.upload_from_file(
        file.file,
        content_type=content_type,
        rewind=True,
    )

    # ✅ Generate signed URL (e.g. 7 days)
    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=1500), # CHECK THIS. IMPORTANT TO CHANGE IT IN THE FUTURE
        method="GET",
    )

    return signed_url

def delete_user_avatars(user_id: str):
    bucket = storage_client.bucket(BUCKET_NAME)
    prefix = f"avatars/{user_id}/"

    blobs = bucket.list_blobs(prefix=prefix)
    for blob in blobs:
        blob.delete()
        
def generate_avatar_url(object_path: str) -> str:
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(object_path)

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET",
    )
