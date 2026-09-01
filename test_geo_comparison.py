#!/usr/bin/env python3
"""
Compare geocoding methods against known venue addresses.

This script is for evaluating Nominatim vs Google vs postcode lookup.
It is not the production pipeline.

Usage:
    python3 test_geo_comparison.py --google-api-key YOUR_KEY

Reads test data from test-data.csv (venue name, true address, optional team).
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request


def load_test_venues(csv_path: str | None = None) -> list[tuple[str, str, str]]:
    """Load venues from CSV: Venue Name, Venue Address, Team Name (optional)."""
    if csv_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        csv_path = os.path.join(script_dir, "test-data.csv")

    venues: list[tuple[str, str, str]] = []
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 2:
                    venue_name = row[0].strip()
                    venue_address = row[1].strip()
                    team_name = row[2].strip() if len(row) > 2 else ""
                    if venue_name and venue_address:
                        venues.append((venue_name, venue_address, team_name))
        print(f"Loaded {len(venues)} venues from {csv_path}")
    except FileNotFoundError:
        print(f"ERROR: Could not find {csv_path}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to read {csv_path}: {e}")
        sys.exit(1)
    return venues


def extract_postcode(text: str) -> str | None:
    match = re.search(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})", text.upper())
    return match.group(1) if match else None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def geocode_nominatim(query: str) -> dict | None:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&addressdetails=1&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "GeoComparisonTest/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode())
            if results:
                r = results[0]
                return {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "display": r.get("display_name", "")[:60],
                    "source": "nominatim",
                }
    except Exception as e:
        print(f"    Nominatim error: {e}")
    return None


def geocode_postcode_io(postcode: str) -> dict | None:
    try:
        clean = postcode.replace(" ", "").upper()
        url = f"https://api.postcodes.io/postcodes/{clean}"
        req = urllib.request.Request(url, headers={"User-Agent": "GeoComparisonTest/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == 200 and data.get("result"):
                r = data["result"]
                return {
                    "lat": r["latitude"],
                    "lon": r["longitude"],
                    "display": f"{r.get('admin_ward', '')}, {r.get('admin_district', '')}",
                    "source": "postcode.io",
                }
    except Exception as e:
        print(f"    postcodes.io error: {e}")
    return None


def geocode_google(query: str, api_key: str) -> dict | None:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "OK" and data.get("results"):
                r = data["results"][0]
                loc = r["geometry"]["location"]
                return {
                    "lat": loc["lat"],
                    "lon": loc["lng"],
                    "display": r.get("formatted_address", "")[:60],
                    "source": "google",
                }
            if data.get("status") == "REQUEST_DENIED":
                print(f"    Google API error: {data.get('error_message', 'Request denied')}")
    except Exception as e:
        print(f"    Google error: {e}")
    return None


def _status(distance: float) -> str:
    if distance < 100:
        return "✅"
    if distance < 500:
        return "⚠️"
    return "❌"


def test_geo_comparison(google_api_key: str | None = None) -> None:
    test_venues = load_test_venues()

    print("=" * 80)
    print("GEO COMPARISON TEST")
    print("=" * 80)
    print("Compares Nominatim, Google (venue / venue+team), and postcode lookup.")
    print("The true address is used only as the reference location, except for")
    print("postcode lookup which uses the postcode extracted from that address.")
    print("")

    results_summary: list[tuple[str, str, float]] = []

    for venue_name, full_address, opponent_team in test_venues:
        print(f"\n{'─' * 70}")
        print(f"VENUE: {venue_name}")
        print(f"TRUE ADDRESS: {full_address}")
        if opponent_team:
            print(f"TEAM: {opponent_team}")

        postcode = extract_postcode(full_address)

        reference = None
        if google_api_key:
            reference = geocode_google(full_address, google_api_key)
            if reference:
                print(f"  Reference (Google): {reference['lat']:.6f}, {reference['lon']:.6f}")
        if not reference and postcode:
            reference = geocode_postcode_io(postcode)
            if reference:
                print(f"  Reference (Postcode): {reference['lat']:.6f}, {reference['lon']:.6f}")

        if not reference:
            print("  Could not get reference location")
            results_summary.append((venue_name, "NO REFERENCE", -1))
            continue

        print("\n  Method comparison:")
        test_methods: list[tuple[str, dict, float]] = []

        print(f"  → Nominatim '{venue_name}, Birmingham, UK'...", end=" ")
        nom_result = geocode_nominatim(f"{venue_name}, Birmingham, UK")
        if nom_result:
            dist = haversine_distance(reference["lat"], reference["lon"], nom_result["lat"], nom_result["lon"])
            test_methods.append(("Nominatim", nom_result, dist))
            print(f"found ({dist:.0f}m)")
        else:
            print("NO RESULTS")

        if google_api_key:
            print(f"  → Google '{venue_name}, Birmingham, UK'...", end=" ")
            g_venue = geocode_google(f"{venue_name}, Birmingham, UK", google_api_key)
            if g_venue:
                if g_venue["display"].lower() in ["birmingham, uk", "birmingham, west midlands, uk"]:
                    print(f"TOO GENERIC ({g_venue['display']})")
                else:
                    dist = haversine_distance(reference["lat"], reference["lon"], g_venue["lat"], g_venue["lon"])
                    test_methods.append(("Google (venue only)", g_venue, dist))
                    print(f"found ({dist:.0f}m)")
            else:
                print("NO RESULTS")

            if opponent_team:
                query = f"{venue_name} {opponent_team}, Birmingham, UK"
                print(f"  → Google '{query[:55]}...'...", end=" ")
                g_team = geocode_google(query, google_api_key)
                if g_team:
                    if g_team["display"].lower() in ["birmingham, uk", "birmingham, west midlands, uk"]:
                        print("TOO GENERIC")
                    else:
                        dist = haversine_distance(reference["lat"], reference["lon"], g_team["lat"], g_team["lon"])
                        test_methods.append(("Google (with team)", g_team, dist))
                        print(f"found ({dist:.0f}m)")
                else:
                    print("NO RESULTS")

        if postcode:
            pc_result = geocode_postcode_io(postcode)
            if pc_result:
                dist = haversine_distance(reference["lat"], reference["lon"], pc_result["lat"], pc_result["lon"])
                test_methods.append(("Postcode (from true address)", pc_result, dist))

        if test_methods:
            print("\n  Results (closest first):")
            for method_name, result, distance in sorted(test_methods, key=lambda x: x[2]):
                print(f"    {_status(distance)} {method_name}: {distance:.0f}m")
                print(f"       Found: {result['display']}")
            best = min(test_methods, key=lambda x: x[2])
            results_summary.append((venue_name, best[0], best[2]))
        else:
            results_summary.append((venue_name, "FAILED", -1))

    print("\n" + "=" * 80)
    print("SUMMARY - Closest method per venue (oracle: knows true address)")
    print("=" * 80)
    for venue, best_method, distance in results_summary:
        if distance < 0:
            print(f"❌ {venue[:40]:<40} -> {best_method}")
        else:
            print(f"{_status(distance)} {venue[:40]:<40} -> {best_method:<28} ({distance:.0f}m)")


if __name__ == "__main__":
    api_key = None
    if len(sys.argv) > 2 and sys.argv[1] == "--google-api-key":
        api_key = sys.argv[2]
        print(f"Using Google Maps API key: {api_key[:10]}...")
    else:
        print("No Google API key provided. Run with:")
        print("  python3 test_geo_comparison.py --google-api-key YOUR_KEY")
        print("Get a key at: https://console.cloud.google.com/google/maps-apis/credentials")

    test_geo_comparison(api_key)
