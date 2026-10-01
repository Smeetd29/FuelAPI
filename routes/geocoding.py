import re
import requests
from django.conf import settings
from django.core.cache import cache



class LocationError(Exception):
    """Raised when a location cannot be resolved or is outside the USA."""
    pass


def normalize_city(name: str) -> str:
    """
    Normalizes city/place names for reliable matching against gazetteers.
    Handles case, punctuation, common abbreviations, and gazetteer administrative suffixes.
    """
    if not name:
        return ""
    n = name.upper().strip()
    # Replace punctuation with space
    n = re.sub(r"[\.,\'\-\(\)\/\#\&\@]", " ", n)

    # Standardize common word abbreviations
    n = re.sub(r"\bST\b", "SAINT", n)
    n = re.sub(r"\bSTE\b", "SAINTE", n)
    n = re.sub(r"\bFT\b", "FORT", n)
    n = re.sub(r"\bMT\b", "MOUNT", n)
    n = re.sub(r"\bN\b", "NORTH", n)
    n = re.sub(r"\bS\b", "SOUTH", n)
    n = re.sub(r"\bE\b", "EAST", n)
    n = re.sub(r"\bW\b", "WEST", n)

    # Strip trailing/administrative suffixes commonly present in gazetteer entries
    n = re.sub(
        r"\b(CITY|TOWN|VILLAGE|CDP|BOROUGH|MUNICIPALITY|TOWNSHIP|CHARTER TOWNSHIP|PLANTATION|COUNTY|PARISH)\b",
        " ",
        n,
    )
    # Collapse multiple spaces
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Regex to detect coordinate input like "40.7128, -74.0060"
COORD_REGEX = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$"
)


def is_in_usa_bounding_box(lat: float, lon: float) -> bool:
    """
    Quick check that coordinates fall within the general US geographical bounds
    (contiguous US, Alaska, Hawaii, territories: lat 17 to 72, lon -180 to -65).
    """
    return 17.0 <= lat <= 72.0 and -180.0 <= lon <= -65.0


def resolve_location(text: str) -> tuple[dict, bool]:
    """
    Resolves a start or finish location string into coordinates.
    Accepts 'lat,lon' directly without an API call.
    Otherwise queries OpenRouteService geocoding, restricted strictly to the USA.
    Requires OPENROUTESERVICE_API_KEY to be configured.
    Caches results for 24 hours.

    Returns:
        tuple (data, called_external_api) where:
            data = {'lat': float, 'lon': float, 'display_name': str}
            called_external_api = bool (True if an external API call was made)
    """
    if not text or not text.strip():
        raise LocationError("Location string cannot be empty.")

    clean_text = text.strip()

    # 1. Check if user provided direct "lat,lon" coordinates
    coord_match = COORD_REGEX.match(clean_text)
    if coord_match:
        lat = float(coord_match.group(1))
        lon = float(coord_match.group(2))
        if not is_in_usa_bounding_box(lat, lon):
            raise LocationError(f"Coordinates ({lat}, {lon}) are outside the United States.")
        return {
            'lat': lat,
            'lon': lon,
            'display_name': f"{lat:.4f}, {lon:.4f}"
        }, False

    # 2. Check cache (key based on sanitized lowercase text)
    safe_key = re.sub(r"[^a-zA-Z0-9_\-]", "_", clean_text.lower())
    cache_key = f"geocode_v1_{safe_key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, False


    # 3. External API Geocoding (OpenRouteService only)
    api_key = getattr(settings, 'OPENROUTESERVICE_API_KEY', '')
    if not api_key:
        raise LocationError(
            "OPENROUTESERVICE_API_KEY is not configured. "
            "Please set it in your .env file to enable geocoding."
        )

    lat, lon, display_name = None, None, clean_text

    ors_url = "https://api.openrouteservice.org/geocode/search"
    params = {
        'api_key': api_key,
        'text': clean_text,
        'boundary.country': 'USA',
        'size': 1
    }
    try:
        resp = requests.get(ors_url, params=params, timeout=10)
        if resp.status_code == 429:
            raise LocationError("Geocoding rate limit exceeded. Please try again later.")
        if resp.status_code in (401, 403):
            raise LocationError("OpenRouteService authentication failed. Please check your API key.")
        resp.raise_for_status()
        data = resp.json()
        features = data.get('features', [])
        if not features:
            raise LocationError(f"Could not resolve location '{clean_text}' within the USA.")
        geom = features[0].get('geometry', {})
        coords = geom.get('coordinates', [])
        if len(coords) < 2:
            raise LocationError(f"Invalid coordinate format from provider for '{clean_text}'.")
        lon, lat = float(coords[0]), float(coords[1])
        display_name = features[0].get('properties', {}).get('label', clean_text)
    except requests.RequestException as e:
        raise LocationError(f"Error connecting to OpenRouteService geocoding: {e}")


    # Validate coordinates are within USA
    if not is_in_usa_bounding_box(lat, lon):
        raise LocationError(f"Location '{clean_text}' resolved outside the USA ({lat}, {lon}).")

    result = {
        'lat': lat,
        'lon': lon,
        'display_name': display_name
    }
    # Cache for 24 hours
    cache.set(cache_key, result, timeout=86400)
    return result, True
