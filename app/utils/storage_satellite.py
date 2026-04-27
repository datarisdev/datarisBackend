import tempfile
from google.cloud import storage
from datetime import timedelta
import os

import numpy as np
import rasterio

BUCKET_NAME = "dataris-satellite"
storage_client = storage.Client()



def upload_satellite_tif(
    raster: np.ndarray,
    meta: dict,
    user_id: str,
    parcel_id: str,
    index_type: str,
    image_date: str,
) -> str:
    bucket = storage_client.bucket("dataris-satellite")

    object_path = (
        f"satellite/{user_id}/"
        f"{parcel_id}/"
        f"{index_type}/"
        f"{image_date}.tif"
    )

    fd, tmp_path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)  # CRITICAL on Windows

    try:
        with rasterio.open(tmp_path, "w", **meta) as dst:
            dst.write(raster, 1)

        bucket.blob(object_path).upload_from_filename(tmp_path)

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return object_path

def generate_signed_satellite_url(object_path: str) -> str:
    bucket = storage_client.bucket("dataris-satellite")
    blob = bucket.blob(object_path)

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET",
    )

def delete_satellite_images_for_parcel(
    *,
    user_id: str,
    parcel_id: str,
):
    """
    Deletes all satellite images for a specific parcel.
    Removes:
      satellite/{user_id}/{parcel_id}/...

    User folder remains untouched.
    """
    bucket = storage_client.bucket(BUCKET_NAME)
    prefix = f"satellite/{user_id}/{parcel_id}/"

    blobs = bucket.list_blobs(prefix=prefix)

    deleted = 0
    for blob in blobs:
        blob.delete()
        deleted += 1

    return deleted


def delete_satellite_object(object_path: str):
    bucket = storage_client.bucket(BUCKET_NAME)
    bucket.blob(object_path).delete()
