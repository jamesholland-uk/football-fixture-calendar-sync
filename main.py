import os
import time
import json
import hashlib
import smtplib
import asyncio
import traceback
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import schedule
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from spond import spond

from geocoding import fixture_venue_name, geocode_venue
from spond_hosts import parse_spond_host_slots
from venue_addresses import find_venue_address, parse_venue_addresses
from venue_review import (
    build_venue_review_email,
    load_reviewed_venues,
    mark_venue_reviewed,
    venue_review_key,
)
from team_colours import kit_colour_for_fixture, load_team_colours

# Configuration from environment variables
FIXTURE_URLS = os.environ.get("FIXTURE_URLS", "").split(",")
CALENDAR_IDS = os.environ.get("CALENDAR_IDS", "").split(",")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "/app/service_account.json")
POLL_INTERVAL_HOURS = int(os.environ.get("POLL_INTERVAL_HOURS", "12"))
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
TZ = os.environ.get("TZ", "Europe/London")

# Team name translations (format: "Full Name:Short Name,Full Name 2:Short Name 2")
# Example: "Example FC U12:Owls,Other Team U11:Tigers"
TEAM_NAMES_RAW = os.environ.get("TEAM_NAMES", "")


# Email notification settings (optional)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")  # New-fixture notifications
EMAIL_TO_ADMIN = os.environ.get("EMAIL_TO_ADMIN", "")  # Failure/recovery alerts

# Spond integration (optional)
SPOND_EMAIL = os.environ.get("SPOND_EMAIL", "")
SPOND_PASSWORD = os.environ.get("SPOND_PASSWORD", "")
SPOND_GROUP_IDS = os.environ.get("SPOND_GROUP_IDS", "").split(",")  # One per team
SPOND_HOST_SLOTS = parse_spond_host_slots(os.environ.get("SPOND_HOST_IDS", ""))
# Pinned FA addresses so ambiguous short venue names are never guessed
VENUE_ADDRESSES = parse_venue_addresses(os.environ.get("VENUE_ADDRESSES", ""))

# Dry run mode - test without creating events
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
HEALTHCHECKS_PING_URL = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()

# True after a poll that sent a failure alert, so the next clean poll can send "recovered"
_last_poll_had_errors = False


def load_team_names() -> dict[str, str]:
    """Load team name translations from environment variable."""
    translations = {}
    if not TEAM_NAMES_RAW:
        print("TEAM_NAMES not configured")
        return translations
    
    print(f"TEAM_NAMES raw: {TEAM_NAMES_RAW}")
    
    for mapping in TEAM_NAMES_RAW.split(","):
        mapping = mapping.strip()
        if ":" in mapping:
            full_name, short_name = mapping.split(":", 1)
            translations[full_name.strip()] = short_name.strip()
            print(f"  Team mapping: '{full_name.strip()}' -> '{short_name.strip()}'")
    
    return translations


TEAM_NAMES = load_team_names()
TEAM_COLOURS = load_team_colours()


def append_detail_url(parts: list[str], fixture: dict) -> None:
    """Append the FA Full-Time fixture page URL, if we scraped one from the list."""
    url = (fixture.get("detail_url") or "").strip()
    if url:
        parts.append("")
        parts.append(url)


def smtp_configured() -> bool:
    return all([SMTP_USER, SMTP_PASSWORD, EMAIL_FROM])


def send_email(subject: str, body: str, to: str) -> bool:
    """Send an email via SMTP. Returns True on success."""
    recipients = [email.strip() for email in to.split(",") if email.strip()]
    if not smtp_configured() or not recipients:
        return False
    if DRY_RUN:
        # Success, so callers do not treat a dry run as a failure
        print(f"  [DRY RUN] Would email {', '.join(recipients)}: {subject}")
        return True
    try:
        to_header = ", ".join(recipients)
        msg = MIMEMultipart()
        msg["From"] = EMAIL_FROM
        msg["To"] = to_header
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        print(f"  Email sent to {to_header}: {subject}")
        return True
    except Exception as e:
        print(f"  Failed to send email: {e}")
        return False


