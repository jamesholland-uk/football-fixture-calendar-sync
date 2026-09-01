#!/usr/bin/env python3
"""
Test the production geocoding pipeline in geocoding.py (not a method comparison).

Pipeline (inputs: venue name + optional team name only; see geocoding.py):
  web-search postcode → Google Geocoding → Places Find Place when needed →
  postcode-district checks.

CSV address distance is a proxy only. Open the printed Google Maps URLs
(or tap a Spond location) to confirm the pin.

Usage:
    python3 test_geo_chosen.py --google-api-key YOUR_KEY
"""

from __future__ import annotations

import csv
import math
import os
import sys

from geocoding import (
    extract_postcode,
    geocode_chosen,
    geocode_google,
    geocode_postcode_io,
)


def load_test_venues(csv_path: str | None = None) -> list[tuple[str, str, str]]:
    if csv_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        csv_path = os.path.join(script_dir, "test-data.csv")

    venues: list[tuple[str, str, str]] = []
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
    return venues


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _status(distance: float) -> str:
    if distance < 100:
        return "✅"
    if distance < 500:
        return "⚠️"
    return "❌"


def test_geo_chosen(google_api_key: str | None = None) -> None:
    venues = load_test_venues()

    print("=" * 80)
    print("GEO CHOSEN METHOD TEST")
    print("=" * 80)
    print("Pipeline: Google if full postcode matches web search; web postcode if")
    print("same district but different unit; Google if districts differ;")
    print("web postcode if Google is generic or has no postcode.")
    print("CSV address is ground truth only, not an input.")
    print("")

    summary: list[tuple[str, str, float, str]] = []

    for venue_name, true_address, team_name in venues:
        print(f"\n{'─' * 70}")
        print(f"VENUE: {venue_name}")
        if team_name:
            print(f"TEAM (context only for postcode search): {team_name}")
        print(f"GROUND TRUTH (not used as input): {true_address}")

        reference = None
        if google_api_key:
            reference = geocode_google(true_address, google_api_key)
        truth_pc = extract_postcode(true_address)
        if not reference and truth_pc:
            reference = geocode_postcode_io(truth_pc)

        if not reference:
            print("  Could not score (no ground-truth coordinates)")
            summary.append((venue_name, "NO REFERENCE", -1, ""))
            continue

        print(f"  Reference: {reference['lat']:.6f}, {reference['lon']:.6f}")
        print("  Running chosen method...")

        result, reason = geocode_chosen(venue_name, team_name, google_api_key)
        if not result:
            print(f"  FAILED: {reason}")
            summary.append((venue_name, "FAILED", -1, ""))
            continue

        dist = haversine_distance(reference["lat"], reference["lon"], result["lat"], result["lon"])
        maps_url = (
            f"https://www.google.com/maps/search/?api=1&query={result['lat']},{result['lon']}"
        )
        print(f"  Chose: {result['source']} — {result['display']}")
        print(f"  Why: {reason}")
        print(f"  Distance to CSV address (proxy only): {_status(dist)} {dist:.0f}m")
        print(f"  Maps pin (what a tap should open): {maps_url}")
        summary.append((venue_name, result["source"], dist, maps_url))

    print("\n" + "=" * 80)
    print("SUMMARY — CSV address distance is a proxy; open Maps pins for the real check")
    print("=" * 80)
    distances = [d for _, _, d, _ in summary if d >= 0]
    for venue, source, distance, maps_url in summary:
        if distance < 0:
            print(f"❌ {venue[:40]:<40} -> {source}")
        else:
            print(f"{_status(distance)} {venue[:40]:<40} -> {source:<14} ({distance:.0f}m)")
            print(f"   {maps_url}")

    if distances:
        within_100 = sum(1 for d in distances if d < 100)
        within_500 = sum(1 for d in distances if d < 500)
        print("")
        print(f"Within 100m: {within_100}/{len(distances)}")
        print(f"Within 500m: {within_500}/{len(distances)}")
        print(f"Average:     {sum(distances) / len(distances):.0f}m")
        print(f"Worst:       {max(distances):.0f}m")


if __name__ == "__main__":
    api_key = None
    if len(sys.argv) > 2 and sys.argv[1] == "--google-api-key":
        api_key = sys.argv[2]
        print(f"Using Google Maps API key: {api_key[:10]}...")
    else:
        print("No Google API key provided. Run with:")
        print("  python3 test_geo_chosen.py --google-api-key YOUR_KEY")
        print("Get a key at: https://console.cloud.google.com/google/maps-apis/credentials")

    test_geo_chosen(api_key)
