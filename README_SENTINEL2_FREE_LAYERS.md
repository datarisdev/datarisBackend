# Integración Sentinel-2 gratuita para capas agrícolas

Esta entrega agrega una alternativa propia a Graniot para el módulo satelital.

## Qué agrega

- Router FastAPI: `app/api/routers/sentinel2.py`
- Servicios nuevos: `app/services/sentinel2/`
- Endpoints compatibles con el frontend:
  - `GET /api/satellite-free/layers`
  - `GET /api/satellite-free/parcels/{parcel_id}/resolutions/{resolution_key}/dates`
  - `GET /api/satellite-free/parcels/{parcel_id}/layers/{layer_key}/statistics`
  - `GET /api/satellite-free/parcels/{parcel_id}/ndvi/map-layer`
  - `POST /api/satellite-free/satellite/prefetch`
  - `GET /api/satellite-free/cache/{cache_key}.png`

## Índices incluidos

- NDVI
- GNDVI
- NDRE
- SAVI
- MSAVI2
- OSAVI
- EVI
- EVI2
- NDMI
- NDWI
- NBR
- GCI
- CI_REDEDGE
- TRUE_COLOR
- FALSE_COLOR

## Fuente de datos por defecto

Por defecto usa STAC público de Earth Search con Sentinel-2 L2A COG:

```env
SENTINEL_STAC_URL=https://earth-search.aws.element84.com/v1
SENTINEL_STAC_COLLECTIONS=sentinel-2-l2a
SENTINEL_STAC_PROVIDER=earthsearch
```

Esto no requiere credenciales para comenzar. Si quieres usar otro STAC compatible, puedes cambiar esas variables. Para Planetary Computer usa:

```env
SENTINEL_STAC_URL=https://planetarycomputer.microsoft.com/api/stac/v1
SENTINEL_STAC_COLLECTIONS=sentinel-2-l2a
SENTINEL_STAC_PROVIDER=planetary_computer
```

## Cache

El backend guarda PNGs procesados de dos formas:

1. En Google Cloud Storage si está configurado `GCS_SERVICE_ACCOUNT_JSON` o `GOOGLE_APPLICATION_CREDENTIALS` y `GCS_SATELLITE_BUCKET_NAME`.
2. En cache local temporal si GCS no está configurado.

Para producción se recomienda GCS:

```env
GCS_SATELLITE_BUCKET_NAME=dataris-satellite
SENTINEL_LOCAL_CACHE_DIR=/tmp/dataris_sentinel2_cache
SENTINEL_DEFAULT_MAX_CLOUD=80
SENTINEL_DATE_LOOKBACK_DAYS=180
SENTINEL_MAP_LOOKBACK_DAYS=90
```

## Notas importantes

- No se agregó una migración porque se reutiliza la tabla existente `satellite_images`.
- El primer cálculo de una capa puede tardar según tamaño del lote y velocidad del STAC. Después queda en cache y carga como PNG.
- El frontend llama primero al cache de memoria/localStorage y luego al backend.
