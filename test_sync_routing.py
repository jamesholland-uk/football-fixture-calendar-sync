"""Prove fixture URLs are paired with Calendar and Spond by slot.

The first slot can skip Calendar and still create a Spond event. A later slot
can write a calendar and skip Spond when it has no group ID. No network calls.

    python3 -m unittest test_sync_routing
"""

from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _install_import_stubs() -> None:
    """main.py imports browser and API clients at import time. Stub them."""

    class HttpError(Exception):
        pass

    def stub(name: str, **attrs: object) -> types.ModuleType:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    stub("bs4", BeautifulSoup=object)
    stub("schedule")
    stub("playwright")
    sync_api = stub("playwright.sync_api", sync_playwright=lambda: None)
    sys.modules["playwright"].sync_api = sync_api
    google = stub("google")
    oauth2 = stub("google.oauth2", service_account=types.SimpleNamespace())
    google.oauth2 = oauth2
    googleapiclient = stub("googleapiclient")
    discovery = stub("googleapiclient.discovery", build=lambda *args, **kwargs: None)
    errors = stub("googleapiclient.errors", HttpError=HttpError)
    googleapiclient.discovery = discovery
    googleapiclient.errors = errors
    stub("spond", spond=object)


_install_import_stubs()
import main  # noqa: E402


ROOT = Path(__file__).resolve().parent


def _fixture(home_team: str, date: str) -> dict:
    return {
        "date": date,
        "time": "10:00",
        "home_team": home_team,
        "away_team": "Opponents",
        "venue": "Pitch",
    }


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


class SyncRoutingTests(unittest.TestCase):
    def test_first_url_spond_only_and_second_url_calendar_only(self) -> None:
        calendar_calls: list[tuple[str, str]] = []
        spond_calls: list[tuple[str, str, str]] = []

        def fake_calendar(service, calendar_id: str, fixture: dict) -> str:
            calendar_calls.append((calendar_id, fixture["home_team"]))
            return "event-1"

        def fake_spond(fixture: dict, group_id: str, host_id: str = "") -> bool:
            spond_calls.append((group_id, host_id, fixture["home_team"]))
            return True

        with patch.multiple(
            main,
            CALENDAR_IDS=["", "second@group.calendar.google.com"],
            SPOND_GROUP_IDS=["group-for-first-url"],
            SPOND_HOST_IDS=["host-for-first-url"],
            DRY_RUN=True,
        ):
            with (
                patch.object(main, "create_calendar_event", fake_calendar),
                patch.object(main, "create_spond_event_sync", fake_spond),
                patch.object(main, "get_calendar_service", return_value=object()),
                patch.object(main, "load_synced_fixtures", return_value={}),
                patch.object(main, "save_synced_fixtures") as save_synced,
                patch.object(main, "send_email_notification", return_value=False),
            ):
                with redirect_stdout(io.StringIO()):
                    errors = main.sync_to_calendar(
                        [
                            (0, _fixture("First URL", "01/10/26")),
                            (1, _fixture("Second URL", "02/10/26")),
                        ]
                    )

        self.assertEqual(errors, [])
        self.assertEqual(calendar_calls, [("second@group.calendar.google.com", "Second URL")])
        self.assertEqual(spond_calls, [("group-for-first-url", "host-for-first-url", "First URL")])
        save_synced.assert_not_called()

    def test_blank_calendar_slot_passes_validation(self) -> None:
        with tempfile.NamedTemporaryFile() as service_account:
            with patch.multiple(
                main,
                FIXTURE_URLS=["https://example.test/first", "https://example.test/second"],
                CALENDAR_IDS=["", "second@group.calendar.google.com"],
                SPOND_GROUP_IDS=["group-for-first-url"],
                SERVICE_ACCOUNT_FILE=service_account.name,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(main.validate_config())

    def test_calendar_slot_count_must_match_urls(self) -> None:
        with tempfile.NamedTemporaryFile() as service_account:
            with patch.multiple(
                main,
                FIXTURE_URLS=["https://example.test/first", "https://example.test/second"],
                CALENDAR_IDS=["second@group.calendar.google.com"],
                SPOND_GROUP_IDS=["group-for-first-url"],
                SERVICE_ACCOUNT_FILE=service_account.name,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertFalse(main.validate_config())


class LiveEnvShapeTests(unittest.TestCase):
    def test_live_env_matches_first_spond_second_calendar(self) -> None:
        env_path = ROOT / ".env"
        if not env_path.exists():
            self.skipTest(".env is not present")

        env = _parse_env(env_path)
        urls = env.get("FIXTURE_URLS", "").split(",")
        calendars = env.get("CALENDAR_IDS", "").split(",")
        groups = env.get("SPOND_GROUP_IDS", "").split(",")

        self.assertEqual([bool(url.strip()) for url in urls], [True, True])
        self.assertEqual([bool(calendar.strip()) for calendar in calendars], [False, True])
        self.assertTrue(groups[0].strip())
        self.assertFalse(any(group.strip() for group in groups[1:]))
        self.assertFalse(env.get("EMAIL_TO", "").strip())
        self.assertTrue(env.get("EMAIL_TO_ADMIN", "").strip())
        self.assertTrue(env.get("SMTP_USER", "").strip())


if __name__ == "__main__":
    unittest.main()
