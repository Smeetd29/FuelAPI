from decimal import Decimal, ROUND_HALF_UP
from .geo import find_candidate_stations


class InfeasibleRouteError(Exception):
    """Raised when no fuel stops can be found that satisfy vehicle range constraints."""
    pass


DEFAULT_MPG = 10.0
DEFAULT_MAX_RANGE_MILES = 500.0
DEFAULT_TANK_CAPACITY_GALLONS = 50.0
CORRIDOR_STEPS = [5.0, 10.0, 15.0, 20.0, 25.0]


def round_decimal(value: Decimal, places: int = 2) -> Decimal:
    """Helper to round Decimal using ROUND_HALF_UP."""
    q = Decimal(10) ** -places
    return value.quantize(q, rounding=ROUND_HALF_UP)


def optimize_fuel_plan(
    candidate_stations: list[dict],
    total_distance_miles: float,
    max_range_miles: float = DEFAULT_MAX_RANGE_MILES,
    mpg: float = DEFAULT_MPG,
    tank_capacity_gallons: float = DEFAULT_TANK_CAPACITY_GALLONS
) -> dict | None:
    """
    Optimizes fuel stops along a route to minimize total fuel cost.
    The vehicle starts with a full free tank of tank_capacity_gallons (500 miles range).
    Fuel at start is free.
    Routes <= max_range_miles require 0 stops and cost $0.00.
    Destination is treated as a $0.00 terminal.
    Guarantees every gap (start to first stop, stop to stop, last stop to finish) is <= max_range_miles.

    Returns:
        dict with 'fuel_stops' and 'fuel_summary' (using Decimals internally), or None if infeasible.
    """
    mpg_dec = Decimal(str(mpg))
    capacity_dec = Decimal(str(tank_capacity_gallons))
    total_dist_dec = Decimal(str(round(total_distance_miles, 4)))
    total_gallons_consumed = round_decimal(total_dist_dec / mpg_dec, 2)

    # Routes <= 500 miles need no refueling
    if total_distance_miles <= max_range_miles:
        return {
            'fuel_stops': [],
            'fuel_summary': {
                'total_gallons_consumed': total_gallons_consumed,
                'total_gallons_purchased': Decimal('0.00'),
                'total_fuel_cost': Decimal('0.00'),
            }
        }

    if not candidate_stations:
        return None

    # Filter out stations before start or past finish
    valid_candidates = []
    for c in candidate_stations:
        m = c['mile_marker']
        if 0.1 <= m <= total_distance_miles - 0.1:
            valid_candidates.append(c)

    if not valid_candidates:
        return None

    # Sort by mile marker
    valid_candidates.sort(key=lambda s: s['mile_marker'])

    # Deduplicate candidate stations that are at practically the same mile marker (<0.2 mi)
    # keeping the cheapest one
    deduped: list[dict] = []
    for c in valid_candidates:
        if not deduped:
            deduped.append(c)
        else:
            prev = deduped[-1]
            if abs(c['mile_marker'] - prev['mile_marker']) < 0.2:
                # Keep cheaper station
                if c['price'] < prev['price']:
                    deduped[-1] = c
            else:
                deduped.append(c)

    # Build sequence of points:
    # Index 0: Start at mile 0.0 with sentinel high price (no buying at start, fuel is already full)
    # Index 1..N: Candidate stations
    # Index N+1: Finish at total_distance_miles with price $0.00
    start_pt = {
        'mile_marker': 0.0,
        'price': Decimal('999999.00'),
        'name': 'START',
        'opis_id': -1,
        'city': '',
        'state': '',
        'address': '',
        'latitude': 0.0,
        'longitude': 0.0,
        'distance_from_route_miles': 0.0
    }
    finish_pt = {
        'mile_marker': total_distance_miles,
        'price': Decimal('0.00'),
        'name': 'FINISH',
        'opis_id': -2,
        'city': '',
        'state': '',
        'address': '',
        'latitude': 0.0,
        'longitude': 0.0,
        'distance_from_route_miles': 0.0
    }

    all_points = [start_pt] + deduped + [finish_pt]

    curr_idx = 0
    curr_fuel = capacity_dec  # Starts with full 50 gal tank
    fuel_stops: list[dict] = []

    while curr_idx < len(all_points) - 1:
        curr = all_points[curr_idx]
        curr_mile = curr['mile_marker']

        # Find reachable points from curr_idx
        reachable = []
        for j in range(curr_idx + 1, len(all_points)):
            dist = all_points[j]['mile_marker'] - curr_mile
            if dist <= max_range_miles + 1e-6:
                reachable.append(j)
            else:
                break

        # If no station or destination is reachable within range, plan is infeasible
        if not reachable:
            return None

        # Look for the first station in reachable with price < curr['price']
        # Note: Finish has price 0.00, so if finish is in reachable, it is always cheaper!
        first_cheaper_idx = None
        for j in reachable:
            if all_points[j]['price'] < curr['price']:
                first_cheaper_idx = j
                break

        if first_cheaper_idx is not None:
            # Case 1: A cheaper station exists in reachable range
            # Buy just enough fuel at curr to reach first_cheaper_idx
            target = all_points[first_cheaper_idx]
            dist_to_target = Decimal(str(round(target['mile_marker'] - curr_mile, 4)))
            fuel_needed = dist_to_target / mpg_dec

            buy = max(Decimal('0.00'), fuel_needed - curr_fuel)
            # Cannot buy more than tank capacity allows
            if curr_fuel + buy > capacity_dec:
                buy = capacity_dec - curr_fuel

            if curr_idx > 0 and buy > Decimal('0.001'):
                cost = buy * curr['price']
                fuel_stops.append({
                    'opis_id': curr['opis_id'],
                    'name': curr['name'],
                    'address': curr['address'],
                    'city': curr['city'],
                    'state': curr['state'],
                    'latitude': curr['latitude'],
                    'longitude': curr['longitude'],
                    'price_per_gallon': curr['price'],
                    'mile_marker': curr['mile_marker'],
                    'distance_from_route_miles': curr['distance_from_route_miles'],
                    'gallons_purchased': buy,
                    'cost': cost,
                })

            curr_fuel = curr_fuel + buy - fuel_needed
            curr_idx = first_cheaper_idx

        else:
            # Case 2: No cheaper station within reachable range.
            # Station curr has the lowest price in sight! Fill up to maximum capacity.
            buy = capacity_dec - curr_fuel
            if curr_idx > 0 and buy > Decimal('0.001'):
                cost = buy * curr['price']
                fuel_stops.append({
                    'opis_id': curr['opis_id'],
                    'name': curr['name'],
                    'address': curr['address'],
                    'city': curr['city'],
                    'state': curr['state'],
                    'latitude': curr['latitude'],
                    'longitude': curr['longitude'],
                    'price_per_gallon': curr['price'],
                    'mile_marker': curr['mile_marker'],
                    'distance_from_route_miles': curr['distance_from_route_miles'],
                    'gallons_purchased': buy,
                    'cost': cost,
                })

            curr_fuel = capacity_dec

            # Move to the station in reachable range with the lowest price
            # If prices are tied, choose the farthest station to make maximum progress
            best_next_idx = reachable[0]
            for j in reachable[1:]:
                if (
                    all_points[j]['price'] < all_points[best_next_idx]['price']
                    or (
                        all_points[j]['price'] == all_points[best_next_idx]['price']
                        and all_points[j]['mile_marker'] > all_points[best_next_idx]['mile_marker']
                    )
                ):
                    best_next_idx = j

            next_station = all_points[best_next_idx]
            dist_to_next = Decimal(str(round(next_station['mile_marker'] - curr_mile, 4)))
            curr_fuel -= dist_to_next / mpg_dec
            curr_idx = best_next_idx

    # Verification: Guarantee all gaps between stops are <= max_range_miles
    prev_mile = 0.0
    for stop in fuel_stops:
        gap = stop['mile_marker'] - prev_mile
        if gap > max_range_miles + 1e-4:
            return None
        prev_mile = stop['mile_marker']

    if total_distance_miles - prev_mile > max_range_miles + 1e-4:
        return None

    total_gallons_purchased = sum(s['gallons_purchased'] for s in fuel_stops) if fuel_stops else Decimal('0.00')
    total_fuel_cost = sum(s['cost'] for s in fuel_stops) if fuel_stops else Decimal('0.00')

    return {
        'fuel_stops': fuel_stops,
        'fuel_summary': {
            'total_gallons_consumed': total_gallons_consumed,
            'total_gallons_purchased': round_decimal(total_gallons_purchased, 2),
            'total_fuel_cost': round_decimal(total_fuel_cost, 2),
        }
    }


def plan_route_fuel(
    route_coordinates: list[list[float]],
    total_distance_miles: float,
    corridors: list[float] = CORRIDOR_STEPS
) -> dict:
    """
    Plans optimal fuel stops for a given route polyline.
    Automatically widens the search corridor (from 5 miles up to 25 miles)
    if no feasible plan is found on narrower corridors.
    Raises InfeasibleRouteError if even the widest corridor cannot bridge all 500-mile gaps.
    """
    for corridor in corridors:
        candidates = find_candidate_stations(route_coordinates, corridor_miles=corridor)
        plan = optimize_fuel_plan(
            candidates,
            total_distance_miles,
            max_range_miles=DEFAULT_MAX_RANGE_MILES,
            mpg=DEFAULT_MPG,
            tank_capacity_gallons=DEFAULT_TANK_CAPACITY_GALLONS
        )
        if plan is not None:
            return plan

    raise InfeasibleRouteError(
        f"Unable to find a feasible fueling plan within vehicle's {int(DEFAULT_MAX_RANGE_MILES)}-mile range."
    )
