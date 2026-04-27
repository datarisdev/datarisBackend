import requests

def reverse_geocode(lat: float, lon: float):
    """
    Returns a dict with detailed location info from Nominatim
    """
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "zoom": 10,  # city/town level
        "addressdetails": 1
    }
    headers = {
        "User-Agent": "parcel-weather-api"
    }

    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    address = data.get("address", {})

    return {
        "city": address.get("city"),
        "town": address.get("town"),
        "village": address.get("village"),
        "municipality": address.get("municipality"),
        "county": address.get("county"),
        "state": address.get("state"),
        "postcode": address.get("postcode"),
        "country": address.get("country"),
        "country_code": address.get("country_code"),
        "raw_address": address  # for debugging
    }
