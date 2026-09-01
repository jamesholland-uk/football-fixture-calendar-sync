#!/usr/bin/env python3
"""
Create labelled [TEST GEO] Spond events for each row in test-data.csv.

Only the logged-in Spond user is invited (the team is not notified).
Events are scheduled on the coming Sunday so they are easy to find and delete.

Usage:
    # Preview locations only
    python3 test_spond_locations.py

    # Create events in Spond
    python3 test_spond_locations.py --create

Loads SPOND_* and GOOGLE_MAPS_API_KEY from the environment or .env
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from geocoding import geocode_chosen, to_spond_location


def load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def load_test_venues() -> list[tuple[str, str, str]]:
    csv_path = Path(__file__).resolve().parent / "test-data.csv"
    venues: list[tuple[str, str, str]] = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                venue_name = row[0].strip()
                venue_address = row[1].strip()
                team_name = row[2].strip() if len(row) > 2 else ""
                if venue_name and venue_address:
                    venues.append((venue_name, venue_address, team_name))
    return venues


def next_sunday_local(tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    days_ahead = (6 - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= 12:
        days_ahead = 7
    day = (now + timedelta(days=days_ahead)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return day


async def create_test_events(do_create: bool) -> None:
    email = os.environ.get("SPOND_EMAIL", "")
    password = os.environ.get("SPOND_PASSWORD", "")
    group_ids = [g.strip() for g in os.environ.get("SPOND_GROUP_IDS", "").split(",") if g.strip()]
    host_ids = [h.strip() for h in os.environ.get("SPOND_HOST_IDS", "").split(",") if h.strip()]
    tz_name = os.environ.get("TZ", "Europe/London")

    if not email or not password or not group_ids:
        print("ERROR: Set SPOND_EMAIL, SPOND_PASSWORD, and SPOND_GROUP_IDS in .env")
        sys.exit(1)

    group_id = group_ids[0]
    host_id = host_ids[0] if host_ids else ""
    venues = load_test_venues()
    print(f"Venues: {len(venues)}")
    print(f"Spond group: {group_id}")
    print(f"Mode: {'CREATE' if do_create else 'PREVIEW (pass --create to post to Spond)'}")
    print("")

    from spond import spond

    s = spond.Spond(username=email, password=password)
    await s.login()
    groups = await s.get_groups()
    group_data = next((g for g in groups if g["id"] == group_id), None)
    if not group_data:
        print(f"Spond group {group_id} not found")
        await s.clientsession.close()
        sys.exit(1)

    members = group_data.get("members", [])
    me = next(
        (m for m in members if (m.get("email") or "").lower() == email.lower()),
        None,
    )
    invitee_id = host_id or (me["id"] if me else None)
    if not invitee_id:
        print("ERROR: Could not find your member id in the group (set SPOND_HOST_IDS)")
        await s.clientsession.close()
        sys.exit(1)

    print(f"Inviting only member {invitee_id} (not the whole team)")
    start_day = next_sunday_local(tz_name)
    created = 0
    failed = 0

    try:
        for i, (venue_name, true_address, team_name) in enumerate(venues):
            print(f"\n{'─' * 70}")
            print(f"{i + 1}/{len(venues)} {venue_name}")
            result, reason = geocode_chosen(venue_name, team_name)
            if not result:
                print(f"  SKIP: geocoding failed ({reason})")
                failed += 1
                continue

            location = to_spond_location(result, venue_name)
            start_local = start_day + timedelta(hours=i)
            end_local = start_local + timedelta(minutes=60)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)
            heading = f"[TEST GEO] {i + 1}/{len(venues)} {venue_name}"
            description = "\n".join(
                [
                    "TEST EVENT — delete after checking the map pin.",
                    f"Source: {result.get('source')}",
                    f"Why: {reason}",
                    f"True address (not used for geocoding): {true_address}",
                    f"Team hint: {team_name or '(none)'}",
                ]
            )

            print(f"  {heading}")
            print(f"  {start_local.strftime('%a %d %b %H:%M')}  {location.get('addressLine')}")
            print(f"  pin: {location['latitude']:.6f}, {location['longitude']:.6f}")

            if not do_create:
                continue

            event_data = {
                "heading": heading,
                "description": description,
                "spondType": "EVENT",
                "startTimestamp": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "endTimestamp": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "meetupPrior": 30,
                "commentsDisabled": True,
                "maxAccepted": 0,
                "rsvpDate": None,
                "location": location,
                "visibility": "INVITEES",
                "participantsHidden": False,
                "autoReminderType": "DISABLED",
                "autoAccept": False,
                "attachments": [],
                "recipients": {
                    "group": {"id": group_id, "members": [{"id": invitee_id}]},
                },
            }
            if host_id:
                event_data["owners"] = [{"id": host_id}]

            url = f"{s.api_url}sponds/"
            async with s.clientsession.post(url, json=event_data, headers=s.auth_headers) as r:
                if r.ok:
                    print("  Created")
                    created += 1
                else:
                    error = await r.text()
                    print(f"  Failed: {r.status} - {error}")
                    failed += 1
    finally:
        await s.clientsession.close()

    print("\n" + "=" * 70)
    if do_create:
        print(f"Created {created}, failed/skipped {failed}")
        print("Open Spond, find events titled [TEST GEO], tap each location, then delete them.")
    else:
        print("Preview only. Re-run with --create to post events to Spond.")


if __name__ == "__main__":
    load_dotenv()
    do_create = "--create" in sys.argv
    asyncio.run(create_test_events(do_create))
