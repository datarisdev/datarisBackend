# app/processors/satellite/sentinel_processor.py
import logging
import geopandas as gpd
from datetime import date, datetime, timedelta
from pystac_client import Client
import planetary_computer
import rasterio
from shapely.geometry import mapping
from rasterio.warp import reproject, Resampling
import numpy as np

from app.db.session import SessionLocal
from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.utils.geo import clip_raster
from app.services.satellite.indices import INDEX_BANDS, IndexType, compute_index, ndvi, savi, ndmi
from app.services.satellite.utils import compute_stats, featurecollection_to_geometry
from app.utils.storage_satellite import upload_satellite_tif

logging.basicConfig(level=logging.INFO)

def process_sentinel_for_parcel(
    *,
    parcel_geometry: dict,
    user_id: str,
    parcel_id: str,
    start_date: date,
    end_date: date,
    indices: list[str],
    max_cloud: float | None = None,
):
    geom = featurecollection_to_geometry(parcel_geometry)

    # 1️⃣ Load geometry
    gdf = gpd.GeoDataFrame(
        geometry=[geom], crs="EPSG:4326"
    )

    bounds = gdf.total_bounds.tolist()

    # 2️⃣ STAC search
    client = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1"
    )

    query = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bounds,
        "datetime": f"{start_date}/{end_date}",
    }

    if max_cloud is not None:
        query["query"] = {"eo:cloud_cover": {"lt": max_cloud}}

    items = list(client.search(**query).get_items())

    results = []

    for item in items:
        try:
            item = planetary_computer.sign(item)

            image_date = item.properties["datetime"][:10]
            cloud = item.properties["eo:cloud_cover"]

            red_url = item.assets["B04"].href
            nir_url = item.assets["B08"].href
            swir_url = item.assets["B11"].href

            # CRS alignment
            import rasterio
            with rasterio.open(red_url) as src:
                gdf_proj = gdf.to_crs(src.crs)
                shapes = [mapping(g) for g in gdf_proj.geometry]

            red, meta = clip_raster(red_url, shapes)
            nir, _ = clip_raster(nir_url, shapes)
            swir_raw, swir_meta = clip_raster(swir_url, shapes)

            swir = np.zeros_like(red, dtype="float32")
            reproject(
                swir_raw,
                swir,
                src_transform=swir_meta["transform"],
                src_crs=swir_meta["crs"],
                dst_transform=meta["transform"],
                dst_crs=meta["crs"],
                resampling=Resampling.bilinear,
            )

            index_outputs = {}

            if "NDVI" in indices:
                arr = ndvi(nir, red)
                path = upload_satellite_tif(
                    arr, meta, user_id, parcel_id, "NDVI", image_date
                )
                index_outputs["NDVI"] = {
                    "path": path,
                    "stats": compute_stats(arr),
                }

            if "SAVI" in indices:
                arr = savi(nir, red)
                path = upload_satellite_tif(
                    arr, meta, user_id, parcel_id, "SAVI", image_date
                )
                index_outputs["SAVI"] = {
                    "path": path,
                    "stats": compute_stats(arr),
                }

            if "NDMI" in indices:
                arr = ndmi(nir, swir)
                path = upload_satellite_tif(
                    arr, meta, user_id, parcel_id, "NDMI", image_date
                )
                index_outputs["NDMI"] = {
                    "path": path,
                    "stats": compute_stats(arr),
                }

            results.append({
                "image_date": image_date,
                "cloud": cloud,
                "bounds": bounds,
                "indices": index_outputs,
            })

        except Exception as e:
            print(f"⚠️ Failed {item.id}: {e}")
            continue

    return results

