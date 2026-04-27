import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def get_weather(lat: float, lon: float):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": [
            "temperature_2m",
            "relative_humidity_2m",
            "wind_speed_10m",
            "precipitation"
        ],
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum"
        ],
        "forecast_days": 5,
        "timezone": "auto"
    }

    response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    return {
        "current": {
            "temperature": data["current"]["temperature_2m"],
            "humidity": data["current"]["relative_humidity_2m"],
            "wind": data["current"]["wind_speed_10m"],
            "precipitation": data["current"]["precipitation"],
        },
        "forecast_5_days": [
            {
                "date": data["daily"]["time"][i],
                "temp_max": data["daily"]["temperature_2m_max"][i],
                "temp_min": data["daily"]["temperature_2m_min"][i],
                "precipitation": data["daily"]["precipitation_sum"][i],
            }
            for i in range(len(data["daily"]["time"]))
        ]
    }
