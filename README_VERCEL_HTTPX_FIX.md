# Fix Vercel: dependencias faltantes

Corrige el error:

```txt
ModuleNotFoundError: No module named 'httpx'
```

También agrega dependencias que el router de imágenes satelitales importa al iniciar:

- `httpx`
- `matplotlib`
- `Pillow`

Archivos modificados:

- `requirements.txt`
- `pyproject.toml`

Después de subirlo:

```bash
git add requirements.txt pyproject.toml README_VERCEL_HTTPX_FIX.md
git commit -m "fix vercel python dependencies"
git push origin main
```

Luego en Vercel ejecuta **Redeploy without build cache**.
