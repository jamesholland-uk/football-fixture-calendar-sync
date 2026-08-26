import os
import time
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
import schedule
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuration from environment variables
FIXTURE_URLS = os.environ.get("FIXTURE_URLS", "").split(",")
CALENDAR_ID = os.environ.get("CALENDAR_ID", "")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "/app/service_account.json")
POLL_SCHEDULE = os.environ.get("POLL_SCHEDULE", "08:00")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
TZ = os.environ.get("TZ", "Europe/London")

# Team name translations (format: "Full Name:Short Name,Full Name 2:Short Name 2")
# Example: "Boldmere St Michaels Juniors U11 2015 JH:Mikes,Other Team U12:Owls"
TEAM_NAMES_RAW = os.environ.get("TEAM_NAMES", "")


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

SCOPES = ["https://www.googleapis.com/auth/calendar"]


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
            browser.close()
        
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
                # The structure is: type | date | home | logo | vs | logo | away | venue | competition | status
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
                        # First cell after road-team is venue
                        text = cell.get_text(strip=True)
                        if text:
                            venue = text
                            venue_found = True
                        continue
                    
                    if venue_found and not competition:
                        # Next cell is competition
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
                }
                fixtures.append(fixture)
                print(f"  Found: {date_str} {time_str} - {home_team} vs {away_team}")

            except Exception as e:
                print(f"  Error parsing row: {e}")
                continue

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
    """Create and return a Google Calendar API service."""
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        print(f"ERROR: Service account file not found: {SERVICE_ACCOUNT_FILE}")
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
    start_dt = parse_fixture_datetime(fixture["date"], fixture["time"])
    if not start_dt:
        return None

    # Assume matches last ~2 hours
    end_dt = start_dt + timedelta(hours=2)

    # Translate team names to short versions
    home_team = translate_team_name(fixture["home_team"])
    away_team = translate_team_name(fixture["away_team"])

    # Build event summary (no emoji)
    summary = f"{home_team} vs {away_team}"

    # Description keeps full names for reference
    description_parts = [
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


def create_calendar_event(service, fixture: dict) -> str | None:
    """Create a Google Calendar event for a fixture. Returns event ID or None."""
    event = build_calendar_event(fixture)
    if not event:
        return None

    try:
        created_event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"  Created event: {event['summary']} on {fixture['date']}")
        return created_event["id"]
    except HttpError as e:
        print(f"  Failed to create event: {e}")
        return None


def update_calendar_event(service, event_id: str, fixture: dict) -> bool:
    """Update an existing Google Calendar event. Returns True on success."""
    event = build_calendar_event(fixture)
    if not event:
        return False

    try:
        service.events().update(calendarId=CALENDAR_ID, eventId=event_id, body=event).execute()
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

    if not CALENDAR_ID:
        print("ERROR: CALENDAR_ID environment variable not set")
        return

    service = get_calendar_service()
    if not service:
        return

    synced = load_synced_fixtures()
    new_count = 0
    updated_count = 0

    for url_index, fixture in all_fixtures:
        date_key = get_fixture_date_key(fixture, url_index)
        fixture_hash = get_fixture_hash(fixture)

        if date_key in synced:
            # We have a fixture for this date already
            existing = synced[date_key]
            
            if existing["hash"] == fixture_hash:
                # No changes
                print(f"  Unchanged: {fixture['home_team']} vs {fixture['away_team']} on {fixture['date']}")
                continue
            
            # Details changed - update the calendar event
            print(f"  Fixture changed for {fixture['date']} - updating calendar...")
            if update_calendar_event(service, existing["event_id"], fixture):
                synced[date_key] = {"event_id": existing["event_id"], "hash": fixture_hash}
                updated_count += 1
        else:
            # New fixture
            event_id = create_calendar_event(service, fixture)
            if event_id:
                synced[date_key] = {"event_id": event_id, "hash": fixture_hash}
                new_count += 1

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

    if not FIXTURE_URLS or FIXTURE_URLS == [""]:
        errors.append("FIXTURE_URLS environment variable is not set")

    if not CALENDAR_ID:
        errors.append("CALENDAR_ID environment variable is not set")

    if not Path(SERVICE_ACCOUNT_FILE).exists():
        errors.append(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")

    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        return False

    return True


if __name__ == "__main__":
    print("Football Fixture Calendar Sync")
    print("=" * 40)
    print(f"Fixture URLs: {len(FIXTURE_URLS)} configured")
    print(f"Calendar ID: {CALENDAR_ID[:20]}..." if CALENDAR_ID else "Calendar ID: NOT SET")
    print(f"Poll Schedule: {POLL_SCHEDULE}")
    print(f"Timezone: {TZ}")
    print(f"Data Directory: {DATA_DIR}")
    print("=" * 40)

    if not validate_config():
        print("\nPlease fix the configuration errors above and restart.")
        exit(1)

    print("\nStarting FA Full-Time polling service...")

    # Run once immediately on startup
    job()

    # Schedule the job to run at the configured time
    schedule.every().day.at(POLL_SCHEDULE).do(job)
    print(f"\nScheduled daily poll at {POLL_SCHEDULE}")

    # Keep the script running
    while True:
        schedule.run_pending()
        time.sleep(60)
