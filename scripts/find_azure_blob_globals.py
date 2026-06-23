#!/usr/bin/env python3
"""Fail when legacy Google Cloud Storage imports remain in application code.

Usage:
    python scripts/find_azure_blob_globals.py
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "node_modules"}
FORBIDDEN_PATTERNS = (
    "from google.cloud import storage",
    "google.cloud.storage",
    "storage.Client(",
    "GCS_SERVICE_ACCOUNT_JSON",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def main() -> int:
    matches: list[tuple[Path, int, str]] = []
    current_script = Path(__file__).resolve()
    for path in ROOT.rglob("*.py"):
        if path.resolve() == current_script or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            if any(pattern in line for pattern in FORBIDDEN_PATTERNS):
                matches.append((path.relative_to(ROOT), line_number, line.strip()))

    if not matches:
        print("OK: no legacy Google Cloud Storage imports found in Python application code.")
        return 0

    print("ERROR: legacy Google Cloud Storage references found:")
    for path, line_number, line in matches:
        print(f"  {path}:{line_number}: {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