def send_alert(subject: str, body: str) -> None:
    """Email a failure/recovery alert to EMAIL_TO_ADMIN (not fixture recipients)."""
    prefixed = f"[fixture-sync] {subject}"
    if not smtp_configured() or not EMAIL_TO_ADMIN.strip():
        print(f"  Alert (admin email not configured): {prefixed}")
        print(body)
        return
    send_email(prefixed, body, EMAIL_TO_ADMIN)


def reviewed_venues_path() -> Path:
    return Path(DATA_DIR) / "reviewed_venues.json"


def fixture_is_home(fixture: dict) -> tuple[str | None, bool]:
    """Return (kit colour, is_home) for a fixture.

    TEAM_COLOURS names are our teams; otherwise a TEAM_NAMES translation of the home
    side is used as a heuristic.
    """
    kit_colour, coloured_home = kit_colour_for_fixture(fixture, TEAM_COLOURS)
    if coloured_home is not None:
        return kit_colour, coloured_home
    return kit_colour, translate_team_name(fixture["home_team"]) != fixture["home_team"]


# (venue, hint, pinned address) -> geocoded location, so one venue is looked up once per
# run even if it is needed for Spond and Calendar, or for several fixtures. Only
# successful lookups are cached, so a failure is retried on the next fixture.
_LOCATION_CACHE: dict[tuple[str, str, str], dict] = {}


def resolve_venue_location(fixture: dict, is_home: bool) -> dict:
    """Spond-style location for a fixture's venue, used by Spond and Calendar.

    {} when the fixture has no venue, {"feature": venue} when geocoding failed, otherwise
    the geocoded dict (coordinates, address, postcode). A pinned VENUE_ADDRESSES entry
    wins; a venue with no pin is reported to the admin once for checking.
    """
    venue_raw = fixture.get("venue", "")
    if not venue_raw:
        return {}
    venue = fixture_venue_name(venue_raw)

    # An away game is at the opponent's ground, so pass them as the geocoding hint
    opponent_hint = "" if is_home else fixture.get("home_team", "")
    pinned_address = find_venue_address(venue, VENUE_ADDRESSES)

    cache_key = (venue, opponent_hint, pinned_address or "")
    if cache_key in _LOCATION_CACHE:
        return _LOCATION_CACHE[cache_key]

    if pinned_address:
        print(f"  Using pinned VENUE_ADDRESSES entry for: {venue}")
    geocoded = geocode_venue(venue, opponent_hint, address=pinned_address)
    if not pinned_address:
        # Short FA name only: the pin is a guess, so ask the admin to check it once
        report_unpinned_venue(venue, fixture, geocoded or {"feature": venue}, bool(geocoded))

    if geocoded:
        print(f"  Geocoded: {venue} -> {geocoded.get('addressLine', 'found')}")
        _LOCATION_CACHE[cache_key] = geocoded
        return geocoded
    print(f"  Geocoding failed, using plain text: {venue}")
    return {"feature": venue}


def calendar_location_text(location: dict, venue: str) -> str:
    """Text for a Calendar event's location, which is free text (no coordinates field).

    The geocoded address makes Google Maps resolve the right ground. A bare postcode
    centroid gets the venue name in front so it still reads as a place.
    """
    postcode = (location.get("postalCode") or "").strip()
    address = (location.get("addressLine") or "").strip()
    if address and not (postcode and address.upper().startswith(postcode.upper())):
        return address
    if location.get("latitude") is not None and postcode:
        return f"{venue}, {postcode}"
    return venue


def calendar_location_or_empty(fixture: dict) -> dict:
    """resolve_venue_location, but a geocoding problem must never block the Calendar event."""
    try:
        return resolve_venue_location(fixture, fixture_is_home(fixture)[1])
    except Exception as e:
        print(f"  Location lookup failed, using the FA venue text: {e}")
        return {}


