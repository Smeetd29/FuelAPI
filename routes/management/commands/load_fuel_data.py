import csv
import difflib
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from routes.geocoding import normalize_city
from routes.models import FuelStation


class Command(BaseCommand):
    help = "Loads and geocodes fuel station data from CSV into FuelStation model"

    CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default="data/fuel-prices-for-be-assessment.csv",
            help="Path to the fuel prices CSV file"
        )

    def handle(self, *args, **options):
        file_path = Path(options["file"])
        if not file_path.is_absolute():
            file_path = settings.BASE_DIR / file_path

        if not file_path.exists():
            self.stderr.write(f"Error: File not found at {file_path}")
            return

        places_file = settings.BASE_DIR / "data" / "us_places.csv"
        if not places_file.exists():
            self.stderr.write(
                f"Error: {places_file} not found. Run 'python manage.py build_places' first."
            )
            return

        # 1. Load places gazetteer
        self.stdout.write("Loading places lookup table from data/us_places.csv...")
        places_lookup: dict[tuple[str, str], tuple[float, float]] = {}
        state_cities: dict[str, list[str]] = {}

        with open(places_file, "r", encoding="utf-8") as pf:
            p_reader = csv.DictReader(pf)
            for row in p_reader:
                c_norm = row["city_norm"]
                st = row["state"].upper()
                lat = float(row["lat"])
                lon = float(row["lon"])
                key = (c_norm, st)
                if key not in places_lookup:
                    places_lookup[key] = (lat, lon)
                    state_cities.setdefault(st, []).append(c_norm)

        self.stdout.write(f"Loaded {len(places_lookup)} unique city-state places.")

        # 2. Read and deduplicate CSV
        self.stdout.write(f"Reading {file_path}...")
        total_rows_read = 0
        non_us_skipped = 0
        stations_by_id: dict[int, dict] = {}
        duplicates_collapsed = 0

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows_read += 1
                state = row.get("State", "").strip().upper()
                if state in self.CANADIAN_PROVINCES or not state:
                    non_us_skipped += 1
                    continue

                opis_raw = row.get("OPIS Truckstop ID", "").strip()
                if not opis_raw:
                    continue
                try:
                    opis_id = int(opis_raw)
                    price = Decimal(row["Retail Price"].strip())
                except (ValueError, KeyError):
                    continue

                name = row.get("Truckstop Name", "").strip()
                address = row.get("Address", "").strip()
                city = row.get("City", "").strip()
                rack_raw = row.get("Rack ID", "").strip()
                rack_id = int(rack_raw) if rack_raw and rack_raw.isdigit() else None

                station_data = {
                    "opis_id": opis_id,
                    "name": name,
                    "address": address,
                    "city": city,
                    "state": state,
                    "rack_id": rack_id,
                    "price": price,
                }

                if opis_id in stations_by_id:
                    duplicates_collapsed += 1
                    # Keep the row with the LOWEST price
                    if price < stations_by_id[opis_id]["price"]:
                        stations_by_id[opis_id] = station_data
                else:
                    stations_by_id[opis_id] = station_data

        total_stations_loaded = len(stations_by_id)
        self.stdout.write(f"Total rows read: {total_rows_read}")
        self.stdout.write(f"Non-US rows skipped: {non_us_skipped}")
        self.stdout.write(f"Duplicate rows collapsed: {duplicates_collapsed}")
        self.stdout.write(f"Unique US stations to load: {total_stations_loaded}")

        # 3. Match each station against places
        geocoded_count = 0
        unmatched_stations = []

        station_models = []
        for opis_id, sdata in stations_by_id.items():
            norm_city = normalize_city(sdata["city"])
            state = sdata["state"]

            key = (norm_city, state)
            lat, lon = None, None

            if key in places_lookup:
                lat, lon = places_lookup[key]
            else:
                # Fuzzy match within same state
                possible_cities = state_cities.get(state, [])
                matches = difflib.get_close_matches(
                    norm_city, possible_cities, n=1, cutoff=0.85
                )
                if matches:
                    lat, lon = places_lookup[(matches[0], state)]
                else:
                    unmatched_stations.append((sdata["city"], state, opis_id))

            if lat is not None and lon is not None:
                geocoded_count += 1

            station_models.append(
                FuelStation(
                    opis_id=opis_id,
                    name=sdata["name"],
                    address=sdata["address"],
                    city=sdata["city"],
                    state=sdata["state"],
                    rack_id=sdata["rack_id"],
                    price=sdata["price"],
                    latitude=lat,
                    longitude=lon,
                )
            )

        # 4. Save to database idempotently in a transaction
        self.stdout.write("Persisting stations to database...")
        with transaction.atomic():
            # Delete existing records to allow clean idempotent re-runs
            FuelStation.objects.all().delete()
            # Bulk create in batches
            batch_size = 1000
            for i in range(0, len(station_models), batch_size):
                FuelStation.objects.bulk_create(station_models[i : i + batch_size])

        match_rate = (geocoded_count / total_stations_loaded * 100) if total_stations_loaded else 0.0

        self.stdout.write(self.style.SUCCESS("--- Fuel Station Loading Summary ---"))
        self.stdout.write(f"Rows read:              {total_rows_read}")
        self.stdout.write(f"Non-US skipped:         {non_us_skipped}")
        self.stdout.write(f"Duplicates collapsed:   {duplicates_collapsed}")
        self.stdout.write(f"Stations loaded:        {total_stations_loaded}")
        self.stdout.write(f"Stations geocoded:      {geocoded_count}")
        self.stdout.write(f"Unmatched stations:     {len(unmatched_stations)}")
        self.stdout.write(f"Match rate:             {match_rate:.2f}%")

        if unmatched_stations:
            self.stdout.write("Sample unmatched stations (first 5):")
            for city, st, sid in unmatched_stations[:5]:
                self.stdout.write(f"  - Station ID {sid}: {city}, {st}")
        else:
            self.stdout.write("All stations successfully geocoded!")
