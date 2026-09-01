# Football Fixture Calendar Sync

Poll FA Full-Time for youth football fixtures and create **Google Calendar** and/or **Spond** events when new matches appear.

There is no official FA API. The container uses a headless browser to load the fixtures page, then syncs new or changed fixtures on a repeating interval.

## Features

- Scrapes FA Full-Time fixture lists (Playwright, to get past Cloudflare)
- Optional Google Calendar sync (one calendar per fixture URL)
- Optional Spond match events (one group per fixture URL), with clickable map pins
- Updates existing calendar events when kick-off, opponent, or venue changes
- Email notifications for newly found fixtures
- Email alerts on poll/sync failure (and a recovery mail when it works again)
- Optional Healthchecks.io ping so you are emailed if the container stops
- Short team names in event titles (`TEAM_NAMES`)
- Home/away kit colours on Spond match events (`TEAM_COLOURS`)
- Dry-run mode (geocode and log, do not create events or write state)
- Docker / Dockge deployment; credentials stay on the host via bind mount

## What you need

1. **Docker** (and Docker Compose or Dockge)
2. **FA Full-Time fixture URL(s)** for each team
3. At least one output:
   - Google Calendar (service account JSON + calendar IDs), and/or
   - Spond (email, password, group IDs)
4. **Google Maps API key** (strongly recommended for Spond locations): enable **Geocoding API** and **Places API**

## Setup

### 1. FA Full-Time URL

