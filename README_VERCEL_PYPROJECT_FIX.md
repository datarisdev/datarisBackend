# Corrección Vercel / setuptools

Este parche corrige el error:

```
Multiple top-level packages discovered in a flat-layout: ['api', 'app', 'alembic']
```

## Archivos corregidos

- `pyproject.toml`: ahora limita el descubrimiento de paquetes a `app*` y `api*`, excluyendo `alembic*`.
- `vercel.json`: usa rewrites hacia `/api/index` sin mezclar `builds` y `functions`.
- `api/index.py`: expone la app FastAPI para Vercel.

## Después de copiar

```bash
git add pyproject.toml vercel.json api/index.py api/__init__.py README_VERCEL_PYPROJECT_FIX.md
git commit -m "fix vercel setuptools package discovery"
git push origin main
```

Luego en Vercel haz redeploy sin cache.
