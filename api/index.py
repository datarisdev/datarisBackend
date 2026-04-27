"""Vercel Python entrypoint for the Dataris FastAPI backend.

Vercel discovers Python serverless functions inside the /api folder.
This file imports the FastAPI instance from app.main and exposes it as `app`.
"""

from app.main import app