def report_unpinned_venue(venue: str, fixture: dict, location: dict, geocoded: bool) -> bool:
    """Email EMAIL_TO_ADMIN once per venue whose pin was guessed (not in VENUE_ADDRESSES).

    Never raises: a reporting problem must not stop the Spond event being created.
    Returns True if a report was emailed.
    """
    try:
        key = venue_review_key(venue)
        path = reviewed_venues_path()
        if key in load_reviewed_venues(path):
            return False
        subject, body = build_venue_review_email(
            venue, fixture, location, geocoded, existing=VENUE_ADDRESSES
        )
        if DRY_RUN:
            print(f"  [DRY RUN] Would email admin about venue: {subject}")
            print(body)
            return False
        if not (smtp_configured() and EMAIL_TO_ADMIN.strip()):
            print(f"  Venue needs checking (admin email not configured): {subject}")
            print(body)
            return False
        if not send_email(f"[fixture-sync] {subject}", body, EMAIL_TO_ADMIN):
            return False
        resolved = location.get("addressLine") or "(not located)"
        mark_venue_reviewed(path, key, resolved)
        return True
    except Exception as e:
        print(f"  Venue report failed (continuing): {e}")
        return False


def send_email_notification(fixture: dict) -> bool:
    """Send an email notification about a new fixture."""
    if not smtp_configured() or not EMAIL_TO.strip():
        return False

    home_team = translate_team_name(fixture["home_team"])
    away_team = translate_team_name(fixture["away_team"])
    subject = f"New Fixture: {home_team} vs {away_team}"
    body_parts = [
        f"A new fixture has been added to the calendar:\n",
        f"Date: {fixture['date']}",
        f"Kick-off: {fixture['time']}",
        f"Match: {fixture['home_team']} vs {fixture['away_team']}",
    ]
    if fixture.get("venue"):
        body_parts.append(f"Venue: {fixture['venue']}")
    if fixture.get("competition"):
        body_parts.append(f"Competition: {fixture['competition']}")
    append_detail_url(body_parts, fixture)
    return send_email(subject, "\n".join(body_parts), EMAIL_TO)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