Open [FA Full-Time](https://fulltime.thefa.com/), filter to your team’s fixtures, copy the browser URL.

### 2. Google Calendar (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **Google Calendar API**
2. Create a service account, download a JSON key as `service_account.json`
3. Share each calendar with the service account `client_email` as **Make changes to events**
4. Copy Calendar IDs from calendar settings → Integrate calendar

Do not bake `service_account.json` into the image. Bind-mount it (see `compose.yaml`).

### 3. Spond (optional)

1. Use the Spond account that can create events in the group
2. Group ID is the last segment of the group URL, e.g. `https://spond.com/landing/group/ABCD1234` → `ABCD1234`
3. Optional host IDs: the member `id` of the coach/manager who should own the event (from Spond network requests, not `clubMembershipId`)

### 4. Maps key (for Spond pins)

1. [Credentials](https://console.cloud.google.com/google/maps-apis/credentials)
2. Enable **Geocoding API** and **Places API** [APIs](https://console.cloud.google.com/google/maps-apis/api-list)
3. Restrict the key to those APIs if you can
4. Set `GOOGLE_MAPS_API_KEY` in `.env`

Without a key, Spond still gets a venue name, but pins fall back to postcode centroids and are often tens or hundreds of metres off. Places is what matches the Google Maps app pin for named grounds (e.g. Castle Vale Stadium).

### 5. Configure and run

```bash
cp .env.example .env
# Edit .env — see the table below
docker compose up -d --build
docker compose logs -f
```

Dockge: new stack from `compose.yaml`, put `service_account.json` and `.env` on the host, deploy.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `FIXTURE_URLS` | Comma-separated FA Full-Time fixture URLs | required |
| `CALENDAR_IDS` | Calendar IDs, one per URL, same order. Empty = no Calendar sync | off |
| `SERVICE_ACCOUNT_FILE` | Path to service account JSON in the container | `/app/service_account.json` |
| `POLL_INTERVAL_HOURS` | How often to poll | `12` |
| `TZ` | Timezone for local kick-off times | `Europe/London` |
| `TEAM_NAMES` | `Full FA name:Short,Other:Short` | none |
| `TEAM_COLOURS` | `Full FA name:home:away` kit colours for Spond. Names (`white`) or quoted hex (`"#ffffff:#8000ff"` — unquoted `#` is a `.env` comment). Opponent colour is omitted | none |
| `SMTP_*` / `EMAIL_FROM` | SMTP login for outgoing mail | off if blank |
| `EMAIL_TO` | Recipients for new-fixture emails | off |
| `EMAIL_TO_ADMIN` | Recipients for poll/sync failure and recovery alerts | off |
| `HEALTHCHECKS_PING_URL` | Ping URL (e.g. Healthchecks.io) after every poll. This is what catches a dead container | off |
| `SPOND_EMAIL` / `SPOND_PASSWORD` | Spond login | off if blank |
| `SPOND_GROUP_IDS` | Group IDs, one per URL, same order | off |
| `SPOND_HOST_IDS` | Event owner member IDs, one per URL | your Spond user |
| `GOOGLE_MAPS_API_KEY` | Geocoding + Places for Spond locations | postcode fallback |
| `DRY_RUN` | `true` = no Calendar/Spond writes and no `synced_fixtures.json` update | `false` |
| `DATA_DIR` | Persistence directory | `/app/data` |

You must configure Calendar and/or Spond. Either output can be omitted.

## How it works

1. **Poll** each fixture URL with Playwright
2. **Parse** date, time, home/away, venue, competition
3. **Dedupe** by date + URL index; hash details to detect updates
4. **Calendar** (if configured): event from 30 minutes before KO, 90 minutes long, no default reminders, short team names in the title
5. **Spond** (if configured): match event at KO, 60 minutes, meetup 30 minutes before, all group members invited, location geocoded for a tappable map pin. If `TEAM_COLOURS` matches the FA team name, `teamColour` is set to the home or away kit and `opponentColour` is left unset.
6. **Email** (if configured): send when a fixture is first seen

### Venue geocoding (Spond)

Implemented in `geocoding.py`. Inputs are the FA venue name and, for away games, the opponent team name — never a hand-copied street address.

1. Web-search for a UK postcode (snippet must mention the venue)
2. Google Geocoding of the venue name (pitch codes like `CV2` stripped)
3. If that is only a town or a street that does not name the venue, **Places Find Place** (Maps-app POI)
4. Same full postcode → Google/Places pin; same district but different unit → web postcode; different district → Google (web search is noisier)

CSV address distance is a rough check only. The real test is tapping the location in the Spond app.

## Local geo tests (optional)

Python 3.10+ in a venv (system `python3` on macOS is often 3.9 and cannot install `spond`).

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # or at least: ddgs spond
```

| Script | Purpose |
|---|---|
| `test_geo_comparison.py` | Compare Nominatim / Google / postcode methods |
| `test_geo_chosen.py` | Run the production pipeline; prints Maps links for each pin |
| `test_spond_locations.py` | Preview or `--create` labelled `[TEST GEO]` Spond **matches** (HOME/AWAY, kit colours, invites only you) |

`test-data.csv` columns: venue name, true address (scoring only), optional team name.

```bash
.venv/bin/python test_geo_chosen.py --google-api-key "$GOOGLE_MAPS_API_KEY"
.venv/bin/python test_spond_locations.py          # preview
.venv/bin/python test_spond_locations.py --create # real Spond matches
```

## Troubleshooting

**403 / Cloudflare on FA Full-Time**  
The list page needs Playwright. Individual fixture *detail* pages are often blocked; the app does not rely on them.

**Spond location not tappable or in the wrong street**  
Set `GOOGLE_MAPS_API_KEY` and enable Places API. Check logs for `[Places]` vs postcode fallback.

**Spond 400 / host not in group**  
Use the member `id` from the group payload, not `clubMembershipId`.

**Calendar duplicates while testing Spond**  
Leave `CALENDAR_IDS` empty, or set `DRY_RUN=true`.

**Re-sync everything**  
Stop the container, delete `data/synced_fixtures.json`, start again. Dry-run does not write this file.

## Alerting

SMTP cannot tell you the container has died — nothing is left to send mail. Use both:

1. **Failure email** (`EMAIL_TO_ADMIN`, same SMTP as fixture mail): scrape failures, missing fixtures table, Calendar/Spond write errors, and uncaught exceptions. A follow-up mail is sent when the next poll succeeds. New-fixture notices still go only to `EMAIL_TO`.
2. **Healthchecks.io** (optional `HEALTHCHECKS_PING_URL`): the container GETs this URL after every poll, even a failed one. If the ping stops, Healthchecks emails you. Set the check period to `POLL_INTERVAL_HOURS` (e.g. 12h) with ~1 hour grace.

Compose also marks the container unhealthy if `data/last_run` is older than about 2.5 poll intervals (Dockge will show it; it does not send email by itself).

## License

[MIT](LICENSE)
