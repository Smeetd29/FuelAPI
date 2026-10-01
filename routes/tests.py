from decimal import Decimal
from unittest.mock import patch
import math
from django.core.cache import cache
from django.test import TestCase, Client
from rest_framework import status

from .models import FuelStation
from .geo import (
    haversine_miles,
    compute_cumulative_distances,
    simplify_geometry,
    find_candidate_stations,
)
from .geocoding import normalize_city, resolve_location, LocationError
from .fuel import (
    optimize_fuel_plan,
    plan_route_fuel,
    InfeasibleRouteError,
    DEFAULT_MAX_RANGE_MILES,
    DEFAULT_MPG,
    DEFAULT_TANK_CAPACITY_GALLONS,
)
from .routing import get_route, RoutingError


# ---------------------------------------------------------------------------
# 1. Geo Tests
# ---------------------------------------------------------------------------
class GeoTests(TestCase):
    def test_haversine_known_values(self):
        # NYC (40.7128, -74.0060) to LA (34.0522, -118.2437)
        dist = haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertAlmostEqual(dist, 2445.5, delta=15.0)

        # Same point should have distance 0.0
        dist_zero = haversine_miles(40.7128, -74.0060, 40.7128, -74.0060)
        self.assertAlmostEqual(dist_zero, 0.0, places=5)

        # 1 degree of latitude is ~69.0 miles
        dist_lat = haversine_miles(40.0, -74.0, 41.0, -74.0)
        self.assertAlmostEqual(dist_lat, 69.0, delta=0.5)

    def test_cumulative_distances(self):
        coords = [
            [-74.0, 40.0],
            [-74.0, 41.0],  # ~69 miles north
            [-74.0, 42.0],  # ~69 miles north
        ]
        cum_dists = compute_cumulative_distances(coords)
        self.assertEqual(len(cum_dists), 3)
        self.assertEqual(cum_dists[0], 0.0)
        self.assertAlmostEqual(cum_dists[1], 69.0, delta=0.5)
        self.assertAlmostEqual(cum_dists[2], 138.0, delta=1.0)

    def test_simplify_geometry(self):
        # Collinear points on straight line should simplify down to endpoints
        coords = [[-74.0 + i * 0.01, 40.0 + i * 0.01] for i in range(100)]
        simplified = simplify_geometry(coords, epsilon_degrees=0.001)
        self.assertEqual(len(simplified), 2)
        self.assertEqual(simplified[0], coords[0])
        self.assertEqual(simplified[-1], coords[-1])

    def test_mile_marker_and_corridor_logic(self):
        # Route heading east from (-74.0, 40.0) to (-70.0, 40.0) along lat 40.0
        route_coords = [
            [-74.0, 40.0],
            [-72.0, 40.0],
            [-70.0, 40.0],
        ]

        # Station 1: Close to route at mile ~100 (corridor ~1.5 miles north)
        s1 = FuelStation.objects.create(
            opis_id=1001,
            name="Near Station",
            address="Exit 10",
            city="Newark",
            state="NJ",
            price=Decimal("3.2500"),
            latitude=40.02,
            longitude=-72.0,
        )

        # Station 2: Far from route (~70 miles north)
        s2 = FuelStation.objects.create(
            opis_id=1002,
            name="Far Station",
            address="Highway 9",
            city="Albany",
            state="NY",
            price=Decimal("2.9900"),
            latitude=41.0,
            longitude=-72.0,
        )

        # Test with 5-mile corridor
        candidates = find_candidate_stations(route_coords, corridor_miles=5.0)
        found_ids = [c["opis_id"] for c in candidates]

        self.assertIn(1001, found_ids)
        self.assertNotIn(1002, found_ids)

        c1 = next(c for c in candidates if c["opis_id"] == 1001)
        self.assertLess(c1["distance_from_route_miles"], 2.0)
        self.assertGreater(c1["mile_marker"], 50.0)