async def create_spond_event(fixture: dict, group_id: str, host_ids: list[str] | None = None) -> bool:
    """Create a Spond event for a fixture."""
    if not all([SPOND_EMAIL, SPOND_PASSWORD, group_id]):
        return False  # Spond not configured
    
    try:
        s = spond.Spond(username=SPOND_EMAIL, password=SPOND_PASSWORD)
        
        # Authenticate first
        await s.login()
        
        # Fetch group to get member IDs
        groups = await s.get_groups()
        group_data = next((g for g in groups if g["id"] == group_id), None)
        if not group_data:
            print(f"  Spond group {group_id} not found")
            await s.clientsession.close()
            return False
        
        # Get all member IDs from the group (as objects with id field)
        member_ids = [{"id": m["id"]} for m in group_data.get("members", [])]
        group_name = group_data.get("name", "")
        
        # Parse fixture datetime
        kickoff_dt = parse_fixture_datetime(fixture["date"], fixture["time"])
        if not kickoff_dt:
            return False
        
        # Start at kick-off time, end 60 mins after kick-off (match duration)
        start_dt = kickoff_dt
        end_dt = kickoff_dt + timedelta(minutes=60)
        
        # Convert local time to UTC for Spond API (timestamps must be UTC with Z suffix)
        local_tz = ZoneInfo(TZ)
        start_dt_local = start_dt.replace(tzinfo=local_tz)
        end_dt_local = end_dt.replace(tzinfo=local_tz)
        start_dt_utc = start_dt_local.astimezone(timezone.utc)
        end_dt_utc = end_dt_local.astimezone(timezone.utc)
        
        # Translate team names for title
        home_team_translated = translate_team_name(fixture["home_team"])
        away_team_translated = translate_team_name(fixture["away_team"])
        ko_time = kickoff_dt.strftime("%H:%M")
        
        kit_colour, is_home = fixture_is_home(fixture)
        match_type = "HOME" if is_home else "AWAY"
        opponent = fixture["away_team"] if is_home else fixture["home_team"]
        
        # Build event heading
        heading = f"{home_team_translated} vs {away_team_translated} - KO {ko_time}"
        
        # Build description
        description_parts = [
            f"Kick-off: {ko_time}",
            f"Home: {fixture['home_team']}",
            f"Away: {fixture['away_team']}",
        ]
        if fixture.get("venue"):
            description_parts.append(f"Venue: {fixture['venue']}")
        if fixture.get("competition"):
            description_parts.append(f"Competition: {fixture['competition']}")
        append_detail_url(description_parts, fixture)
        description = "\n".join(description_parts)
        
        # Geocoded for a clickable map pin (shared with Calendar, cached per venue)
        location = resolve_venue_location(fixture, is_home)

        # Build Spond event payload for a match event
        event_data = {
            "heading": heading,
            "description": description,
            "spondType": "EVENT",
            "startTimestamp": start_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endTimestamp": end_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "meetupPrior": 30,  # Meet 30 minutes before start
            "matchEvent": True,
            "matchInfo": {
                "teamName": group_name,
                "opponentName": opponent,
                "type": match_type,
            },
            "commentsDisabled": False,
            "maxAccepted": 0,
            "rsvpDate": None,
            "location": location,
            "visibility": "INVITEES",
            "participantsHidden": False,
            "autoReminderType": "DISABLED",
            "autoAccept": False,
            "attachments": [],
            "recipients": {
                "group": {"id": group_id, "members": member_ids},
            },
        }
        
        # Our kit only — omit opponentColour; we do not know the other team's kit
        if kit_colour:
            event_data["matchInfo"]["teamColour"] = kit_colour
        
        # Set event hosts/owners if specified (Spond accepts multiple)
        if host_ids:
            event_data["owners"] = [{"id": host_id} for host_id in host_ids]
        
        # Dry run mode - skip actual creation
        if DRY_RUN:
            print(f"  [DRY RUN] Would create Spond event: {heading}")
            print(f"  [DRY RUN] Location: {location}")
            if kit_colour:
                print(f"  [DRY RUN] teamColour: {kit_colour} ({match_type})")
            if host_ids:
                print(f"  [DRY RUN] owners: {', '.join(host_ids)}")
            await s.clientsession.close()
            return True
        
        # POST to create event
        url = f"{s.api_url}sponds/"
        async with s.clientsession.post(url, json=event_data, headers=s.auth_headers) as r:
            if r.ok:
                result = await r.json()
                print(f"  Created Spond event: {heading}")
                if kit_colour:
                    print(f"  teamColour: {kit_colour} ({match_type})")
                await s.clientsession.close()
                return True
            else:
                error = await r.text()
                print(f"  Failed to create Spond event: {r.status} - {error}")
                await s.clientsession.close()
                return False
                
    except Exception as e:
        print(f"  Failed to create Spond event: {e}")
        return False


def create_spond_event_sync(fixture: dict, group_id: str, host_ids: list[str] | None = None) -> bool:
    """Synchronous wrapper for create_spond_event."""
    if not all([SPOND_EMAIL, SPOND_PASSWORD, group_id]):
        return False
    return asyncio.run(create_spond_event(fixture, group_id, host_ids))


def get_fixture_date_key(fixture: dict, url_index: int) -> str:
    """Generate a key for a fixture based on its date and source URL (one fixture per date per team)."""
    return f"{url_index}_{fixture['date']}"


def get_fixture_hash(fixture: dict) -> str:
    """Generate a hash of fixture details for change detection."""
    key = f"{fixture['date']}_{fixture['time']}_{fixture['home_team']}_{fixture['away_team']}_{fixture.get('venue', '')}"
    return hashlib.md5(key.encode()).hexdigest()


def load_synced_fixtures() -> dict:
    """Load synced fixtures data from disk.
    
    Returns dict mapping date -> {event_id, fixture_hash}
    """
    synced_file = Path(DATA_DIR) / "synced_fixtures.json"
    if synced_file.exists():
        try:
            with open(synced_file, "r") as f:
                content = f.read().strip()
                if not content:
                    return {}
                data = json.loads(content)
                # Handle migration from old format (list of IDs)
                if isinstance(data, list):
                    return {}
                return data
        except json.JSONDecodeError:
            print("  Warning: Could not parse synced_fixtures.json, starting fresh")
            return {}
    return {}


