from __future__ import annotations

from typing import Any

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def _daily_value(data: dict[str, Any], key: str, index: int) -> Any:
    values = data.get("daily", {}).get(key) or []
    return values[index] if index < len(values) else None


def get_weather(lat: float, lon: float) -> dict[str, Any]:
    """Fetch current conditions and a five-day forecast from Open-Meteo."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "wind_speed_10m",
            "precipitation",
            "weather_code",
        ]),
        "daily": ",".join([
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "relative_humidity_2m_mean",
            "wind_speed_10m_max",
        ]),
        "forecast_days": 5,
        "timezone": "auto",
    }

    response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    current = data.get("current") or {}
    daily_times = data.get("daily", {}).get("time") or []

    return {
        "timezone": data.get("timezone"),
        "current": {
            "time": current.get("time"),
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation"),
            "weather_code": current.get("weather_code"),
        },
        "forecast_5_days": [
            {
                "date": day,
                "weather_code": _daily_value(data, "weather_code", index),
                "temp_max": _daily_value(data, "temperature_2m_max", index),
                "temp_min": _daily_value(data, "temperature_2m_min", index),
                "precipitation": _daily_value(data, "precipitation_sum", index),
                "precipitation_probability": _daily_value(data, "precipitation_probability_max", index),
                "humidity": _daily_value(data, "relative_humidity_2m_mean", index),
                "wind": _daily_value(data, "wind_speed_10m_max", index),
            }
            for index, day in enumerate(daily_times)
        ],
    }