def process_sentinel_for_parcel_day_old(
    *,
    parcel_geometry: dict,
    user_id: str,
    parcel_id: str,
    day: date,
    indices: list[str],
    max_cloud: float | None = None,
    db_session,
) -> list[dict]:
    """
    Daily Sentinel-2 processor.
    - If no images exist for a day → silently skip
    - Each index has its own DB lifecycle
    """

    # ----------------------------
    # 1️⃣ Geometry
    # ----------------------------
    geom = featurecollection_to_geometry(parcel_geometry)

    gdf = gpd.GeoDataFrame(
        geometry=[geom],
        crs="EPSG:4326",
    )

    bounds = gdf.total_bounds.tolist()

    # ----------------------------
    # 2️⃣ STAC search (1 day)
    # ----------------------------
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()

    client = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1"
    )

    query = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bounds,
        "datetime": f"{start}/{end}",
    }

    if max_cloud is not None:
        query["query"] = {"eo:cloud_cover": {"lt": max_cloud}}

    items = list(client.search(**query).items())

    # ✅ No images for this day → expected behavior
    if not items:
        logging.info(f"No images found for day {day}")
        return []

    results: list[dict] = []

    # ----------------------------
    # 3️⃣ Process each scene
    # ----------------------------
    for item in items:
        item = planetary_computer.sign(item)

        image_date_str = item.properties["datetime"][:10]
        image_date = datetime.fromisoformat(image_date_str)
        cloud = item.properties.get("eo:cloud_cover")

        # ----------------------------
        # Skip if already processed
        # ----------------------------
        already_done = (
            db_session.query(SatelliteImage)
            .filter(
                SatelliteImage.parcel_id == parcel_id,
                SatelliteImage.image_date == image_date,
                SatelliteImage.index_type.in_(indices),
            )
            .first()
        )

        if already_done:
            continue

        try:
            # ----------------------------
            # Load bands
            # ----------------------------
            red_url = item.assets["B04"].href
            nir_url = item.assets["B08"].href
            swir_url = item.assets["B11"].href

            with rasterio.open(red_url) as src:
                gdf_proj = gdf.to_crs(src.crs)
                shapes = [mapping(g) for g in gdf_proj.geometry]

            red, meta = clip_raster(red_url, shapes)
            nir, _ = clip_raster(nir_url, shapes)
            swir_raw, swir_meta = clip_raster(swir_url, shapes)

            # ----------------------------
            # Resample SWIR → 10m
            # ----------------------------
            swir = np.zeros_like(red, dtype="float32")

            reproject(
                swir_raw,
                swir,
                src_transform=swir_meta["transform"],
                src_crs=swir_meta["crs"],
                dst_transform=meta["transform"],
                dst_crs=meta["crs"],
                resampling=Resampling.bilinear,
            )

        except Exception as e:
            # ❌ Scene-level failure (rare, but possible)
            print(f"⚠️ Scene load failed {item.id}: {e}")
            continue

        # ----------------------------
        # 4️⃣ Compute indices
        # ----------------------------
        for index_type in indices:

            db_obj = SatelliteImage(
                user_id=user_id,
                parcel_id=parcel_id,
                image_date=image_date,
                index_type=index_type,
                image_object_path="",
                processing_status=ProcessingStatus.processing,
                cloud_coverage=cloud,
                bounds=bounds,
            )

            db_session.add(db_obj)
            db_session.commit()
            db_session.refresh(db_obj)

            try:
                if index_type == "NDVI":
                    arr = ndvi(nir, red)
                elif index_type == "SAVI":
                    arr = savi(nir, red)
                elif index_type == "NDMI":
                    arr = ndmi(nir, swir)
                else:
                    db_obj.processing_status = ProcessingStatus.failed
                    db_session.commit()
                    continue

                stats = compute_stats(arr)

                path = upload_satellite_tif(
                    arr,
                    meta,
                    user_id,
                    parcel_id,
                    index_type,
                    image_date_str,
                )

                db_obj.image_object_path = path
                db_obj.statistics = stats
                db_obj.processing_status = ProcessingStatus.completed
                db_session.commit()

                results.append({
                    "image_date": image_date_str,
                    "index": index_type,
                    "path": path,
                    "stats": stats,
                    "cloud": cloud,
                })

            except Exception as e:
                db_session.rollback()
                db_obj.processing_status = ProcessingStatus.failed
                db_session.commit()
                print(f"⚠️ Index {index_type} failed {item.id}: {e}")

    return results

