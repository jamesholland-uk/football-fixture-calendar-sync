#!/usr/bin/env python3
"""DRY_RUN must be a full dry run: log what would happen, change nothing outside.

Uses the REAL create/update/send functions with trapped Google and SMTP clients, so any
real write fails the test. (test_sync_routing patches those functions out, which is why
it never noticed Calendar and email were not guarded.) No network calls.

    python3 -m unittest test_dry_run
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import test_sync_routing  # stubs heavy imports, then imports main

main = test_sync_routing.main

FIXTURE = {
    "date": "18/10/26",
    "time": "09:30",
    "home_team": "Home FC U10",
    "away_team": "Away FC U10",
    "venue": "SOME GROUND Pitch 1",
    "competition": "League",
    "detail_url": "https://fulltime.thefa.com/displayFixture.html?id=1",
}


def _run(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class CalendarGuardTest(unittest.TestCase):
    def test_dry_run_create_does_not_touch_google_calendar(self) -> None:
        service = MagicMock()
        with patch.object(main, "DRY_RUN", True):
            event_id, out = _run(main.create_calendar_event, service, "cal", FIXTURE)
        service.events.assert_not_called()
        self.assertTrue(event_id, "must look like success so no error alert is raised")
        self.assertIn("[DRY RUN]", out)

    def test_dry_run_update_does_not_touch_google_calendar(self) -> None:
        service = MagicMock()
        with patch.object(main, "DRY_RUN", True):
            ok, out = _run(main.update_calendar_event, service, "cal", "evt-1", FIXTURE)
        service.events.assert_not_called()
        self.assertTrue(ok)
        self.assertIn("[DRY RUN]", out)

    def test_real_run_still_creates_and_updates(self) -> None:
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "new-id"}
        service.events.reset_mock()
        with patch.object(main, "DRY_RUN", False):
            event_id, _ = _run(main.create_calendar_event, service, "cal", FIXTURE)
            ok, _ = _run(main.update_calendar_event, service, "cal", "evt-1", FIXTURE)
        self.assertEqual(event_id, "new-id")
        self.assertTrue(ok)
        service.events().insert.assert_called()
        service.events().update.assert_called()


class EmailGuardTest(unittest.TestCase):
    def _smtp_patches(self, dry_run: bool):
        return (
            patch.object(main, "DRY_RUN", dry_run),
            patch.object(main, "smtp_configured", lambda: True),
            patch.object(main, "EMAIL_FROM", "from@example.com"),
            patch.object(main, "EMAIL_TO", "parents@example.com"),
            patch.object(main, "EMAIL_TO_ADMIN", "admin@example.com"),
        )

    def test_dry_run_sends_no_email_of_any_kind(self) -> None:
        smtp = MagicMock(side_effect=AssertionError("SMTP must not be used in a dry run"))
        patches = self._smtp_patches(True)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patch.object(main.smtplib, "SMTP", smtp):
                sent, out1 = _run(main.send_email, "subject", "body", "x@example.com")
                notified, out2 = _run(main.send_email_notification, FIXTURE)
                _, out3 = _run(main.send_alert, "Fixture sync failed", "boom")
        smtp.assert_not_called()
        self.assertTrue(sent)
        self.assertTrue(notified)
        for out in (out1, out2, out3):
            self.assertIn("[DRY RUN]", out)

    def test_real_run_still_sends_email(self) -> None:
        smtp = MagicMock()
        patches = self._smtp_patches(False)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patch.object(main.smtplib, "SMTP", smtp), patch.object(main, "SMTP_USER", "u"), patch.object(main, "SMTP_PASSWORD", "p"):
                sent, _ = _run(main.send_email, "subject", "body", "x@example.com")
        self.assertTrue(sent)
        smtp.assert_called_once()


class FullSyncDryRunTest(unittest.TestCase):
    def test_new_and_changed_fixtures_write_nothing_and_raise_no_errors(self) -> None:
        service = MagicMock()
        smtp = MagicMock(side_effect=AssertionError("SMTP must not be used in a dry run"))
        changed = dict(FIXTURE, date="19/10/26")
        # An existing entry with a stale hash drives the "changed -> update" path.
        existing = {
            main.get_fixture_date_key(changed, 0): {"event_id": "evt-9", "hash": "stale"}
        }
        with patch.multiple(
            main,
            DRY_RUN=True,
            CALENDAR_IDS=["cal@group.calendar.google.com"],
            SPOND_GROUP_IDS=[""],
            EMAIL_TO="parents@example.com",
            EMAIL_TO_ADMIN="admin@example.com",
        ):
            with (
                patch.object(main, "smtp_configured", lambda: True),
                patch.object(main.smtplib, "SMTP", smtp),
                patch.object(main, "get_calendar_service", return_value=service),
                patch.object(main, "load_synced_fixtures", return_value=existing),
                patch.object(main, "save_synced_fixtures") as save,
            ):
                errors, out = _run(main.sync_to_calendar, [(0, FIXTURE), (0, changed)])

        self.assertEqual(errors, [], "a dry run must not raise 'create failed' alerts")
        service.events.assert_not_called()
        smtp.assert_not_called()
        save.assert_not_called()
        self.assertIn("Added 1 new, updated 1 existing", out)


if __name__ == "__main__":
    unittest.main()
