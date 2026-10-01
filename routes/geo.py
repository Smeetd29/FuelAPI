import math
import numpy as np
from .models import FuelStation

# Earth radius in statute miles
EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Computes great-circle distance between two (lat, lon) points in miles using the haversine formula.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_MILES * c


def compute_cumulative_distances(coordinates: list[list[float]]) -> np.ndarray:
    """
    Given a list of GeoJSON [lon, lat] coordinates, returns a 1D numpy array of
    cumulative distances along the polyline in miles, starting with 0.0.
    """
    if len(coordinates) <= 1:
        return np.array([0.0])

    pts = np.asarray(coordinates, dtype=np.float64)
    lons = pts[:, 0]
    lats = pts[:, 1]

    phi1 = np.radians(lats[:-1])
    phi2 = np.radians(lats[1:])
    dphi = np.radians(lats[1:] - lats[:-1])
    dlam = np.radians(lons[1:] - lons[:-1])

    a = (
        np.sin(dphi / 2.0) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2.0) ** 2
    )
    a = np.clip(a, 0.0, 1.0)
    c = 2.0 * np.arcsin(np.sqrt(a))
    seg_dists = EARTH_RADIUS_MILES * c

    cum_dists = np.zeros(len(coordinates), dtype=np.float64)
    cum_dists[1:] = np.cumsum(seg_dists)
    return cum_dists


def simplify_geometry(coordinates: list[list[float]], epsilon_degrees: float = 0.0005) -> list[list[float]]:
    """
    Simplifies polyline geometry using an iterative, stack-based Ramer-Douglas-Peucker algorithm.
    Safe for 100k+ point routes without recursion depth limits.
    Default epsilon (~0.0005 deg) corresponds to ~50m, preserving visual fidelity on map zoom.
    """
    if len(coordinates) < 3:
        return coordinates

    pts = np.asarray(coordinates, dtype=np.float64)
    n_pts = len(pts)
    keep = np.zeros(n_pts, dtype=bool)
    keep[0] = True
    keep[-1] = True

    stack = [(0, n_pts - 1)]
    while stack:
        start_idx, end_idx = stack.pop()
        p1 = pts[start_idx]
        p2 = pts[end_idx]
        diff = p2 - p1
        line_len_sq = np.sum(diff ** 2)

        segment_points = pts[start_idx + 1:end_idx]
        if len(segment_points) == 0:
            continue

        if line_len_sq == 0:
            dists = np.sqrt(np.sum((segment_points - p1) ** 2, axis=1))
        else:
            dists = np.abs(
                (p2[1] - p1[1]) * segment_points[:, 0]
                - (p2[0] - p1[0]) * segment_points[:, 1]
                + p2[0] * p1[1]
                - p2[1] * p1[0]
            ) / math.sqrt(line_len_sq)

        max_sub_idx = np.argmax(dists)
        max_dist = dists[max_sub_idx]

        if max_dist > epsilon_degrees:
            split_idx = start_idx + 1 + max_sub_idx
            keep[split_idx] = True
            stack.append((start_idx, split_idx))
            stack.append((split_idx, end_idx))

    return pts[keep].tolist()


def downsample_for_candidate_projection(
    coordinates: list[list[float]],
    cum_dists: np.ndarray,
    max_calc_points: int = 2500
) -> tuple[np.ndarray, np.ndarray]:
    """
    If the polyline has more than max_calc_points, downsamples it while preserving
    cumulative distance checkpoints so candidate station projection stays well under 0.1s.
    """
    pts = np.asarray(coordinates, dtype=np.float64)
    if len(pts) <= max_calc_points:
        return pts, cum_dists

    # Step sampling ensuring endpoints are kept
    step = int(math.ceil(len(pts) / max_calc_points))
    indices = list(range(0, len(pts), step))
    if indices[-1] != len(pts) - 1:
        indices.append(len(pts) - 1)

    return pts[indices], cum_dists[indices]


