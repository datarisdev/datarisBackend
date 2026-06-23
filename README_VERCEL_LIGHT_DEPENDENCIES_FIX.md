# Fix Vercel: dependencias ligeras + imports seguros

Este parche corrige el error:

```txt
Total bundle size exceeds Lambda ephemeral storage limit (500 MB)
```

La causa era que `pyproject.toml` instalaba paquetes muy pesados en Vercel:

```txt
rasterio, geopandas, pandas, numpy, matplotlib, planetary-computer, pystac-client
```

Vercel no puede empaquetar ese stack geoespacial completo dentro del límite de Lambda.

## Qué cambia

- `pyproject.toml` queda con dependencias ligeras para producción en Vercel.
- `requirements.txt` queda ligero.
- `requirements-local.txt` conserva las dependencias pesadas para local/VM/Docker.
- `app/main.py` carga routers opcionales de forma segura.
- `compat.py` ya no importa procesamiento pesado al iniciar.
- `parcel_upload.py` y `helicopter_processor.py` usan `pyshp + shapely + pyproj` en lugar de `geopandas/pandas/rasterio`.

## Importante

El procesamiento satelital completo con rasterio/geopandas debe correr en VM/Docker/Azure Container Apps, no en Vercel.
El login, compat, carga básica de parcelas y análisis de helicóptero ligero sí quedan preparados para Vercel.
