import csv
import io
import urllib.request
import zipfile
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from routes.geocoding import normalize_city


class Command(BaseCommand):
    help = "Downloads US Census Gazetteer and GeoNames data to generate data/us_places.csv"

    CENSUS_PLACES_URL = (
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
        "2023_Gazetteer/2023_Gaz_place_national.zip"
    )
    CENSUS_COUSUB_URL = (
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
        "2023_Gazetteer/2023_Gaz_cousubs_national.zip"
    )
    GEONAMES_US_URL = "https://download.geonames.org/export/zip/US.zip"

    def handle(self, *args, **options):
        self.stdout.write("Building US places gazetteer...")
        places: dict[tuple[str, str], tuple[float, float]] = {}

        # 1. Download and parse US Census Places National File
        self.stdout.write(f"Downloading US Census Places from {self.CENSUS_PLACES_URL}...")
        try:
            req = urllib.request.Request(
                self.CENSUS_PLACES_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            zf = zipfile.ZipFile(io.BytesIO(data))
            content = zf.open(zf.namelist()[0]).read().decode("utf-8", errors="ignore")
            reader = csv.reader(content.splitlines(), delimiter="\t")
            header = [h.strip() for h in next(reader)]
            usps_idx = header.index("USPS")
            name_idx = header.index("NAME")
            lat_idx = header.index("INTPTLAT")
            lon_idx = header.index("INTPTLONG")

            census_count = 0
            for row in reader:
                if len(row) <= max(usps_idx, name_idx, lat_idx, lon_idx):
                    continue
                state = row[usps_idx].strip().upper()
                raw_name = row[name_idx].strip()
                norm_name = normalize_city(raw_name)
                try:
                    lat = float(row[lat_idx].strip())
                    lon = float(row[lon_idx].strip())
                except ValueError:
                    continue

                key = (norm_name, state)
                if key not in places:
                    places[key] = (lat, lon)
                    census_count += 1

            self.stdout.write(f"Loaded {census_count} places from Census Gazetteer.")
        except Exception as e:
            self.stderr.write(f"Warning: Failed to fetch Census Places ({e}).")

        # 2. Download and parse US Census County Subdivisions (for townships/boroughs)
        self.stdout.write(f"Downloading US Census County Subdivisions from {self.CENSUS_COUSUB_URL}...")
        try:
            req = urllib.request.Request(
                self.CENSUS_COUSUB_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            zf = zipfile.ZipFile(io.BytesIO(data))
            content = zf.open(zf.namelist()[0]).read().decode("utf-8", errors="ignore")
            reader = csv.reader(content.splitlines(), delimiter="\t")
            header = [h.strip() for h in next(reader)]
            usps_idx = header.index("USPS")
            name_idx = header.index("NAME")
            lat_idx = header.index("INTPTLAT")
            lon_idx = header.index("INTPTLONG")

            cousub_count = 0
            for row in reader:
                if len(row) <= max(usps_idx, name_idx, lat_idx, lon_idx):
                    continue
                state = row[usps_idx].strip().upper()
                raw_name = row[name_idx].strip()
                norm_name = normalize_city(raw_name)
                try:
                    lat = float(row[lat_idx].strip())
                    lon = float(row[lon_idx].strip())
                except ValueError:
                    continue

                key = (norm_name, state)
                if key not in places:
                    places[key] = (lat, lon)
                    cousub_count += 1

            self.stdout.write(f"Loaded {cousub_count} additional places from Census Subdivisions.")
        except Exception as e:
            self.stderr.write(f"Warning: Failed to fetch Census Subdivisions ({e}).")

        # 3. GeoNames US Postal / Places fallback for unincorporated communities
        self.stdout.write(f"Downloading GeoNames US fallback from {self.GEONAMES_US_URL}...")
        try:
            req = urllib.request.Request(
                self.GEONAMES_US_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            zf = zipfile.ZipFile(io.BytesIO(data))
            content = zf.open("US.txt").read().decode("utf-8", errors="ignore")
            reader = csv.reader(content.splitlines(), delimiter="\t")
            geonames_count = 0
            for row in reader:
                if len(row) < 11:
                    continue
                raw_name = row[2].strip()
                state = row[4].strip().upper()
                norm_name = normalize_city(raw_name)
                try:
                    lat = float(row[9].strip())
                    lon = float(row[10].strip())
                except ValueError:
                    continue

                key = (norm_name, state)
                if key not in places:
                    places[key] = (lat, lon)
                    geonames_count += 1

            self.stdout.write(f"Loaded {geonames_count} additional places from GeoNames fallback.")
        except Exception as e:
            self.stderr.write(f"Warning: Failed to fetch GeoNames US fallback ({e}).")

        # Write data/us_places.csv
        data_dir = settings.BASE_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        output_file = data_dir / "us_places.csv"

        self.stdout.write(f"Writing {len(places)} records to {output_file}...")
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["city_norm", "state", "lat", "lon"])
            for (city_norm, state), (lat, lon) in sorted(places.items()):
                writer.writerow([city_norm, state, f"{lat:.6f}", f"{lon:.6f}"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully generated {output_file} with {len(places)} places."
            )
        )
