# Fix Sentinel-2: lotes desde compat storage

Este build corrige el error de Azure Container Apps:

```text
(psycopg2.errors.UndefinedTable) relation "parcels" does not exist
```

El proyecto Dataris actual no guarda los lotes del frontend en una tabla SQL normalizada `parcels`; los guarda dentro del estado compatible `/api/compat` (`dataris_compat_state`). Por eso el router Sentinel-2 no debe consultar `app.models.Parcel` directamente.

## Cambio aplicado

`app/api/routers/sentinel2.py` ahora obtiene los lotes desde:

```python
from app.api.routers import compat as compat_store
compat_db = compat_store.read_db()
parcels = compat_store.table(compat_db, "parcels")
```

También acepta geometría guardada como:

- `geometry`
- `geometry_geojson`
- `geojson`
- `feature_collection`
- `featureCollection`

## Variables recomendadas en Azure Container Apps

```bash
# Configurado por Terraform en datarisInfra. Variables relevantes: BACKEND_CORS_ORIGINS=https://app.dataris.es,SENTINEL_STAC_URL=https://earth-search.aws.element84.com/v1,SENTINEL_STAC_COLLECTIONS=sentinel-2-l2a,SENTINEL_STAC_PROVIDER=earthsearch,SENTINEL_DEFAULT_MAX_CLOUD=100,SENTINEL_DATE_LOOKBACK_DAYS=180,SENTINEL_MAP_LOOKBACK_DAYS=120,SENTINEL_DB_CACHE_ENABLED=false,AWS_NO_SIGN_REQUEST=YES,SENTINEL_AWS_REGION=us-west-2,GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR,CPL_VSIL_CURL_USE_HEAD=NO
```

No necesitas API key para Earth Search. Azure Blob Storage es opcional para cache persistente.