def process_sentinel_for_parcel_day(
    *,
    parcel_geometry: dict,
    user_id: str,
    parcel_id: str,
    day: date,
    indices: list[str],
    max_cloud: float | None = None,
    db_session,
) -> list[dict]:
    """
    Daily Sentinel-2 processor.
    - If no images exist for a day → silently skip
    - Each index has its own DB lifecycle
    """

    # ----------------------------
    # 1️⃣ Geometry
    # ----------------------------
    geom = featurecollection_to_geometry(parcel_geometry)

    gdf = gpd.GeoDataFrame(
        geometry=[geom],
        crs="EPSG:4326",
    )

    bounds = gdf.total_bounds.tolist()

    # ----------------------------
    # 2️⃣ STAC search (1 day)
    # ----------------------------
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()

    client = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1"
    )

    query = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bounds,
        "datetime": f"{start}/{end}",
    }

    if max_cloud is not None:
        query["query"] = {"eo:cloud_cover": {"lt": max_cloud}}

    items = list(client.search(**query).items())

    if not items:
        logging.info(f"No images found for day {day}")
        return []

    results: list[dict] = []

    # ----------------------------
    # 3️⃣ Resolve required bands
    # ----------------------------
    index_enums = [IndexType(i) for i in indices]

    required_bands: set[str] = set()
    for idx in index_enums:
        required_bands |= set(INDEX_BANDS[idx])

    # ----------------------------
    # 4️⃣ Process each scene
    # ----------------------------
    for item in items:
        item = planetary_computer.sign(item)

        image_date_str = item.properties["datetime"][:10]
        image_date = datetime.fromisoformat(image_date_str)
        cloud = item.properties.get("eo:cloud_cover")

        # ----------------------------
        # Skip if already processed
        # ----------------------------
        already_done = (
            db_session.query(SatelliteImage)
            .filter(
                SatelliteImage.parcel_id == parcel_id,
                SatelliteImage.image_date == image_date,
                SatelliteImage.index_type.in_(indices),
            )
            .first()
        )

        if already_done:
            continue

        try:
            # ----------------------------
            # 5️⃣ Load & clip bands
            # ----------------------------
            band_arrays: dict[str, np.ndarray] = {}
            band_metas: dict[str, dict] = {}

            # Open one 10m band to get CRS
            ref_band_url = item.assets["B04"].href

            with rasterio.open(ref_band_url) as src:
                gdf_proj = gdf.to_crs(src.crs)
                shapes = [mapping(g) for g in gdf_proj.geometry]

            for band in required_bands:
                asset = item.assets.get(band)
                if asset is None:
                    raise ValueError(f"Band {band} missing in item {item.id}")

                arr, meta = clip_raster(asset.href, shapes)
                band_arrays[band] = arr
                band_metas[band] = meta

            # ----------------------------
            # 6️⃣ Resample non-10m bands
            # ----------------------------
            ref_meta = band_metas["B04"]
            ref_shape = band_arrays["B04"].shape

            for band in required_bands:
                if band in ["B11", "B05"]:  # 20m bands
                    resampled = np.zeros(ref_shape, dtype="float32")

                    reproject(
                        band_arrays[band],
                        resampled,
                        src_transform=band_metas[band]["transform"],
                        src_crs=band_metas[band]["crs"],
                        dst_transform=ref_meta["transform"],
                        dst_crs=ref_meta["crs"],
                        resampling=Resampling.bilinear,
                    )

                    band_arrays[band] = resampled

        except Exception as e:
            logging.warning(f"⚠️ Scene load failed {item.id}: {e}")
            continue

        # ----------------------------
        # 7️⃣ Compute each index
        # ----------------------------
        for idx in index_enums:
            index_type = idx.value

            db_obj = SatelliteImage(
                user_id=user_id,
                parcel_id=parcel_id,
                image_date=image_date,
                index_type=index_type,
                image_object_path="",
                processing_status=ProcessingStatus.processing,
                cloud_coverage=cloud,
                bounds=bounds,
            )

            db_session.add(db_obj)
            db_session.commit()
            db_session.refresh(db_obj)

            try:
                arr = compute_index(idx, band_arrays)

                stats = compute_stats(arr)

                path = upload_satellite_tif(
                    arr,
                    ref_meta,
                    user_id,
                    parcel_id,
                    index_type,
                    image_date_str,
                )

                db_obj.image_object_path = path
                db_obj.statistics = stats
                db_obj.processing_status = ProcessingStatus.completed
                db_session.commit()

                results.append(
                    {
                        "image_date": image_date_str,
                        "index": index_type,
                        "path": path,
                        "stats": stats,
                        "cloud": cloud,
                    }
                )

            except Exception as e:
                db_session.rollback()
                db_obj.processing_status = ProcessingStatus.failed
                db_session.commit()
                logging.warning(
                    f"⚠️ Index {index_type} failed for {item.id}: {e}"
                )

    return results

def process_sentinel_range_task(
    *,
    parcel_geometry: dict,
    user_id: str,
    parcel_id: str,
    start_date: date,
    end_date: date,
    indices: list[str],
    max_cloud: float | None,
):
    db = SessionLocal()
    try:
        day = start_date
        while day <= end_date:
            logging.info(f"Processing day: {day}")
            process_sentinel_for_parcel_day(
                parcel_geometry=parcel_geometry,
                user_id=user_id,
                parcel_id=parcel_id,
                day=day,
                indices=indices,
                max_cloud=max_cloud,
                db_session=db,
            )
            day += timedelta(days=1)
            logging.info(f"Day: {day} - Processed")
    finally:
        db.close()

