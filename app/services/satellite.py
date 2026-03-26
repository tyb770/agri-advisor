# app/services/satellite.py

import logging
import openmeteo_requests
import requests_cache
from retry_requests import retry
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# ── Open-Meteo client (free, no API key) ──────────────────────
cache_session = requests_cache.CachedSession(".weather_cache", expire_after=3600)
retry_session = retry(cache_session, retries=3, backoff_factor=0.5)
openmeteo = openmeteo_requests.Client(session=retry_session)

def get_weather_for_field(latitude: float, longitude: float) -> dict:
    """
    Fetch current weather + 3-day forecast for a field location.
    Returns a dict with temperature, humidity, rainfall, wind.
    """
    try:
        responses = openmeteo.weather_api(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "wind_speed_10m",
                ],
                "daily": [
                    "precipitation_sum",
                    "temperature_2m_max",
                    "temperature_2m_min",
                ],
                "timezone": "Asia/Karachi",
                "forecast_days": 3,
            }
        )

        response = responses[0]
        current = response.Current()
        daily = response.Daily()

        weather = {
            "temperature_c": round(current.Variables(0).Value(), 1),
            "humidity_pct":  round(current.Variables(1).Value(), 1),
            "precipitation_mm": round(current.Variables(2).Value(), 2),
            "wind_speed_kmh": round(current.Variables(3).Value(), 1),
            "forecast_rain_mm": [
                round(daily.Variables(0).ValuesAsNumpy()[i].item(), 2)
                for i in range(3)
            ],
            "forecast_temp_max": [
                round(daily.Variables(1).ValuesAsNumpy()[i].item(), 1)
                for i in range(3)
            ],
        }

        logger.info(f"Weather fetched for ({latitude}, {longitude}): {weather['temperature_c']}°C")
        return weather

    except Exception as e:
        logger.error(f"Weather fetch failed for ({latitude}, {longitude}): {e}")
        return {}


def estimate_ndvi(crop_type: str, weather: dict, current_ndvi: float | None) -> float:
    """
    Estimate NDVI based on weather stress factors.
    Phase 3: replace this with real GEE Sentinel-2 NDVI pull.

    Rules:
    - Start from current NDVI or crop-type baseline
    - High temp (>40°C) degrades NDVI
    - High rainfall improves NDVI for most crops
    - Low humidity degrades NDVI
    """
    # Baseline NDVI by crop if we have no current reading
    baselines = {
        "wheat":     0.65,
        "cotton":    0.60,
        "rice":      0.70,
        "sugarcane": 0.72,
        "maize":     0.63,
        "other":     0.55,
    }
    ndvi = current_ndvi if current_ndvi is not None else baselines.get(crop_type, 0.55)

    temp = weather.get("temperature_c", 30)
    humidity = weather.get("humidity_pct", 50)
    rain = weather.get("precipitation_mm", 0)

    # Apply stress/boost factors
    if temp > 42:
        ndvi -= 0.05   # heat stress
    elif temp > 38:
        ndvi -= 0.02

    if humidity < 30:
        ndvi -= 0.03   # drought stress
    elif humidity > 70:
        ndvi += 0.02

    if rain > 5:
        ndvi += 0.03   # good rainfall
    elif rain == 0 and humidity < 40:
        ndvi -= 0.02   # dry spell

    # Clamp to valid range
    return round(max(0.1, min(1.0, ndvi)), 3)