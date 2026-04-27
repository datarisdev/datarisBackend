# DATARIS Backend — despliegue en Vercel

## Variables mínimas en Vercel

Configura estas variables en Project Settings → Environment Variables y vuelve a hacer Redeploy:

```env
JWT_SECRET_KEY=pon_una_clave_larga_y_segura
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME
BACKEND_CORS_ORIGINS=https://TU_FRONTEND.vercel.app,http://localhost:5173,http://localhost:8080
DATARIS_COMPAT_STORAGE_DIR=/tmp/dataris_compat_storage
```

## Google Cloud Storage

Si vas a usar subida de avatares/logo/storage en Google Cloud Storage, agrega también:

```env
GCS_BUCKET_NAME=dataris-user-avatars
GCS_SERVICE_ACCOUNT_JSON={...json completo del service account...}
GOOGLE_CLOUD_PROJECT=tu-project-id
```

El JSON del service account debe pegarse como valor de la variable, no subirse al repositorio.

## Nota

El backend ahora crea el cliente de Google Cloud Storage de forma lazy. Por eso el proyecto ya no falla al arrancar si todavía no configuraste credenciales de Google; solo fallarán las rutas que realmente intenten usar GCS.
