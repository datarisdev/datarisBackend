# Integración Backend DATARIS ↔ Frontend Vite

Se añadió el router:

```txt
app/api/routers/compat.py
```

y se registra en:

```txt
app/main.py
```

Este router expone endpoints bajo:

```txt
/api/compat
```

para mantener compatible el frontend que antes usaba Supabase:

- Auth: `/api/compat/auth/sign-in`, `/sign-up`, `/me`, `/update-user`
- Tablas: `/api/compat/tables/{table}/query|insert|update|delete|upsert`
- Storage: `/api/compat/storage/...`
- Funciones: `/api/compat/functions/sentinel-hub`, `/process-parcel-images`
- RPC: `/api/compat/rpc/get_analysis_filter_options`, `/check_duplicate_analysis`

## Ejecutar backend

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Variables recomendadas

```env
JWT_SECRET_KEY=cambia_esta_clave_por_una_muy_segura
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:55433/datarisdb
BACKEND_CORS_ORIGINS=*
DATARIS_COMPAT_STORAGE_DIR=app/storage
```

## Usuario inicial

Al iniciar, el router de compatibilidad crea datos locales en `app/storage/compat_db.json`.

```txt
Email: admin@dataris.local
Password: admin123456
```

## Nota técnica

Los routers nativos existentes del backend se conservan. La capa `/api/compat` permite que el frontend actual funcione de inmediato contra FastAPI, incluso para tablas que todavía no están modeladas en SQLAlchemy.
