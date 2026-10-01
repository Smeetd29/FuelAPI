# Fuel Route Optimizer

## Project Overview

This project is a fuel route planner for long-haul trips across the United States. Given a start and finish location, it calculates the driving route, identifies fuel stations within 5 miles of the highway, and picks the most cost-effective stations to refuel so the vehicle never runs out of gas.

Vehicle rules:
- Tank capacity: 50 gallons
- Fuel economy: 10 miles per gallon
- Maximum range: 500 miles on a full tank
- Starting condition: Starts with a full, free tank at mile zero

---

## How It Works

### 1. Getting the Route
When you provide a start and finish city, the application resolves them to coordinates and queries a routing service (OpenRouteService or OSRM). The router returns the total distance and roughly 3,000 GPS coordinates that trace the actual highway path.

### 2. Finding Fuel Stations Along the Route
The database contains thousands of fuel stations across the country. Comparing every station against all 3,000 road points would require millions of distance checks and slow down the app. Instead, it finds stations in two steps:

- Step A (Bounding Box Filter): The application scans all 3,000 road points to find the minimum and maximum latitude (north and south) and longitude (east and west). It adds a 5-mile buffer around these edges and runs a fast database query to select only stations within that geographic rectangle. This quickly eliminates stations in distant states.

- Step B (Road Distance Filter): A station inside the rectangle might still be far from the actual highway. For example, Pittsburgh falls inside the rectangle for a New York to Chicago route, but the interstate passes 30 miles north of it. Python calculates the exact perpendicular distance from each station to the road line and drops anything farther than 5 miles away. Each surviving station is also assigned its exact mile marker along the trip.

### 3. Choosing Fuel Stops
Once the nearby stations are identified, the optimizer selects where to stop:
- Trips of 500 miles or less require zero stops and zero cost because the truck starts with a full tank.
- For longer trips, the algorithm looks ahead within the truck's remaining fuel range and selects the cheapest stations, ensuring the gap between any two consecutive stops never exceeds 500 miles.

### 4. Interactive Map
The application includes a web interface built with Leaflet. It draws the route on a map, places numbered markers at each chosen fuel stop, and shows the price, gallons purchased, and cost.

---

## Data Preparation

The provided fuel prices CSV contained station names, cities, and prices, but no geographic coordinates. To avoid hitting external API rate limits, all stations were geocoded completely offline:
- US Census Bureau and GeoNames gazetteer datasets were used to build a local lookup table of US cities and their centroid coordinates.
- Canadian records were dropped, duplicates were resolved by selecting the lowest price per station ID, and names were normalized.
- All US stations were matched against the local gazetteer and loaded into the database with zero external API calls.

---

## How to Run

1. Create and activate a virtual environment:
   python -m venv .venv
   .venv\Scripts\activate   (or source .venv/bin/activate on Linux/macOS)

2. Install dependencies:
   pip install -r requirements.txt

3. Initialize the database and load data:
   python manage.py migrate
   python manage.py build_places
   python manage.py load_fuel_data

4. Run tests:
   python manage.py test

5. Start the server:
   python manage.py runserver

Once the server is running, you can open the interactive map in your browser at:
http://127.0.0.1:8000/api/v1/route/map/?start=New+York,+NY&finish=Chicago,+IL
