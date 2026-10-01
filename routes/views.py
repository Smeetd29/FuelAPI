import json
import time
import urllib.parse
from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .fuel import plan_route_fuel, InfeasibleRouteError
from .geocoding import resolve_location, LocationError
from .geo import simplify_geometry
from .routing import get_route, RoutingError
from .serializers import RouteRequestSerializer


class RouteOptimizeView(APIView):
    """
    POST /api/v1/route/
    Calculates the driving route between start and finish locations in the USA,
    identifies cost-optimal fuel stops along the route using a vehicle with 500-mile range
    and 10 mpg, and computes total fuel expenditure.
    """

    def post(self, request):
        t0 = time.time()
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            first_err = next(iter(serializer.errors.values()))
            err_msg = first_err[0] if isinstance(first_err, list) else str(first_err)
            return Response(
                {"error": {"code": "invalid_input", "message": err_msg}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_text = serializer.validated_data["start"]
        finish_text = serializer.validated_data["finish"]

        # 1. Resolve start and finish locations
        try:
            start_data, start_called = resolve_location(start_text)
            finish_data, finish_called = resolve_location(finish_text)
        except LocationError as e:
            return Response(
                {"error": {"code": "invalid_location", "message": str(e)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2. Get directions route
        try:
            route_data, route_called = get_route(
                start_data["lat"], start_data["lon"],
                finish_data["lat"], finish_data["lon"]
            )
        except RoutingError as e:
            return Response(
                {"error": {"code": "routing_provider_error", "message": str(e)}},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        total_distance = route_data["distance_miles"]
        raw_coords = route_data["geometry"]["coordinates"]

        # 3. Optimize fuel stops
        try:
            fuel_plan = plan_route_fuel(raw_coords, total_distance)
        except InfeasibleRouteError as e:
            return Response(
                {"error": {"code": "no_feasible_route", "message": str(e)}},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Simplify geometry for fast transmission and map rendering
        simplified_coords = simplify_geometry(raw_coords, epsilon_degrees=0.0005)

        external_api_calls = int(start_called) + int(finish_called) + int(route_called)
        cache_hit = external_api_calls == 0
        elapsed_ms = round((time.time() - t0) * 1000.0, 2)

        # Convert Decimals to rounded JSON numbers at response boundary
        formatted_fuel_stops = []
        for stop in fuel_plan["fuel_stops"]:
            formatted_fuel_stops.append({
                "opis_id": stop["opis_id"],
                "name": stop["name"],
                "address": stop["address"],
                "city": stop["city"],
                "state": stop["state"],
                "latitude": round(stop["latitude"], 6),
                "longitude": round(stop["longitude"], 6),
                "price_per_gallon": round(float(stop["price_per_gallon"]), 3),
                "mile_marker": round(float(stop["mile_marker"]), 2),
                "distance_from_route_miles": round(float(stop["distance_from_route_miles"]), 2),
                "gallons_purchased": round(float(stop["gallons_purchased"]), 2),
                "cost": round(float(stop["cost"]), 2),
            })

        summary = fuel_plan["fuel_summary"]
        formatted_summary = {
            "total_gallons_consumed": round(float(summary["total_gallons_consumed"]), 2),
            "total_gallons_purchased": round(float(summary["total_gallons_purchased"]), 2),
            "total_fuel_cost": round(float(summary["total_fuel_cost"]), 2),
        }

        map_query = urllib.parse.urlencode({"start": start_text, "finish": finish_text})
        map_url = f"/api/v1/route/map/?{map_query}"

        response_payload = {
            "route": {
                "start": start_data,
                "finish": finish_data,
                "distance_miles": round(total_distance, 2),
                "duration_minutes": round(route_data["duration_minutes"], 2),
                "geometry": {
                    "type": "LineString",
                    "coordinates": simplified_coords,
                },
            },
            "vehicle": {
                "mpg": 10,
                "max_range_miles": 500,
            },
            "fuel_stops": formatted_fuel_stops,
            "fuel_summary": formatted_summary,
            "map_url": map_url,
            "meta": {
                "external_api_calls": external_api_calls,
                "cache_hit": cache_hit,
                "elapsed_ms": elapsed_ms,
            },
        }

        return Response(response_payload, status=status.HTTP_200_OK)


class RouteMapView(APIView):
    """
    GET /api/v1/route/map/?start=...&finish=...
    Interactive Leaflet map visualization displaying the route polyline,
    numbered fuel stop markers with pricing details, and overall trip summary.
    """

    def get(self, request):
        start_text = request.GET.get("start", "").strip()
        finish_text = request.GET.get("finish", "").strip()

        if not start_text or not finish_text:
            return render(
                request,
                "map.html",
                {
                    "error": "Both 'start' and 'finish' query parameters are required.",
                    "start": start_text,
                    "finish": finish_text,
                },
                status=400,
            )

        if start_text.lower() == finish_text.lower():
            return render(
                request,
                "map.html",
                {
                    "error": "Start and finish locations cannot be the same.",
                    "start": start_text,
                    "finish": finish_text,
                },
                status=400,
            )

        try:
            start_data, _ = resolve_location(start_text)
            finish_data, _ = resolve_location(finish_text)
        except LocationError as e:
            return render(
                request,
                "map.html",
                {"error": str(e), "start": start_text, "finish": finish_text},
                status=400,
            )

        try:
            route_data, _ = get_route(
                start_data["lat"], start_data["lon"],
                finish_data["lat"], finish_data["lon"]
            )
        except RoutingError as e:
            return render(
                request,
                "map.html",
                {"error": f"Routing provider error: {e}", "start": start_text, "finish": finish_text},
                status=502,
            )

        total_distance = route_data["distance_miles"]
        raw_coords = route_data["geometry"]["coordinates"]

        try:
            fuel_plan = plan_route_fuel(raw_coords, total_distance)
        except InfeasibleRouteError as e:
            return render(
                request,
                "map.html",
                {"error": str(e), "start": start_text, "finish": finish_text},
                status=422,
            )

        simplified_coords = simplify_geometry(raw_coords, epsilon_degrees=0.0005)

        stops = []
        for i, s in enumerate(fuel_plan["fuel_stops"], 1):
            stops.append({
                "number": i,
                "opis_id": s["opis_id"],
                "name": s["name"],
                "address": s["address"],
                "city": s["city"],
                "state": s["state"],
                "latitude": round(s["latitude"], 6),
                "longitude": round(s["longitude"], 6),
                "price_per_gallon": round(float(s["price_per_gallon"]), 3),
                "mile_marker": round(float(s["mile_marker"]), 1),
                "gallons_purchased": round(float(s["gallons_purchased"]), 2),
                "cost": round(float(s["cost"]), 2),
            })

        duration_hours = int(route_data["duration_minutes"] // 60)
        duration_mins = int(round(route_data["duration_minutes"] % 60))
        duration_str = f"{duration_hours}h {duration_mins}m" if duration_hours else f"{duration_mins}m"

        context = {
            "start": start_text,
            "finish": finish_text,
            "start_display": start_data["display_name"],
            "finish_display": finish_data["display_name"],
            "distance_miles": f"{total_distance:.1f}",
            "duration_str": duration_str,
            "total_cost": f"${float(fuel_plan['fuel_summary']['total_fuel_cost']):.2f}",
            "gallons_purchased": f"{float(fuel_plan['fuel_summary']['total_gallons_purchased']):.2f}",
            "gallons_consumed": f"{float(fuel_plan['fuel_summary']['total_gallons_consumed']):.2f}",
            "stops_count": len(stops),
            "fuel_stops": stops,
            "geometry_json": json.dumps(simplified_coords),
            "stops_json": json.dumps(stops),
            "start_coords": json.dumps([start_data["lat"], start_data["lon"]]),
            "finish_coords": json.dumps([finish_data["lat"], finish_data["lon"]]),
        }

        return render(request, "map.html", context)