# ---------------------------------------------------------------------------
# 2. Fuel Tests
# ---------------------------------------------------------------------------
class FuelTests(TestCase):
    def test_route_under_500_miles_gives_no_stops_and_zero_cost(self):
        total_dist = 450.0
        candidates = [
            {
                "opis_id": 1,
                "name": "Mid Station",
                "price": Decimal("3.1000"),
                "mile_marker": 200.0,
                "city": "CityA",
                "state": "ST",
                "address": "Addr",
                "latitude": 40.0,
                "longitude": -75.0,
                "distance_from_route_miles": 1.0,
            }
        ]

        plan = optimize_fuel_plan(candidates, total_dist)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["fuel_stops"]), 0)
        self.assertEqual(plan["fuel_summary"]["total_gallons_purchased"], Decimal("0.00"))
        self.assertEqual(plan["fuel_summary"]["total_fuel_cost"], Decimal("0.00"))
        self.assertEqual(plan["fuel_summary"]["total_gallons_consumed"], Decimal("45.00"))

    def test_cost_matches_hand_calculation(self):
        # Hand calculation scenario:
        # Distance = 800 miles. Tank = 50 gal (500 miles free start). MPG = 10.
        # Single station at mile 400 with price $3.00/gal.
        # At mile 400, vehicle arrives with: 50 - (400/10) = 10 gal.
        # Distance from mile 400 to finish (mile 800) is 400 miles.
        # Fuel needed to reach finish = 400 / 10 = 40 gal.
        # Since finish has price $0 (cheaper than $3.00), vehicle buys exactly:
        # 40 - 10 = 30 gallons at $3.00/gal.
        # Total cost must be 30 * 3.00 = $90.00!
        total_dist = 800.0
        candidates = [
            {
                "opis_id": 50,
                "name": "Mid Station",
                "price": Decimal("3.0000"),
                "mile_marker": 400.0,
                "city": "Midway",
                "state": "OH",
                "address": "Exit 40",
                "latitude": 40.0,
                "longitude": -82.0,
                "distance_from_route_miles": 0.5,
            }
        ]

        plan = optimize_fuel_plan(candidates, total_dist)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["fuel_stops"]), 1)
        stop = plan["fuel_stops"][0]
        self.assertEqual(stop["opis_id"], 50)
        self.assertEqual(stop["gallons_purchased"], Decimal("30.00"))
        self.assertEqual(stop["cost"], Decimal("90.00"))
        self.assertEqual(plan["fuel_summary"]["total_fuel_cost"], Decimal("90.00"))
        self.assertEqual(plan["fuel_summary"]["total_gallons_consumed"], Decimal("80.00"))

    def test_no_gap_ever_exceeds_500_miles(self):
        # Long route of 2,200 miles with several stations
        total_dist = 2200.0
        candidates = [
            {"opis_id": 10, "name": "S1", "price": Decimal("3.50"), "mile_marker": 350.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
            {"opis_id": 20, "name": "S2", "price": Decimal("3.20"), "mile_marker": 700.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
            {"opis_id": 30, "name": "S3", "price": Decimal("2.90"), "mile_marker": 1150.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
            {"opis_id": 40, "name": "S4", "price": Decimal("3.40"), "mile_marker": 1550.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
            {"opis_id": 50, "name": "S5", "price": Decimal("3.10"), "mile_marker": 1900.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
        ]

        plan = optimize_fuel_plan(candidates, total_dist)
        self.assertIsNotNone(plan)

        stops = plan["fuel_stops"]
        self.assertGreater(len(stops), 0)

        # Check all gaps
        prev_mile = 0.0
        for s in stops:
            gap = s["mile_marker"] - prev_mile
            self.assertLessEqual(gap, 500.0 + 1e-4)
            prev_mile = s["mile_marker"]
        final_gap = total_dist - prev_mile
        self.assertLessEqual(final_gap, 500.0 + 1e-4)

    def test_infeasible_case_returns_none(self):
        # Route of 1200 miles with no stations between 300 and 900 (gap 600 miles > 500)
        total_dist = 1200.0
        candidates = [
            {"opis_id": 1, "name": "S1", "price": Decimal("3.00"), "mile_marker": 300.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
            {"opis_id": 2, "name": "S2", "price": Decimal("3.00"), "mile_marker": 900.0, "city": "", "state": "", "address": "", "latitude": 0, "longitude": 0, "distance_from_route_miles": 0},
        ]
        plan = optimize_fuel_plan(candidates, total_dist)
        self.assertIsNone(plan)

    def test_optimizer_matches_dp_reference_on_random_cases(self):
        # Reference exact DP solver for the gas station problem
        def dp_reference(stations, total_d, cap=50.0, mpg=10.0):
            pts = [{'mile': 0.0, 'price': 1e9}] + stations + [{'mile': total_d, 'price': 0.0}]
            memo = {}

            def solve(i, f):
                state = (i, round(f, 4))
                if state in memo:
                    return memo[state]
                if i == len(pts) - 1:
                    return 0.0

                curr = pts[i]
                best = float('inf')
                reachable = [
                    j for j in range(i + 1, len(pts))
                    if pts[j]['mile'] - curr['mile'] <= cap * mpg + 1e-6
                ]
                if not reachable:
                    return float('inf')

                candidate_buys = {0.0, cap - f}
                for j in reachable:
                    need = (pts[j]['mile'] - curr['mile']) / mpg
                    if f <= need <= cap:
                        candidate_buys.add(need - f)

                for buy in candidate_buys:
                    if buy < -1e-6 or f + buy > cap + 1e-6:
                        continue
                    buy = max(0.0, buy)
                    cost = buy * (curr['price'] if i > 0 else 0.0)
                    new_f = f + buy
                    for j in reachable:
                        d = pts[j]['mile'] - curr['mile']
                        if d / mpg <= new_f + 1e-6:
                            res = cost + solve(j, new_f - d / mpg)
                            if res < best:
                                best = res

                memo[state] = best
                return best

            ans = solve(0, cap)
            return ans if ans < float('inf') else None

        # Run test cases
        test_cases = [
            (
                900.0,
                [
                    {"mile": 200.0, "price": 3.40},
                    {"mile": 400.0, "price": 3.10},
                    {"mile": 650.0, "price": 3.80},
                ]
            ),
            (
                1400.0,
                [
                    {"mile": 300.0, "price": 3.90},
                    {"mile": 450.0, "price": 3.20},
                    {"mile": 800.0, "price": 3.60},
                    {"mile": 1100.0, "price": 3.30},
                ]
            ),
            (
                1600.0,
                [
                    {"mile": 250.0, "price": 3.15},
                    {"mile": 600.0, "price": 3.50},
                    {"mile": 900.0, "price": 3.10},
                    {"mile": 1300.0, "price": 3.40},
                ]
            ),
        ]

        for total_d, st_list in test_cases:
            candidates = [
                {
                    "opis_id": i + 1,
                    "name": f"Station {i}",
                    "price": Decimal(str(s["price"])),
                    "mile_marker": s["mile"],
                    "city": "", "state": "", "address": "",
                    "latitude": 0, "longitude": 0, "distance_from_route_miles": 0
                }
                for i, s in enumerate(st_list)
            ]
            plan = optimize_fuel_plan(candidates, total_d)
            ref_cost = dp_reference(st_list, total_d)

            self.assertIsNotNone(plan)
            self.assertIsNotNone(ref_cost)
            self.assertAlmostEqual(
                float(plan["fuel_summary"]["total_fuel_cost"]),
                ref_cost,
                places=2
            )


# ---------------------------------------------------------------------------
# 3. API Tests (All HTTP Mocked, No Network)
# ---------------------------------------------------------------------------
class APITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

        # Seed test database with stations
        FuelStation.objects.create(
            opis_id=1,
            name="PA Stop 1",
            address="Exit 100",
            city="Breezewood",
            state="PA",
            price=Decimal("3.1500"),
            latitude=40.0,
            longitude=-78.0,
        )
        FuelStation.objects.create(
            opis_id=2,
            name="OH Stop 1",
            address="Exit 126",
            city="Hebron",
            state="OH",
            price=Decimal("2.9900"),
            latitude=39.95,
            longitude=-82.5,
        )
        FuelStation.objects.create(
            opis_id=3,
            name="IN Stop 1",
            address="Exit 15",
            city="Lake Station",
            state="IN",
            price=Decimal("3.4500"),
            latitude=41.5,
            longitude=-87.2,
        )

    @patch("routes.views.get_route")
    @patch("routes.views.resolve_location")
    def test_happy_path(self, mock_resolve, mock_route):
        # Mock geocoding
        mock_resolve.side_effect = [
            ({"lat": 40.7128, "lon": -74.0060, "display_name": "New York, NY"}, True),
            ({"lat": 41.8781, "lon": -87.6298, "display_name": "Chicago, IL"}, True),
        ]
        # Mock routing: ~790 miles
        mock_route.return_value = (
            {
                "distance_miles": 790.0,
                "duration_minutes": 890.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-74.0, 40.7],
                        [-78.0, 40.0],
                        [-82.5, 39.95],
                        [-87.2, 41.5],
                        [-87.6, 41.8],
                    ],
                },
            },
            True,
        )

        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "New York, NY", "finish": "Chicago, IL"},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Validate response structure
        self.assertIn("route", data)
        self.assertIn("vehicle", data)
        self.assertIn("fuel_stops", data)
        self.assertIn("fuel_summary", data)
        self.assertIn("map_url", data)
        self.assertIn("meta", data)

        # Numbers must be JSON numbers, not strings
        self.assertIsInstance(data["route"]["distance_miles"], (int, float))
        self.assertIsInstance(data["fuel_summary"]["total_fuel_cost"], (int, float))
        self.assertIsInstance(data["fuel_summary"]["total_gallons_consumed"], (int, float))
        self.assertIsInstance(data["meta"]["external_api_calls"], int)
        self.assertEqual(data["meta"]["external_api_calls"], 3)  # 2 geocodes + 1 route

    def test_400_missing_field(self):
        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "New York, NY"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error"]["code"], "invalid_input")

    def test_400_blank_field(self):
        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "   ", "finish": "Chicago, IL"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error"]["code"], "invalid_input")

    def test_400_same_start_and_finish(self):
        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "Chicago, IL", "finish": "chicago, il"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error"]["code"], "invalid_input")

    @patch("routes.views.resolve_location")
    def test_400_non_us_location(self, mock_resolve):
        mock_resolve.side_effect = LocationError("Location 'London, UK' resolved outside the USA.")

        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "London, UK", "finish": "New York, NY"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error"]["code"], "invalid_location")

    @patch("routes.views.resolve_location")
    @patch("routes.views.get_route")
    def test_502_provider_failure(self, mock_route, mock_resolve):
        mock_resolve.side_effect = [
            ({"lat": 40.7, "lon": -74.0, "display_name": "New York, NY"}, True),
            ({"lat": 41.8, "lon": -87.6, "display_name": "Chicago, IL"}, True),
        ]
        mock_route.side_effect = RoutingError("Routing provider unavailable (502).")

        resp = self.client.post(
            "/api/v1/route/",
            data={"start": "New York, NY", "finish": "Chicago, IL"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(resp.json()["error"]["code"], "routing_provider_error")

    @patch("routes.views.get_route")
    @patch("routes.views.resolve_location")
    def test_external_api_calls_and_cache_hit(self, mock_resolve, mock_route):
        # First call: simulate 1 uncached external call
        mock_resolve.side_effect = [
            ({"lat": 40.7, "lon": -74.0, "display_name": "New York, NY"}, False),
            ({"lat": 41.8, "lon": -87.6, "display_name": "Chicago, IL"}, False),
        ]
        mock_route.return_value = (
            {
                "distance_miles": 790.0,
                "duration_minutes": 890.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-74.0, 40.7],
                        [-78.0, 40.0],
                        [-82.5, 39.95],
                        [-87.2, 41.5],
                        [-87.6, 41.8],
                    ],
                },
            },
            True,  # 1 external call made
        )

        resp1 = self.client.post(
            "/api/v1/route/",
            data={"start": "New York, NY", "finish": "Chicago, IL"},
            content_type="application/json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        meta1 = resp1.json()["meta"]
        self.assertEqual(meta1["external_api_calls"], 1)
        self.assertFalse(meta1["cache_hit"])

        # Second identical request: all cached
        mock_resolve.side_effect = [
            ({"lat": 40.7, "lon": -74.0, "display_name": "New York, NY"}, False),
            ({"lat": 41.8, "lon": -87.6, "display_name": "Chicago, IL"}, False),
        ]
        mock_route.return_value = (
            {
                "distance_miles": 790.0,
                "duration_minutes": 890.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-74.0, 40.7],
                        [-78.0, 40.0],
                        [-82.5, 39.95],
                        [-87.2, 41.5],
                        [-87.6, 41.8],
                    ],
                },
            },
            False,  # 0 external calls (cache hit)
        )

        resp2 = self.client.post(
            "/api/v1/route/",
            data={"start": "New York, NY", "finish": "Chicago, IL"},
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        meta2 = resp2.json()["meta"]
        self.assertEqual(meta2["external_api_calls"], 0)
        self.assertTrue(meta2["cache_hit"])

    @patch("routes.views.get_route")
    @patch("routes.views.resolve_location")
    def test_map_html_view(self, mock_resolve, mock_route):
        mock_resolve.side_effect = [
            ({"lat": 40.7, "lon": -74.0, "display_name": "New York, NY"}, False),
            ({"lat": 41.8, "lon": -87.6, "display_name": "Chicago, IL"}, False),
        ]
        mock_route.return_value = (
            {
                "distance_miles": 790.0,
                "duration_minutes": 890.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-74.0, 40.7],
                        [-78.0, 40.0],
                        [-82.5, 39.95],
                        [-87.2, 41.5],
                        [-87.6, 41.8],
                    ],
                },
            },
            False,
        )

        resp = self.client.get("/api/v1/route/map/?start=New+York%2C+NY&finish=Chicago%2C+IL")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertContains(resp, "Fuel Route Optimizer")
        self.assertContains(resp, "790.0 mi")