def save_synced_fixtures(synced: dict) -> None:
    """Save synced fixtures data to disk."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    synced_file = Path(DATA_DIR) / "synced_fixtures.json"
    with open(synced_file, "w") as f:
        json.dump(synced, f, indent=2)


def fetch_fixtures(url: str) -> tuple[list[dict] | None, str | None]:
    """Fetch fixtures from an FA Full-Time fixtures page.

    Returns (fixtures, None) on success, or (None, error) if the page could not
    be loaded or the fixtures table is missing (as opposed to a valid empty list).
    """
    print(f"Polling FA Full-Time: {url[:80]}...")

    try:
        # Use Playwright (real browser) to bypass bot protection
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # Navigate to the fixtures page
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for the fixtures table to load
            page.wait_for_selector(".fixtures-table", timeout=30000)
            
            # Get the page content
            html = page.content()
            
            # Parse fixtures from the page
            soup = BeautifulSoup(html, "html.parser")

            fixtures = []
            fixtures_table = soup.find("div", class_="fixtures-table")
            if not fixtures_table:
                print("  No fixtures table found on page")
                browser.close()
                return None, "No fixtures table found on page (FA layout change or Cloudflare?)"

            table = fixtures_table.find("table")
            if not table:
                print("  No table found in fixtures-table div")
                browser.close()
                return None, "No table found in fixtures-table div"

            tbody = table.find("tbody")
            if not tbody:
                print("  No tbody found in table")
                browser.close()
                return None, "No tbody found in fixtures table"

            for row in tbody.find_all("tr"):
                try:
                    # Date/Time cell - contains two spans
                    date_cell = row.find("td", class_="left")
                    if not date_cell:
                        continue

                    date_link = date_cell.find("a")
                    if not date_link:
                        continue

                    # Extract fixture detail URL
                    fixture_url = ""
                    href = date_link.get("href", "")
                    if href:
                        if href.startswith("http"):
                            fixture_url = href
                        else:
                            fixture_url = f"https://fulltime.thefa.com{href}"

                    spans = date_link.find_all("span")
                    if len(spans) < 2:
                        continue

                    date_str = spans[0].get_text(strip=True)
                    time_str = spans[1].get_text(strip=True)

                    # Home team
                    home_cell = row.find("td", class_="home-team")
                    if not home_cell:
                        continue
                    home_team = home_cell.get_text(strip=True)

                    # Away team
                    away_cell = row.find("td", class_="road-team")
                    if not away_cell:
                        continue
                    away_team = away_cell.get_text(strip=True)

                    # Find venue - it's the cell after road-team
                    venue = ""
                    competition = ""
                    
                    all_cells = row.find_all("td")
                    road_team_found = False
                    venue_found = False
                    
                    for cell in all_cells:
                        classes = cell.get("class", [])
                        
                        if "road-team" in classes:
                            road_team_found = True
                            continue
                        
                        if road_team_found and not venue_found:
                            text = cell.get_text(strip=True)
                            if text:
                                venue = text
                                venue_found = True
                            continue
                        
                        if venue_found and not competition:
                            text = cell.get_text(strip=True)
                            if text:
                                competition = text
                                break

                    fixture = {
                        "date": date_str,
                        "time": time_str if time_str else "TBC",
                        "home_team": home_team,
                        "away_team": away_team,
                        "venue": venue,
                        "competition": competition,
                        "detail_url": fixture_url,
                        "venue_address": None,
                    }
                    fixtures.append(fixture)
                    print(f"  Found: {date_str} {time_str} - {home_team} vs {away_team}")

                except Exception as e:
                    print(f"  Error parsing row: {e}")
                    continue

            # Detail pages are not used (Cloudflare blocks them, confirmed from the
            # server). The list page has no address, so use VENUE_ADDRESSES to pin venues.
            browser.close()
            return fixtures, None

    except Exception as e:
        print(f"  Error fetching fixtures: {e}")
        return None, str(e)


def parse_fixture_datetime(date_str: str, time_str: str) -> datetime | None:
    """Parse date and time strings into a datetime object."""
    try:
        # Handle TBC times
        if time_str.upper() == "TBC":
            time_str = "10:00"

        # Parse DD/MM/YY format
        full_str = f"{date_str} {time_str}"
        return datetime.strptime(full_str, "%d/%m/%y %H:%M")
    except ValueError as e:
        print(f"  Could not parse datetime '{date_str} {time_str}': {e}")
        return None


def get_calendar_service():
    """Create and return a Google Calendar API service. Returns None if not configured."""
    # Check if any calendar IDs are configured
    has_calendar_ids = any(cid.strip() for cid in CALENDAR_IDS)
    if not has_calendar_ids:
        return None  # Calendar not configured, skip silently
    
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        print(f"WARNING: Service account file not found: {SERVICE_ACCOUNT_FILE}")
        print("  Google Calendar sync disabled.")
        return None

    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        print(f"ERROR: Failed to create calendar service: {e}")
        return None


def translate_team_name(full_name: str) -> str:
    """Translate a full team name to its short version if configured."""
    short_name = TEAM_NAMES.get(full_name, full_name)
    if short_name != full_name:
        print(f"  Translated: '{full_name}' -> '{short_name}'")
    else:
        print(f"  No translation for: '{full_name}'")
    return short_name


def build_calendar_event(fixture: dict, location: dict | None = None) -> dict | None:
    """Build a Google Calendar event dict from a fixture."""
    kickoff_dt = parse_fixture_datetime(fixture["date"], fixture["time"])
    if not kickoff_dt:
        return None

    # Start 30 mins before kick-off (warmup time)
    start_dt = kickoff_dt - timedelta(minutes=30)
    
    # Total duration 90 mins (30 warmup + 60 playing)
    end_dt = start_dt + timedelta(minutes=90)

    # Translate team names to short versions
    home_team = translate_team_name(fixture["home_team"])
    away_team = translate_team_name(fixture["away_team"])

    # Format kick-off time for title
    ko_time = kickoff_dt.strftime("%H:%M")
    
    # Build event summary with kick-off time
    summary = f"{home_team} vs {away_team} - KO {ko_time}"

    # Description keeps full names for reference
    description_parts = [
        f"Kick-off: {ko_time}",
        f"Home: {fixture['home_team']}",
        f"Away: {fixture['away_team']}",
    ]
    if fixture.get("venue"):
        description_parts.append(f"Venue: {fixture['venue']}")
        if location and location.get("latitude") is not None:
            description_parts.append(
                f"Map: https://www.google.com/maps?q={location['latitude']},{location['longitude']}"
            )
    if fixture.get("competition"):
        description_parts.append(f"Competition: {fixture['competition']}")
    append_detail_url(description_parts, fixture)

    description = "\n".join(description_parts)

    event = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": TZ,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": TZ,
        },
        # Disable default reminders/notifications
        "reminders": {
            "useDefault": False,
            "overrides": [],
        },
    }

    # Add venue as location if available. Calendar location is free text, so a geocoded
    # address goes there and the exact coordinates go in the description as a Map link.
    if fixture.get("venue"):
        venue = fixture_venue_name(fixture["venue"])  # no time suffix or trailing space
        event["location"] = calendar_location_text(location or {}, venue)

    return event


def create_calendar_event(
    service, calendar_id: str, fixture: dict, location: dict | None = None
) -> str | None:
    """Create a Google Calendar event for a fixture. Returns event ID or None."""
    event = build_calendar_event(fixture, location)
    if not event:
        return None

    if DRY_RUN:
        print(f"  [DRY RUN] Would create Calendar event: {event['summary']} on {fixture['date']}")
        return "dry-run-event-id"  # truthy so the sync does not report a failure

    try:
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"  Created event: {event['summary']} on {fixture['date']}")
        return created_event["id"]
    except HttpError as e:
        print(f"  Failed to create event: {e}")
        return None


def update_calendar_event(
    service, calendar_id: str, event_id: str, fixture: dict, location: dict | None = None
) -> bool:
    """Update an existing Google Calendar event. Returns True on success."""
    event = build_calendar_event(fixture, location)
    if not event:
        return False

    if DRY_RUN:
        print(f"  [DRY RUN] Would update Calendar event: {event['summary']} on {fixture['date']}")
        return True

    try:
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        print(f"  Updated event: {event['summary']} on {fixture['date']}")
        return True
    except HttpError as e:
        print(f"  Failed to update event: {e}")
        return False


def sync_to_calendar(all_fixtures: list[tuple[int, dict]]) -> list[str]:
    """Sync fixtures to Google Calendar and Spond. Returns error messages."""
    errors: list[str] = []
    if not all_fixtures:
        print("No fixtures found to sync.")
        return errors

    service = get_calendar_service()

    synced = load_synced_fixtures()
    new_count = 0
    updated_count = 0

    for url_index, fixture in all_fixtures:
        calendar_id = ""
        if url_index < len(CALENDAR_IDS):
            calendar_id = CALENDAR_IDS[url_index].strip()

        date_key = get_fixture_date_key(fixture, url_index)
        fixture_hash = get_fixture_hash(fixture)
        label = f"{fixture['home_team']} vs {fixture['away_team']} on {fixture['date']}"

        if date_key in synced:
            existing = synced[date_key]
            
            if existing["hash"] == fixture_hash:
                print(f"  Unchanged: {label}")
                continue
            
            print(f"  Fixture changed for {fixture['date']} - updating...")
            if service and calendar_id and existing.get("event_id"):
                location = calendar_location_or_empty(fixture)
                if not update_calendar_event(
                    service, calendar_id, existing["event_id"], fixture, location
                ):
                    errors.append(f"Calendar update failed: {label}")
            synced[date_key] = {"event_id": existing.get("event_id", ""), "hash": fixture_hash}
            updated_count += 1
        else:
            event_id = ""
            if service and calendar_id:
                location = calendar_location_or_empty(fixture)
                event_id = create_calendar_event(service, calendar_id, fixture, location) or ""
                if not event_id:
                    errors.append(f"Calendar create failed: {label}")
            
            synced[date_key] = {"event_id": event_id, "hash": fixture_hash}
            new_count += 1
            
            send_email_notification(fixture)
            
            if url_index < len(SPOND_GROUP_IDS):
                spond_group_id = SPOND_GROUP_IDS[url_index].strip()
                if spond_group_id:
                    spond_host_ids: list[str] = []
                    if url_index < len(SPOND_HOST_SLOTS):
                        spond_host_ids = SPOND_HOST_SLOTS[url_index]
                    if not create_spond_event_sync(fixture, spond_group_id, spond_host_ids):
                        errors.append(f"Spond create failed: {label}")

    if not DRY_RUN:
        save_synced_fixtures(synced)
    print(f"Sync complete. Added {new_count} new, updated {updated_count} existing.")
    return errors


def last_run_path() -> Path:
    return Path(DATA_DIR) / "last_run"


def mark_job_ran() -> None:
    """Touch last_run so Docker HEALTHCHECK knows the poll loop is alive."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    last_run_path().write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def ping_healthchecks() -> None:
    """Tell Healthchecks.io (or similar) that this process still completed a poll."""
    if not HEALTHCHECKS_PING_URL:
        return
    try:
        req = urllib.request.Request(HEALTHCHECKS_PING_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10):
            pass
        print("  Healthchecks ping sent")
    except Exception as e:
        print(f"  Healthchecks ping failed: {e}")


