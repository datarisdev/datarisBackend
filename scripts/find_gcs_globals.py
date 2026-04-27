"""
Script auxiliar para encontrar inicializaciones peligrosas de Google Cloud Storage.

Ejecutar desde la raíz del backend:
    python scripts/find_gcs_globals.py

Busca patrones como:
    storage.Client()
    Client()

Revisa especialmente si aparecen fuera de funciones/clases.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    "storage.Client(",
    "Client()",
    "Client(project=",
    "from google.cloud import storage",
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)

def main() -> None:
    found = False

    for path in ROOT.rglob("*.py"):
        if should_skip(path):
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()

        for idx, line in enumerate(lines, start=1):
            if any(pattern in line for pattern in PATTERNS):
                found = True
                rel = path.relative_to(ROOT)
                print(f"{rel}:{idx}: {line.strip()}")

    if not found:
        print("No se encontraron patrones directos de Google Cloud Storage.")

if __name__ == "__main__":
    main()
