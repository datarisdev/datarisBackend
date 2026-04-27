# app/core/satellite_config.py

from datetime import date, timedelta

SATELLITE_YEARS_BACK = 1

SATELLITE_INDICES = [
    "NDVI",
    "NDMI",
    "LCI", 
    "LAI", 
    "LCC", 
    "CWC"
    #"SAVI",
]

# Avoid downloading totally useless scenes
SATELLITE_MAX_CLOUD = 80.0  # %
