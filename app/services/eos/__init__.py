"""EOS Data Analytics (EOSDA API Connect) satellite provider.

This package integrates https://api-connect.eos.com for the satellite module:
scene search, vegetation-index statistics (mt_stats) and index tile rendering.

The API key lives only in backend settings (``EOS_API_KEY``) / Azure Container
App secrets — it is never exposed to the frontend.
"""

from app.services.eos.client import EOSApiError, EOSNotConfigured, is_configured  # noqa: F401