def find_candidate_stations(
    route_coordinates: list[list[float]],
    corridor_miles: float = 5.0
) -> list[dict]:
    """
    Finds fuel stations in the database within corridor_miles of the route polyline.
    1. Pre-filters using a bounding box DB query with a degree buffer.
    2. Projects each station onto route polyline segments using vectorized equirectangular math.
    3. Computes each station's distance to the route and its mile marker along the route.

    Returns a list of dicts:
        [{
            'station': FuelStation instance,
            'mile_marker': float,
            'distance_from_route_miles': float,
            'price': Decimal,
            'opis_id': int,
            'name': str,
            'address': str,
            'city': str,
            'state': str,
            'latitude': float,
            'longitude': float,
        }, ...]
    """
    if len(route_coordinates) < 2:
        return []

    route_arr = np.asarray(route_coordinates, dtype=np.float64)
    cum_dists = compute_cumulative_distances(route_coordinates)

    # 1. Bounding box pre-filter with buffer
    # 1 degree latitude ~ 69 miles. 1 degree longitude at US mid-lat ~ 53 miles.
    lat_buf = (corridor_miles + 5.0) / 60.0
    lon_buf = (corridor_miles + 5.0) / 45.0

    min_lat = float(np.min(route_arr[:, 1]) - lat_buf)
    max_lat = float(np.max(route_arr[:, 1]) + lat_buf)
    min_lon = float(np.min(route_arr[:, 0]) - lon_buf)
    max_lon = float(np.max(route_arr[:, 0]) + lon_buf)

    stations = list(
        FuelStation.objects.filter(
            latitude__isnull=False,
            longitude__isnull=False,
            latitude__range=(min_lat, max_lat),
            longitude__range=(min_lon, max_lon),
        )
    )

    if not stations:
        return []

    # 2. Downsample polyline for candidate projection if extremely dense
    calc_coords, calc_cum_dists = downsample_for_candidate_projection(
        route_coordinates, cum_dists, max_calc_points=2500
    )

    seg_lon1 = calc_coords[:-1, 0]
    seg_lon2 = calc_coords[1:, 0]
    seg_lat1 = calc_coords[:-1, 1]
    seg_lat2 = calc_coords[1:, 1]
    seg_c1 = calc_cum_dists[:-1]
    seg_c2 = calc_cum_dists[1:]
    seg_len = seg_c2 - seg_c1

    mid_lat_rad = np.radians((seg_lat1 + seg_lat2) * 0.5)
    cos_lat = np.cos(mid_lat_rad)

    dx = (seg_lon2 - seg_lon1) * (69.172 * cos_lat)
    dy = (seg_lat2 - seg_lat1) * 69.0
    seg_l2 = dx * dx + dy * dy
    seg_l2 = np.where(seg_l2 <= 1e-12, 1e-12, seg_l2)

    st_lats = np.array([s.latitude for s in stations], dtype=np.float64)
    st_lons = np.array([s.longitude for s in stations], dtype=np.float64)

    # Broadcast: (N_stations, N_segments)
    # Batch processing in chunks if N_stations is large to conserve memory
    candidates = []
    chunk_size = 500

    for chunk_start in range(0, len(stations), chunk_size):
        chunk_end = min(chunk_start + chunk_size, len(stations))
        chunk_stations = stations[chunk_start:chunk_end]
        sub_lats = st_lats[chunk_start:chunk_end]
        sub_lons = st_lons[chunk_start:chunk_end]

        sx = (sub_lons[:, None] - seg_lon1[None, :]) * (69.172 * cos_lat[None, :])
        sy = (sub_lats[:, None] - seg_lat1[None, :]) * 69.0

        t = (sx * dx[None, :] + sy * dy[None, :]) / seg_l2[None, :]
        t = np.clip(t, 0.0, 1.0)

        proj_x = sx - t * dx[None, :]
        proj_y = sy - t * dy[None, :]
        dists = np.sqrt(proj_x ** 2 + proj_y ** 2)

        best_seg_idx = np.argmin(dists, axis=1)
        best_dists = dists[np.arange(len(sub_lats)), best_seg_idx]
        best_t = t[np.arange(len(sub_lats)), best_seg_idx]
        mile_markers = seg_c1[best_seg_idx] + best_t * seg_len[best_seg_idx]

        for idx, station in enumerate(chunk_stations):
            dist_mi = float(best_dists[idx])
            if dist_mi <= corridor_miles:
                candidates.append({
                    'station': station,
                    'opis_id': station.opis_id,
                    'name': station.name,
                    'address': station.address,
                    'city': station.city,
                    'state': station.state,
                    'latitude': station.latitude,
                    'longitude': station.longitude,
                    'price': station.price,
                    'mile_marker': float(mile_markers[idx]),
                    'distance_from_route_miles': dist_mi,
                })

    return candidates
