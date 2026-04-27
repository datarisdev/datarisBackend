# Corrección de deploy en Vercel para FastAPI

Este parche corrige el error:

```txt
The `functions` property cannot be used in conjunction with the `builds` property.
```

## Archivos corregidos

```txt
vercel.json
api/index.py
api/__init__.py
```

## Configuración correcta

El proyecto ya no usa `builds`. Ahora Vercel ejecuta FastAPI desde:

```txt
api/index.py
```

Ese archivo importa la instancia real:

```py
from app.main import app
```

## Qué debes hacer

1. Copia estos archivos al repositorio del backend.
2. Haz commit y push a `main`:

```bash
git add vercel.json api/index.py api/__init__.py README_VERCEL_DEPLOY_CONFIG_FIX.md
git commit -m "fix vercel fastapi deployment config"
git push origin main
```

3. En Vercel, haz redeploy sin cache:

```txt
dataris-backend → Deployments → Redeploy → Redeploy without build cache
```

4. Prueba:

```txt
https://dataris-backend.vercel.app/
https://dataris-backend.vercel.app/health
```

## Variables necesarias en Vercel

Asegúrate de tenerlas en el proyecto `dataris-backend`, no solo en Team Variables:

```env
JWT_SECRET_KEY=pon_una_clave_larga_y_segura
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME
REDIS_URL=redis://localhost:6379/0
BACKEND_CORS_ORIGINS=*
DATARIS_COMPAT_STORAGE_DIR=/tmp/dataris_compat_storage
```

Si usas Google Cloud Storage:

```env
GCS_BUCKET_NAME=tu_bucket
GOOGLE_CLOUD_PROJECT=tu_project_id
GCS_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

No subas credenciales reales al repositorio.