def job():
    """Main job that fetches all fixtures and syncs them."""
    global _last_poll_had_errors
    print(f"\n{'='*60}")
    print(f"Running fixture sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    errors: list[str] = []
    all_fixtures: list[tuple[int, dict]] = []

    try:
        for url_index, url in enumerate(FIXTURE_URLS):
            url = url.strip()
            if not url:
                continue
            fixtures, fetch_error = fetch_fixtures(url)
            if fetch_error:
                errors.append(f"Fetch failed (URL {url_index + 1}): {fetch_error}\n{url}")
                continue
            for fixture in fixtures or []:
                all_fixtures.append((url_index, fixture))

        print(f"\nTotal fixtures found: {len(all_fixtures)}")
        errors.extend(sync_to_calendar(all_fixtures))
    except Exception:
        errors.append(traceback.format_exc())

    if errors:
        send_alert("Fixture sync failed", "\n\n".join(errors))
        _last_poll_had_errors = True
    else:
        if _last_poll_had_errors:
            send_alert(
                "Fixture sync recovered",
                "The latest poll completed without errors.",
            )
        _last_poll_had_errors = False

    mark_job_ran()
    ping_healthchecks()


def validate_config() -> bool:
    """Validate that required configuration is present."""
    errors = []
    warnings = []

    if not FIXTURE_URLS or FIXTURE_URLS == [""]:
        errors.append("FIXTURE_URLS environment variable is not set")

    # Check calendar configuration (optional).
    # Slots line up with FIXTURE_URLS. A blank slot skips Calendar for that URL.
    has_calendar = any(c.strip() for c in CALENDAR_IDS)
    if has_calendar:
        if len(FIXTURE_URLS) != len(CALENDAR_IDS):
            errors.append(
                f"Mismatch: {len(FIXTURE_URLS)} fixture URL slots but {len(CALENDAR_IDS)} calendar slots. "
                "Use one entry per URL in the same order; a blank entry skips Calendar for that URL "
                "(for example CALENDAR_IDS=,second@group.calendar.google.com)."
            )
        if not Path(SERVICE_ACCOUNT_FILE).exists():
            errors.append(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")
    else:
        warnings.append("Google Calendar sync disabled (no CALENDAR_IDS configured)")

    # Check Spond configuration (optional)
    has_spond = any(g.strip() for g in SPOND_GROUP_IDS)
    if not has_spond:
        warnings.append("Spond sync disabled (no SPOND_GROUP_IDS configured)")

    # Must have at least one output configured
    if not has_calendar and not has_spond:
        errors.append("No outputs configured - set CALENDAR_IDS and/or SPOND_GROUP_IDS")

    if warnings:
        for warning in warnings:
            print(f"  Note: {warning}")

    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        return False

    return True


if __name__ == "__main__":
    print("Football Fixture Calendar Sync")
    print("=" * 40)
    url_count = len([u for u in FIXTURE_URLS if u.strip()])
    cal_count = len([c for c in CALENDAR_IDS if c.strip()])
    spond_count = len([g for g in SPOND_GROUP_IDS if g.strip()])
    print(f"Fixture URLs: {url_count} configured")
    print(f"Calendar IDs: {cal_count} configured")
    print(f"Spond Groups: {spond_count} configured" if SPOND_EMAIL else "Spond: not configured")
    if DRY_RUN:
        print("DRY RUN MODE: No events will be created")
    print(f"Poll Interval: every {POLL_INTERVAL_HOURS} hour(s)")
    print(f"Timezone: {TZ}")
    print(f"Data Directory: {DATA_DIR}")
    if TEAM_NAMES:
        print(f"Team translations: {len(TEAM_NAMES)} configured")
    if TEAM_COLOURS:
        print(f"Team colours: {len(TEAM_COLOURS)} configured")
    if EMAIL_TO.strip() and smtp_configured():
        print("Fixture emails: configured")
    else:
        print("Fixture emails: not configured")
    if EMAIL_TO_ADMIN.strip() and smtp_configured():
        print("Alerts: admin email on poll/sync failure (and recovery)")
    else:
        print("Alerts: admin email not configured (failures logged only)")
    if HEALTHCHECKS_PING_URL:
        print("Healthchecks.io: ping URL configured")
    else:
        print("Healthchecks.io: not configured (no crash/dead-host email)")
    google_api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if google_api_key:
        print(f"Google Maps API: configured (accurate geocoding)")
    else:
        print("Google Maps API: not configured (using postcode fallback)")
    print("=" * 40)

    if not validate_config():
        print("\nPlease fix the configuration errors above and restart.")
        exit(1)

    print("\nStarting FA Full-Time polling service...")

    # Run once immediately on startup
    job()

    # Schedule the job to run at the configured interval
    schedule.every(POLL_INTERVAL_HOURS).hours.do(job)
    print(f"\nScheduled to poll every {POLL_INTERVAL_HOURS} hour(s)")

    # Keep the script running
    while True:
        schedule.run_pending()
        time.sleep(60)
