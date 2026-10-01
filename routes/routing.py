import requests
from django.conf import settings
from django.core.cache import cache


class RoutingError(Exception):
    """Raised for any network errors, HTTP errors, 429, or invalid results from the routing provider."""
    pass


METERS_TO_MILES = 0.000621371


def get_route(start_lat: float, start_lon: float, finish_lat: float, finish_lon: float) -> tuple[dict, bool]:
    """
    Retrieves route directions between start and finish coordinates using OpenRouteService.
    Requires OPENROUTESERVICE_API_KEY to be configured.
    Makes ONE call without turn-by-turn instructions.
    Caches the result for 24 hours by rounded coordinates (4 decimal places).

    Returns:
        tuple (route_data, called_external_api) where:
            route_data = {
                'distance_miles': float,
                'duration_minutes': float,
                'geometry': {'type': 'LineString', 'coordinates': [[lon, lat], ...]}
            }
            called_external_api = bool
    """
    # Cache key rounded to 4 decimal places (~11 meters precision)
    cache_key = (
        f"route_v1_{round(start_lat, 4)}_{round(start_lon, 4)}_to_"
        f"{round(finish_lat, 4)}_{round(finish_lon, 4)}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, False

    api_key = getattr(settings, 'OPENROUTESERVICE_API_KEY', '')
    if not api_key:
        raise RoutingError(
            "OPENROUTESERVICE_API_KEY is not configured. "
            "Please set it in your .env file to enable routing."
        )

    # OpenRouteService Directions API (v2 GeoJSON endpoint)
    ors_url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    headers = {
        'Authorization': api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json, application/geo+json'
    }
    body = {
        'coordinates': [[start_lon, start_lat], [finish_lon, finish_lat]],
        'instructions': False,
        'geometry_simplify': False
    }
    try:
        resp = requests.post(ors_url, json=body, headers=headers, timeout=15)
        if resp.status_code == 429:
            raise RoutingError("OpenRouteService rate limit exceeded (429). Please wait and retry.")
        if resp.status_code in (401, 403):
            raise RoutingError("OpenRouteService authentication failed. Please check your API key.")
        resp.raise_for_status()
        data = resp.json()
        features = data.get('features', [])
        if not features:
            raise RoutingError("No route returned by OpenRouteService.")
        feat = features[0]
        summary = feat.get('properties', {}).get('summary', {})
        dist_meters = summary.get('distance', 0)
        duration_secs = summary.get('duration', 0)
        coordinates = feat.get('geometry', {}).get('coordinates', [])
        if not coordinates:
            raise RoutingError("Route geometry is empty.")
    except requests.RequestException as e:
        raise RoutingError(f"Network error communicating with OpenRouteService: {e}")

    route_data = {
        'distance_miles': float(dist_meters * METERS_TO_MILES),
        'duration_minutes': float(duration_secs / 60.0),
        'geometry': {
            'type': 'LineString',
            'coordinates': coordinates
        }
    }

    # Cache for 24 hours
    cache.set(cache_key, route_data, timeout=86400)
    return route_data, True
