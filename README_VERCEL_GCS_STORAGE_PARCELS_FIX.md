# Fix Vercel: GCS lazy loading + CORS compat

Este parche corrige el error de Vercel donde el backend falla al importar `app/main.py` por credenciales faltantes de Google Cloud Storage.

## Cambios incluidos

- `app/utils/storage_parcels.py` ya no ejecuta `storage.Client()` al importar.
- `app/utils/storage_satellite.py` ya no ejecuta `storage.Client()` al importar.
- Ambos módulos usan `app/utils/gcs.py`, que inicializa Google Cloud Storage de forma lazy.
- `app/main.py` soporta tanto `/api/compat/...` como `/compat/...`.
- `app/main.py` ajusta CORS para evitar problemas cuando `BACKEND_CORS_ORIGINS=*`.
- `vercel.json` queda sin `builds` y sin `functions` para evitar conflictos.
- `pyproject.toml` evita el error de setuptools por múltiples paquetes top-level.

## Después de copiar

```bash
git add app/main.py app/utils/gcs.py app/utils/storage_parcels.py app/utils/storage_satellite.py app/core/config.py vercel.json pyproject.toml api/index.py api/__init__.py README_VERCEL_GCS_STORAGE_PARCELS_FIX.md
git commit -m "fix vercel gcs lazy imports and cors"
git push origin main
```

Luego en Vercel usa `Redeploy without build cache`.
