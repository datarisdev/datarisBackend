# Sentinel-2 NDVI default, exact crop and progressive prefetch

This delivery updates the free Sentinel-2 integration used by `/api/satellite-free`.

## Included fixes

1. **NDVI as the default layer**
   - `/api/satellite-free/layers` already marks NDVI with `is_init_visible=true` and priority 10.
   - The frontend now also prioritizes the `NDVI` key explicitly.

2. **Exact crop by selected lot geometry**
   - The frontend sends the exact GeoJSON currently rendered for the selected lot to `POST /api/satellite-free/parcels/{parcel_id}/ndvi/map-layer`.
   - The backend no longer depends only on compat-storage geometry for raster generation when the frontend provides a geometry override.
   - The backend accepts Polygon, MultiPolygon, Feature, FeatureCollection, GeometryCollection and arrays of geometries.
   - Invalid rings are repaired with Shapely `make_valid` when available, with `buffer(0)` fallback.
   - A final `rasterio.features.geometry_mask` is applied after band resampling so pixels outside the real lot are transparent.
   - Local/GCS cache keys include a geometry fingerprint so stale rasters from older geometry are not reused.

3. **Progressive backend prefetch**
   - `POST /api/satellite-free/satellite/prefetch` now queues a background job by default.
   - It can receive exact parcel geometries from the frontend.
   - It generates NDVI first, then the requested layers gradually with a small delay.
   - This avoids blocking the page while still warming local/GCS cache for future visits.

## Recommended Cloud Run env vars

```bash
gcloud run services update dataris-api-staging \
  --region us-central1 \
  --update-env-vars BACKEND_CORS_ORIGINS=https://app.dataris.es,SENTINEL_STAC_URL=https://earth-search.aws.element84.com/v1,SENTINEL_STAC_COLLECTIONS=sentinel-2-l2a,SENTINEL_STAC_PROVIDER=earthsearch,SENTINEL_DEFAULT_MAX_CLOUD=100,SENTINEL_DATE_LOOKBACK_DAYS=180,SENTINEL_MAP_LOOKBACK_DAYS=120,SENTINEL_DB_CACHE_ENABLED=false,SENTINEL_MASK_ALL_TOUCHED=true,AWS_NO_SIGN_REQUEST=YES,SENTINEL_AWS_REGION=us-west-2,GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR,CPL_VSIL_CURL_USE_HEAD=NO
```

`GCS_SATELLITE_BUCKET_NAME` and Google credentials are optional but recommended for persistent cache.
