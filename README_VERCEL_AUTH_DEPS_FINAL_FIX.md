# Fix final Vercel: dependencias ligeras + autenticación compat

Este parche corrige dos problemas comunes en Vercel:

1. Evita agregar librerías geoespaciales pesadas que exceden el límite de Lambda.
2. Corrige el error `Not authenticated` en `/compat/helicopter/analyze` cuando el backend usa almacenamiento temporal `/tmp` y la sesión queda apuntando a otro estado temporal.

## Dependencias de Vercel

El deploy ligero usa solo:

- fastapi
- python-jose
- passlib / bcrypt
- pydantic / pydantic-settings
- sqlalchemy / psycopg2-binary
- python-multipart
- google-cloud-storage
- requests / httpx
- shapely
- pyproj
- pyshp

No se incluyen `rasterio`, `geopandas`, `pandas`, `numpy` ni `matplotlib` en Vercel porque hacen que el bundle supere el límite de 500 MB.

## Nueva variable opcional

Por defecto, en Vercel se permite compatibilidad de carga si la sesión temporal no existe:

```env
DATARIS_COMPAT_ALLOW_GUEST_UPLOADS=true
```

Para producción más estricta puedes desactivarlo:

```env
DATARIS_COMPAT_ALLOW_GUEST_UPLOADS=false
```

## Después de aplicar

```bash
git add .
git commit -m "fix vercel auth and lightweight dependencies"
git push origin main
```

Luego en Vercel haz:

Deployments → Redeploy → Redeploy without build cache
