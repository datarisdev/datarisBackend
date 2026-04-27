from google.cloud import storage
import uuid
import os
from datetime import timedelta

PARCELS_BUCKET_NAME = "dataris-parcels"

storage_client = storage.Client()

def upload_parcel_file(file, content_type: str, user_id: str, parcel_id:str) -> str:
    """
    Uploads a parcel file (zip/shp/etc) to GCS and returns a signed URL
    """

    bucket = storage_client.bucket(PARCELS_BUCKET_NAME)

    ext = os.path.splitext(file.filename)[1].lower()

    filename = f"parcels/{user_id}/{parcel_id}/original{ext}"

    blob = bucket.blob(filename)

    blob.upload_from_file(
        file.file,
        content_type=content_type,
        rewind=True,
    )

    # Signed URL (long-lived, like avatars)
    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=7),# CHECK THIS. IMPORTANT TO CHANGE IT IN THE FUTURE
        method="GET",
    )

    return signed_url

def delete_parcel_files(user_id: str, parcel_id: str):
    bucket = storage_client.bucket(PARCELS_BUCKET_NAME)
    prefix = f"parcels/{user_id}/{parcel_id}/"

    blobs = bucket.list_blobs(prefix=prefix)
    for blob in blobs:
        blob.delete()

def delete_all_user_parcels(user_id: str):
    bucket = storage_client.bucket(PARCELS_BUCKET_NAME)
    prefix = f"parcels/{user_id}/"

    blobs = bucket.list_blobs(prefix=prefix)
    for blob in blobs:
        blob.delete()

