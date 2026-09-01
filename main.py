import os
import time
import json
import hashlib
import smtplib
import asyncio
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

from geocoding import geocode_venue

# Configuration from environment variables
FIXTURE_URLS = os.environ.get("FIXTURE_URLS", "").split(",")
CALENDAR_IDS = os.environ.get("CALENDAR_IDS", "").split(",")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "/app/service_account.json")
POLL_INTERVAL_HOURS = int(os.environ.get("POLL_INTERVAL_HOURS", "12"))
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
TZ = os.environ.get("TZ", "Europe/London")

# Team name translations (format: "Full Name:Short Name,Full Name 2:Short Name 2")
# Example: "Boldmere St Michaels Juniors U11 2015 JH:Mikes,Other Team U12:Owls"
TEAM_NAMES_RAW = os.environ.get("TEAM_NAMES", "")


# Email notification settings (optional)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")  # Comma-separated for multiple recipients

# Spond integration (optional)
SPOND_EMAIL = os.environ.get("SPOND_EMAIL", "")
SPOND_PASSWORD = os.environ.get("SPOND_PASSWORD", "")
SPOND_GROUP_IDS = os.environ.get("SPOND_GROUP_IDS", "").split(",")  # One per fixture URL
SPOND_HOST_IDS = os.environ.get("SPOND_HOST_IDS", "").split(",")  # One per fixture URL (optional)

# Dry run mode - test without creating events
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")


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


def send_email_notification(fixture: dict) -> bool:
    """Send an email notification about a new fixture."""
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        return False  # Email not configured
    
    try:
        # Build email content
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
        
        body = "\n".join(body_parts)
        
        # Create message
        msg = MIMEMultipart()
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        
        # Send email
        recipients = [email.strip() for email in EMAIL_TO.split(",")]
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        
        print(f"  Email notification sent to {EMAIL_TO}")
        return True
        
    except Exception as e:
        print(f"  Failed to send email notification: {e}")
        return False

SCOPES = ["https://www.googleapis.com/auth/calendar"]


async def create_spond_event(fixture: dict, group_id: str, host_id: str = "") -> bool:
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
        
        # Determine if home or away (if our team name was translated, that's our team)
        is_home = home_team_translated != fixture["home_team"]
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
        description = "\n".join(description_parts)
        
        # Build location - try geocoding for clickable map links
        location = {}
        venue_raw = fixture.get("venue", "")
        if venue_raw:
            venue = venue_raw
            # Remove time suffix if present (e.g., "VENUE NAME - 11:00AM" -> "VENUE NAME")
            if " - " in venue:
                venue = venue.split(" - ")[0]
            
            # For away games, pass opponent team as geocoding hint
            opponent_hint = ""
            if not is_home:
                opponent_hint = fixture.get("home_team", "")  # Away game = at opponent's ground
            
            geocoded = geocode_venue(venue, opponent_hint)
            
            if geocoded:
                location = geocoded
                print(f"  Geocoded: {venue} -> {geocoded.get('addressLine', 'found')}")
            else:
                # Fall back to plain text
                print(f"  Geocoding failed, using plain text: {venue}")
                location = {"feature": venue}
        
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
        
        # Set event host/owner if specified
        if host_id:
            event_data["owners"] = [{"id": host_id}]
        
        # Dry run mode - skip actual creation
        if DRY_RUN:
            print(f"  [DRY RUN] Would create Spond event: {heading}")
            print(f"  [DRY RUN] Location: {location}")
            await s.clientsession.close()
            return True
        
        # POST to create event
        url = f"{s.api_url}sponds/"
        async with s.clientsession.post(url, json=event_data, headers=s.auth_headers) as r:
            if r.ok:
                result = await r.json()
                print(f"  Created Spond event: {heading}")
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


def create_spond_event_sync(fixture: dict, group_id: str, host_id: str = "") -> bool:
    """Synchronous wrapper for create_spond_event."""
    if not all([SPOND_EMAIL, SPOND_PASSWORD, group_id]):
        return False
    return asyncio.run(create_spond_event(fixture, group_id, host_id))


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


def fetch_fixtures(url: str) -> list[dict]:
    """Fetch fixtures from an FA Full-Time fixtures page."""
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
                return []

            table = fixtures_table.find("table")
            if not table:
                print("  No table found in fixtures-table div")
                return []

            tbody = table.find("tbody")
            if not tbody:
                print("  No tbody found in table")
                return []

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

            # Note: Detail page fetching disabled due to Cloudflare blocking
            # Use VENUE_ADDRESSES env var for manual address mapping instead
            
            browser.close()
            return fixtures

    except Exception as e:
        print(f"  Error fetching fixtures: {e}")
        return []


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


