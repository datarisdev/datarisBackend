from datetime import date, datetime, timedelta
import httpx
from fastapi.responses import JSONResponse
import httpx
import numpy as np
from pyproj import Transformer
from shapely.ops import transform
from shapely.geometry import shape
import rasterio
from io import BytesIO
import matplotlib.cm as cm
from fastapi.responses import StreamingResponse
from PIL import Image
import json
import logging
from fastapi import BackgroundTasks, Query
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from app.api.deps import get_current_user, get_db
from app.api.task import process_satellite_day_job
from app.core.satellite_config import SATELLITE_INDICES, SATELLITE_MAX_CLOUD, SATELLITE_YEARS_BACK
from app.models.parcel import Parcel
from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.models.satellite_job import JobStatus, SatelliteJob
from app.models.user import User
from rasterio.mask import mask
from app.schemas.satellite_image import SatelliteImageCreate, SatelliteImageOut, SatelliteProcessRequest
from app.services.satellite.processor import process_sentinel_range_task
from app.utils.storage_satellite import delete_satellite_images_for_parcel, delete_satellite_object, generate_signed_satellite_url
logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/satellite", tags=["satellite"])

# Routes
# ----------------------
@router.get("/parcel/{parcel_id}")
def list_satellite_images(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    images = (
        db.query(SatelliteImage)
        .filter(
            SatelliteImage.parcel_id == parcel_id,
            SatelliteImage.user_id == current_user["id"],
        )
        .all()
    )

    return [
        {
            **img.__dict__,
            "image_url": generate_signed_satellite_url(img.image_object_path),
        }
        for img in images
    ]

@router.delete("/{image_id}")
def delete_single_satellite_image(
    image_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    image = (
        db.query(SatelliteImage)
        .filter(
            SatelliteImage.id == image_id,
            SatelliteImage.user_id == current_user["id"],
        )
        .first()
    )

    if not image:
        raise HTTPException(status_code=404, detail="Satellite image not found")

    try:
        delete_satellite_object(image.image_object_path)
    except Exception as e:
        print(f"Failed deleting satellite file: {e}")

    db.delete(image)
    db.commit()

    return {"ok": True}

@router.delete("/parcel/{parcel_id}")
def delete_all_satellite_images_for_parcel(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    images = (
        db.query(SatelliteImage)
        .filter(
            SatelliteImage.parcel_id == parcel_id,
            SatelliteImage.user_id == current_user["id"],
        )
        .all()
    )

    if not images:
        raise HTTPException(status_code=404, detail="No satellite images found")

    try:
        deleted = delete_satellite_images_for_parcel(
            user_id=str(current_user["id"]),
            parcel_id=str(parcel_id),
        )
    except Exception as e:
        print(f"Failed deleting satellite files: {e}")

    for img in images:
        db.delete(img)

    db.commit()

    return {
        "ok": True,
        "deleted_images": len(images),
        "deleted_objects": deleted,
    }

@router.post("/process")
def process_satellite_images(
    payload: SatelliteProcessRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    logging.info(
        f"Starting process satellite images for the following payload: "
        f"{json.dumps(payload.model_dump(), default=str)}"
    )
    parcel = (
        db.query(Parcel)
        .filter(
            Parcel.id == payload.parcel_id,
            Parcel.user_id == current_user["id"],
        )
        .first()
    )
    logging.info(f"Parcel with id {payload.parcel_id} founded")
    if not parcel:
        raise HTTPException(404, "Parcel not found")

    background_tasks.add_task(
        process_sentinel_range_task,
        parcel_geometry=parcel.geometry,
        user_id=str(current_user["id"]),
        parcel_id=str(parcel.id),
        start_date=payload.start_date,
        end_date=payload.end_date,
        indices=payload.indices,
        max_cloud=payload.max_cloud,
    )

    return {"status": "processing_started"}

@router.get("/jobs/parcel/{parcel_id}")
def list_jobs(parcel_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    jobs = db.query(SatelliteJob).filter(
        SatelliteJob.parcel_id == parcel_id,
        SatelliteJob.user_id == current_user["id"],
    ).all()

    return [
        {
            "id": str(job.id),
            "day": job.day.isoformat(),
            "status": job.status.value,
            "attempts": job.attempts,
            "error": job.error,
        } for job in jobs
    ]

@router.post("/process_job")
def process_satellite(
    payload: SatelliteProcessRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Instead of processing immediately, create one SatelliteJob per day.
    """
    parcel = db.query(Parcel).filter(
        Parcel.id == payload.parcel_id,
        Parcel.user_id == current_user["id"],
    ).first()

    if not parcel:
        raise HTTPException(status_code=404, detail="Parcel not found")

    #start_date = payload.start_date
    #end_date = payload.end_date
    #indices = payload.indices
    #max_cloud = payload.max_cloud
    # ✅ Fixed backend logic
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * SATELLITE_YEARS_BACK)

    indices = SATELLITE_INDICES
    max_cloud = SATELLITE_MAX_CLOUD

    # Loop per day
    day = start_date
    created_jobs = []
    while day <= end_date:
        # Check if job already exists
        existing_job = db.query(SatelliteJob).filter(
            SatelliteJob.parcel_id == parcel.id,
            SatelliteJob.day == day,
        ).first()

        if not existing_job:
            job = SatelliteJob(
                user_id=current_user["id"],
                parcel_id=parcel.id,
                day=day,
                status=JobStatus.PENDING,
                indices=indices,       
                max_cloud=max_cloud,   
            )
            db.add(job)
            created_jobs.append(job)

        day += timedelta(days=1)

    db.commit()

    # Optionally enqueue Celery tasks immediately
    for job in created_jobs:
        process_satellite_day_job.delay(str(job.id))

    return {
        "message": "Jobs created",
        "jobs_created": len(created_jobs),
    }

@router.get("/parcel/{parcel_id}/histogram_all")
async def parcel_histogram_all(
    parcel_id: str,
    index_type: str = Query(...),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    max_cloud: float = Query(100.0, description="Maximum cloud coverage allowed, 0-100"),
    bins: int = 10,
    db: Session = Depends(get_db),
):
    """
    Return satellite image URL + histogram + stats for a parcel, index type, and date.
    """
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format. Use YYYY-MM-DD"})

    start = datetime.combine(date_obj, datetime.min.time())
    end = datetime.combine(date_obj, datetime.max.time())

    # Get parcel geometry
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return JSONResponse(status_code=404, content={"error": "Parcel not found"})

    # Compute parcel bounding box from geometry
    import shapely.geometry
    import shapely.ops

    try:
        geojson = parcel.geometry
        shapes = [shapely.geometry.shape(f["geometry"]) for f in geojson.get("features", [])]
        if shapes:
            merged = shapely.ops.unary_union(shapes)
            minx, miny, maxx, maxy = merged.bounds  # lon/lat
            parcel_bounds = {
                "west": minx,
                "south": miny,
                "east": maxx,
                "north": maxy
            }
        else:
            parcel_bounds = None
    except Exception as e:
        print("Error parsing parcel geometry:", e)
        parcel_bounds = None

    # Filter images by parcel, index_type, date, completed status
    image = db.query(SatelliteImage).filter(
        SatelliteImage.parcel_id == parcel_id,
        SatelliteImage.index_type == index_type.upper(),
        SatelliteImage.cloud_coverage <= max_cloud,
        SatelliteImage.processing_status == ProcessingStatus.completed,
        SatelliteImage.image_date >= start,
        SatelliteImage.image_date <= end
    ).first()

    if not image:
        return JSONResponse(status_code=404, content={"error": "No image found for this date"})

    # Signed URL for map display
    signed_url = generate_signed_satellite_url(image.image_object_path)

    # Fetch TIFF bytes and compute histogram
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(signed_url)
        resp.raise_for_status()
        tiff_bytes = BytesIO(resp.content)

    with rasterio.open(tiff_bytes) as src:
        data = src.read(1).flatten()
    data = data[~np.isnan(data)]

    if len(data) == 0:
        histogram = []
        stats = None
    else:
        hist, edges = np.histogram(data, bins=bins)
        total = hist.sum()
        histogram = []
        for i in range(bins):
            from_val, to_val = edges[i], edges[i+1]
            value = int(hist[i])
            percentage = round((value / total) * 100, 3)
            if i < 2: color="hsl(0, 75%, 50%)"
            elif i < 4: color="hsl(25, 90%, 50%)"
            elif i < 6: color="hsl(45, 95%, 50%)"
            elif i < 8: color="hsl(90, 70%, 45%)"
            else: color="hsl(130, 65%, 40%)"
            histogram.append({
                "range": f"{from_val:.3f}",
                "fullRange": f"{from_val:.3f} - {to_val:.3f}",
                "value": value,
                "percentage": percentage,
                "color": color
            })

        weighted_sum = sum((i*0.1+0.05)*h for i,h in enumerate(hist))
        mean = weighted_sum / total if total else 0
        if mean < 0.2: health = "critical"; label="Crítico"
        elif mean < 0.35: health="poor"; label="Deficiente"
        elif mean < 0.5: health="moderate"; label="Moderado"
        elif mean < 0.7: health="good"; label="Bueno"
        else: health="excellent"; label="Excelente"

        stats = {
            "data": histogram,
            "mean": round(mean,3),
            "mode": int(np.argmax(hist)),
            "total": int(total),
            "health": health,
            "healthLabel": label
        }

    return JSONResponse(content={
        "image_id": str(image.id),  
        "image_url": signed_url,
        "histogram": histogram,
        "stats": stats,
        "bounds": parcel_bounds
    })

@router.get("/parcel/{parcel_id}/histogram")
async def parcel_histogram(
    parcel_id: str,
    geometry: str = Query(..., description="GeoJSON Feature as string"),
    index_type: str = Query(...),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    max_cloud: float = Query(100.0, description="Maximum cloud coverage allowed, 0-100"),
    bins: int = 10,
    db: Session = Depends(get_db),
):
    """
    Return satellite image URL + histogram + stats for a parcel, index type, and date.
    """
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format. Use YYYY-MM-DD"})

    start = datetime.combine(date_obj, datetime.min.time())
    end = datetime.combine(date_obj, datetime.max.time())

    # Get parcel geometry
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        return JSONResponse(status_code=404, content={"error": "Parcel not found"})

    # Compute parcel bounding box from geometry
    import shapely.geometry
    import shapely.ops

    try:
        geojson = parcel.geometry
        shapes = [shapely.geometry.shape(f["geometry"]) for f in geojson.get("features", [])]
        if shapes:
            merged = shapely.ops.unary_union(shapes)
            minx, miny, maxx, maxy = merged.bounds  # lon/lat
            parcel_bounds = {
                "west": minx,
                "south": miny,
                "east": maxx,
                "north": maxy
            }
        else:
            parcel_bounds = None
    except Exception as e:
        print("Error parsing parcel geometry:", e)
        parcel_bounds = None

    # Filter images by parcel, index_type, date, completed status
    image = db.query(SatelliteImage).filter(
        SatelliteImage.parcel_id == parcel_id,
        SatelliteImage.index_type == index_type.upper(),
        SatelliteImage.cloud_coverage <= max_cloud,
        SatelliteImage.processing_status == ProcessingStatus.completed,
        SatelliteImage.image_date >= start,
        SatelliteImage.image_date <= end
    ).first()

    if not image:
        return JSONResponse(status_code=404, content={"error": "No image found for this date"})

    # Signed URL for map display
    signed_url = generate_signed_satellite_url(image.image_object_path)

    # Fetch TIFF bytes and compute histogram
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(signed_url)
        resp.raise_for_status()
        tiff_bytes = BytesIO(resp.content)
    
    with rasterio.open(tiff_bytes) as src:
        try:
            geometry_feature = json.loads(geometry)
            selected_geom = shape(geometry_feature["geometry"])

            # Reproject geometry from WGS84 → raster CRS
            if src.crs and src.crs.to_string() != "EPSG:4326":
                transformer = Transformer.from_crs(
                    "EPSG:4326",
                    src.crs,
                    always_xy=True
                )
                selected_geom = transform(transformer.transform, selected_geom)

        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid geometry", "detail": str(e)}
            )

        masked, _ = mask(
            src,
            [selected_geom],
            crop=False,
            filled=False
        )

    data = masked[0].compressed()

    if len(data) == 0:
        histogram = []
        stats = None
    else:
        hist, edges = np.histogram(data, bins=bins)
        total = hist.sum()
        histogram = []
        for i in range(bins):
            from_val, to_val = edges[i], edges[i+1]
            value = int(hist[i])
            percentage = round((value / total) * 100, 3)
            if i < 2: color="hsl(0, 75%, 50%)"
            elif i < 4: color="hsl(25, 90%, 50%)"
            elif i < 6: color="hsl(45, 95%, 50%)"
            elif i < 8: color="hsl(90, 70%, 45%)"
            else: color="hsl(130, 65%, 40%)"
            histogram.append({
                "range": f"{from_val:.3f}",
                "fullRange": f"{from_val:.3f} - {to_val:.3f}",
                "value": value,
                "percentage": percentage,
                "color": color
            })

        weighted_sum = sum((i*0.1+0.05)*h for i,h in enumerate(hist))
        mean = weighted_sum / total if total else 0
        if mean < 0.2: health = "critical"; label="Crítico"
        elif mean < 0.35: health="poor"; label="Deficiente"
        elif mean < 0.5: health="moderate"; label="Moderado"
        elif mean < 0.7: health="good"; label="Bueno"
        else: health="excellent"; label="Excelente"

        stats = {
            "data": histogram,
            "mean": round(mean,3),
            "mode": int(np.argmax(hist)),
            "total": int(total),
            "health": health,
            "healthLabel": label
        }

    return JSONResponse(content={
        "image_id": str(image.id),  
        "image_url": signed_url,
        "histogram": histogram,
        "stats": stats,
        "bounds": parcel_bounds
    })

@router.get("/parcel/{parcel_id}/overlay")
async def parcel_overlay(signed_url: str):
    # 2. Fetch TIFF bytes
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(signed_url)
        resp.raise_for_status()
        tiff_bytes = BytesIO(resp.content)

    # 3. Read index band
    with rasterio.open(tiff_bytes) as src:
        index = src.read(1)
    
    # 4. Normalize -1..1 to 0..1
    index_norm = (index + 1) / 2
    index_norm = np.clip(index_norm, 0, 1)

    # 5. Apply colormap
    cmap = cm.get_cmap("RdYlGn")
    index_rgba = cmap(index_norm)  # RGBA floats 0..1
    index_rgba = (index_rgba * 255).astype(np.uint8)

    # 6. Convert to PNG bytes
    image = Image.fromarray(index_rgba)
    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")

@router.get("/parcel/{parcel_id}/overlay_indi")
async def parcel_overlay_indi(parcel_id: str, signed_url: str = Query(...), geometry: str = Query(...)):
    """
    Return a PNG of the satellite index masked to the parcel polygon.
    """
    # 1. Parse geometry
    try:
        geom_json = json.loads(geometry)
        parcel_geom = shape(geom_json["geometry"])
    except Exception as e:
        return {"error": "Invalid geometry", "detail": str(e)}

    # 2. Fetch TIFF bytes
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(signed_url)
        resp.raise_for_status()
        tiff_bytes = BytesIO(resp.content)

    # 3. Read raster and mask to parcel
    with rasterio.open(tiff_bytes) as src:
        # Reproject geometry if raster CRS is not WGS84
        if src.crs and src.crs.to_string() != "EPSG:4326":
            transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            from shapely.ops import transform
            parcel_geom = transform(transformer.transform, parcel_geom)

        masked, _ = mask(src, [parcel_geom], crop=False, filled=False)
        band = masked[0]

    # 4. Normalize -1..1 to 0..1
    index_norm = (band + 1) / 2
    index_norm = np.clip(index_norm, 0, 1)

    # 5. Apply colormap
    cmap = cm.get_cmap("RdYlGn")
    index_rgba = cmap(index_norm)  # RGBA floats 0..1
    index_rgba = (index_rgba * 255).astype(np.uint8)

    # 6. Apply transparency outside polygon
    if np.ma.is_masked(band):
        index_rgba[..., 3] = np.where(band.mask, 0, 255)

    # 7. Convert to PNG bytes
    image = Image.fromarray(index_rgba)
    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")