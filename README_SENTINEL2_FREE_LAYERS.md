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

1. En Azure Blob Storage si está configurado `Azure Blob Storage_SERVICE_ACCOUNT_JSON` o `AZURE_STORAGE_ACCOUNT_URL` y `Azure Blob Storage_SATELLITE_BUCKET_NAME`.
2. En cache local temporal si Azure Blob Storage no está configurado.

Para producción se recomienda Azure Blob Storage:

```env
Azure Blob Storage_SATELLITE_BUCKET_NAME=dataris-satellite
SENTINEL_LOCAL_CACHE_DIR=/tmp/dataris_sentinel2_cache
SENTINEL_DEFAULT_MAX_CLOUD=80
SENTINEL_DATE_LOOKBACK_DAYS=180
SENTINEL_MAP_LOOKBACK_DAYS=90
```

## Notas importantes

- No se agregó una migración porque se reutiliza la tabla existente `satellite_images`.
- El primer cálculo de una capa puede tardar según tamaño del lote y velocidad del STAC. Después queda en cache y carga como PNG.
- El frontend llama primero al cache de memoria/localStorage y luego al backend.

## Fix producción Azure Container Apps 500 en map-layer

Si el navegador muestra `HTTP/2 500` en `/api/satellite-free/.../ndvi/map-layer`, esta versión corrige los dos casos más comunes:

1. **Earth Search devuelve assets `s3://...`**. En Azure Container Apps, GDAL/Rasterio puede intentar leerlos con firma AWS y fallar si no hay credenciales. Ahora los assets `s3://sentinel-cogs/...` se normalizan a HTTPS público y se fuerza lectura anónima.
2. **Cache DB incompatible**. Si la tabla `satellite_images` no tiene exactamente las columnas esperadas o no tienes migraciones al día, el nuevo motor no bloquea la imagen. Por defecto `SENTINEL_DB_CACHE_ENABLED=false` y se usa cache local/Azure Blob Storage.

Variables recomendadas:

```env
SENTINEL_STAC_URL=https://earth-search.aws.element84.com/v1
SENTINEL_STAC_COLLECTIONS=sentinel-2-l2a
SENTINEL_STAC_PROVIDER=earthsearch
SENTINEL_DEFAULT_MAX_CLOUD=100
SENTINEL_DATE_LOOKBACK_DAYS=180
SENTINEL_MAP_LOOKBACK_DAYS=120
SENTINEL_DB_CACHE_ENABLED=false
AWS_NO_SIGN_REQUEST=YES
SENTINEL_AWS_REGION=us-west-2
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
CPL_VSIL_CURL_USE_HEAD=NO
```

Para cache persistente en producción, configura `Azure Blob Storage_SATELLITE_BUCKET_NAME` y credenciales de Azure Blob Storage. Si no configuras Azure Blob Storage, funciona con cache local, pero Azure Container Apps puede perderlo al reiniciar instancia.