def build_calendar_event(fixture: dict) -> dict | None:
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
    if fixture.get("competition"):
        description_parts.append(f"Competition: {fixture['competition']}")

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

    # Add venue as location if available
    if fixture.get("venue"):
        # Extract just the venue name (remove time suffix if present)
        venue = fixture["venue"]
        if " - " in venue:
            venue = venue.split(" - ")[0]
        event["location"] = venue

    return event


def create_calendar_event(service, calendar_id: str, fixture: dict) -> str | None:
    """Create a Google Calendar event for a fixture. Returns event ID or None."""
    event = build_calendar_event(fixture)
    if not event:
        return None

    try:
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"  Created event: {event['summary']} on {fixture['date']}")
        return created_event["id"]
    except HttpError as e:
        print(f"  Failed to create event: {e}")
        return None


def update_calendar_event(service, calendar_id: str, event_id: str, fixture: dict) -> bool:
    """Update an existing Google Calendar event. Returns True on success."""
    event = build_calendar_event(fixture)
    if not event:
        return False

    try:
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        print(f"  Updated event: {event['summary']} on {fixture['date']}")
        return True
    except HttpError as e:
        print(f"  Failed to update event: {e}")
        return False


def sync_to_calendar(all_fixtures: list[tuple[int, dict]]) -> None:
    """Sync fixtures to Google Calendar, creating or updating as needed.
    
    Args:
        all_fixtures: List of (url_index, fixture) tuples
    """
    if not all_fixtures:
        print("No fixtures found to sync.")
        return

    # Get calendar service (optional - may be None if not configured)
    service = get_calendar_service()

    synced = load_synced_fixtures()
    new_count = 0
    updated_count = 0

    for url_index, fixture in all_fixtures:
        # Get the calendar ID for this URL index (optional)
        calendar_id = ""
        if url_index < len(CALENDAR_IDS):
            calendar_id = CALENDAR_IDS[url_index].strip()

        date_key = get_fixture_date_key(fixture, url_index)
        fixture_hash = get_fixture_hash(fixture)

        if date_key in synced:
            # We have a fixture for this date already
            existing = synced[date_key]
            
            if existing["hash"] == fixture_hash:
                # No changes
                print(f"  Unchanged: {fixture['home_team']} vs {fixture['away_team']} on {fixture['date']}")
                continue
            
            # Details changed - update the calendar event if configured
            print(f"  Fixture changed for {fixture['date']} - updating...")
            if service and calendar_id and existing.get("event_id"):
                update_calendar_event(service, calendar_id, existing["event_id"], fixture)
            synced[date_key] = {"event_id": existing.get("event_id", ""), "hash": fixture_hash}
            updated_count += 1
        else:
            # New fixture
            event_id = ""
            if service and calendar_id:
                event_id = create_calendar_event(service, calendar_id, fixture) or ""
            
            synced[date_key] = {"event_id": event_id, "hash": fixture_hash}
            new_count += 1
            
            # Send email notification for new fixtures
            send_email_notification(fixture)
            
            # Create Spond event if configured
            if url_index < len(SPOND_GROUP_IDS):
                spond_group_id = SPOND_GROUP_IDS[url_index].strip()
                if spond_group_id:
                    spond_host_id = ""
                    if url_index < len(SPOND_HOST_IDS):
                        spond_host_id = SPOND_HOST_IDS[url_index].strip()
                    create_spond_event_sync(fixture, spond_group_id, spond_host_id)

    if not DRY_RUN:
        save_synced_fixtures(synced)
    print(f"Sync complete. Added {new_count} new, updated {updated_count} existing.")


def job():
    """Main job that fetches all fixtures and syncs them."""
    print(f"\n{'='*60}")
    print(f"Running fixture sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    all_fixtures = []  # List of (url_index, fixture) tuples

    for url_index, url in enumerate(FIXTURE_URLS):
        url = url.strip()
        if not url:
            continue
        fixtures = fetch_fixtures(url)
        # Tag each fixture with its source URL index
        for fixture in fixtures:
            all_fixtures.append((url_index, fixture))

    print(f"\nTotal fixtures found: {len(all_fixtures)}")
    sync_to_calendar(all_fixtures)


def validate_config() -> bool:
    """Validate that required configuration is present."""
    errors = []
    warnings = []

    if not FIXTURE_URLS or FIXTURE_URLS == [""]:
        errors.append("FIXTURE_URLS environment variable is not set")

    url_count = len([u for u in FIXTURE_URLS if u.strip()])
    
    # Check calendar configuration (optional)
    has_calendar = any(c.strip() for c in CALENDAR_IDS)
    if has_calendar:
        cal_count = len([c for c in CALENDAR_IDS if c.strip()])
        if url_count != cal_count:
            errors.append(f"Mismatch: {url_count} fixture URLs but {cal_count} calendar IDs (must be equal)")
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
