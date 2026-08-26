# Football Fixture Calendar Sync

Automatically sync youth football fixtures from FA Full-Time to Google Calendar.

This Docker container polls the FA Full-Time website for fixture updates and creates Google Calendar events for new matches. It runs on a schedule (default: daily at 08:00) and tracks which fixtures have already been synced to avoid duplicates.

## Features

- Scrapes fixtures from FA Full-Time team pages
- Creates Google Calendar events with match details
- Supports multiple teams/fixture URLs
- Avoids duplicate calendar entries
- Persists state across container restarts
- Configurable poll schedule and timezone

## Prerequisites

1. **Docker** (and optionally Docker Compose / Dockge)
2. **Google Cloud Service Account** with Calendar API access
3. **FA Full-Time fixture URL(s)** for your team(s)

## Setup

### 1. Get Your FA Full-Time Fixture URL

1. Go to [FA Full-Time](https://fulltime.thefa.com/)
2. Search for your league and navigate to your team
3. Go to the **Fixtures** page
4. Use the filters to select your team
5. Copy the full URL from your browser's address bar

Example URL:
```
https://fulltime.thefa.com/fixtures.html?selectedSeason=357645222&selectedFixtureGroupAgeGroup=12&selectedClub=950375314&selectedTeam=42648016&selectedRelatedFixtureOption=2&selectedDateCode=all
```

### 2. Create a Google Cloud Service Account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Google Calendar API**:
   - Go to APIs & Services → Library
   - Search for "Google Calendar API"
   - Click Enable
4. Create a Service Account:
   - Go to APIs & Services → Credentials
   - Click "Create Credentials" → "Service Account"
   - Give it a name (e.g., "fixture-calendar-sync")
   - Click Create and Continue
   - Skip the optional steps and click Done
5. Create a key for the service account:
   - Click on the service account you just created
   - Go to the "Keys" tab
   - Click "Add Key" → "Create new key"
   - Select JSON and click Create
   - Save the downloaded file as `service_account.json`

### 3. Share Your Calendar with the Service Account

1. Open [Google Calendar](https://calendar.google.com/)
2. Find your calendar in the left sidebar
3. Click the three dots → "Settings and sharing"
4. Scroll to "Share with specific people or groups"
5. Click "Add people and groups"
6. Enter the service account email (found in your `service_account.json` as `client_email`)
7. Set permission to "Make changes to events"
8. Click Send

### 4. Get Your Calendar ID

1. In Google Calendar settings for your calendar
2. Scroll to "Integrate calendar"
3. Copy the **Calendar ID**
   - For your primary calendar, this is your email address
   - For other calendars, it looks like: `abc123@group.calendar.google.com`

### 5. Configure the Container

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with your values:
   ```bash
   # Single team
   FIXTURE_URLS=https://fulltime.thefa.com/fixtures.html?selectedSeason=XXX&selectedTeam=YYY

   # Multiple teams (comma-separated)
   FIXTURE_URLS=https://fulltime.thefa.com/fixtures.html?team1,https://fulltime.thefa.com/fixtures.html?team2

   CALENDAR_ID=your_calendar_id@group.calendar.google.com
   POLL_SCHEDULE=08:00
   TZ=Europe/London
   ```

3. Place your `service_account.json` in the project directory

### 6. Deploy

#### Using Docker Compose

```bash
docker compose up -d
```

#### Using Dockge

1. Create a new stack in Dockge
2. Paste the contents of `compose.yaml`
3. Set the environment variables
4. Ensure you have:
   - `service_account.json` in the stack directory
   - A `data/` directory for persistence (will be created automatically)
5. Deploy the stack

## Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `FIXTURE_URLS` | Comma-separated FA Full-Time fixture URLs | (required) |
| `CALENDAR_ID` | Google Calendar ID to add events to | (required) |
| `SERVICE_ACCOUNT_FILE` | Path to service account JSON | `/app/service_account.json` |
| `POLL_SCHEDULE` | Time to run daily poll (24h format) | `08:00` |
| `TZ` | Timezone for calendar events | `Europe/London` |
| `DATA_DIR` | Directory for persistence data | `/app/data` |

## How It Works

1. **Polling**: The container fetches fixtures from the configured FA Full-Time URLs at the scheduled time
2. **Parsing**: It extracts match details (date, time, teams, venue) from the page HTML
3. **Deduplication**: Each fixture is assigned a unique ID based on its details. Already-synced fixtures are skipped
4. **Calendar Creation**: New fixtures are added to Google Calendar with:
   - Summary: `⚽ Home Team vs Away Team`
   - Location: Venue (if available)
   - Duration: 2 hours
   - Description: Full match details

## Logs

View container logs to see sync activity:

```bash
docker compose logs -f fixture-sync
```

Example output:
```
Football Fixture Calendar Sync
========================================
Fixture URLs: 1 configured
Calendar ID: abc123@group.cale...
Poll Schedule: 08:00
Timezone: Europe/London
========================================

Running fixture sync at 2024-01-15 08:00:00
============================================================
Polling FA Full-Time: https://fulltime.thefa.com/fixtures.html?...
  Found: 21/01/24 11:00 - Home FC vs Away United
  Found: 28/01/24 10:30 - Home FC vs Another Team

Total fixtures found: 2
  Created event: ⚽ Home FC vs Away United on 21/01/24
  Created event: ⚽ Home FC vs Another Team on 28/01/24
Sync complete. Added 2 new events.
```

## Troubleshooting

### "Service account file not found"
Ensure `service_account.json` is in the correct location and mounted properly in the container.

### "Failed to create event: 403"
The service account doesn't have write access to the calendar. Check that you've shared the calendar with the service account email.

### "No fixtures table found"
The FA Full-Time page structure may have changed, or the URL might be incorrect. Verify your fixture URL loads correctly in a browser.

### Fixtures not appearing
- Check the container logs for errors
- Verify your fixture URL shows fixtures when viewed in a browser
- Ensure the calendar ID is correct

## Data Persistence

Synced fixture IDs are stored in `data/synced_fixtures.json`. This file tracks which fixtures have already been added to the calendar, preventing duplicates.

If you need to re-sync all fixtures:
1. Stop the container
2. Delete `data/synced_fixtures.json`
3. Restart the container

## License

MIT
