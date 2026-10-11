#!/usr/bin/env python3
"""Unit tests for the admin "venue needs a look" report (no network, no SMTP)."""

import json
import tempfile
import unittest
from pathlib import Path

from venue_review import (
    build_venue_review_email,
    load_reviewed_venues,
    mark_venue_reviewed,
    venue_review_key,
)

FIXTURE = {
    "date": "11/10/26",
    "time": "09:30",
    "home_team": "Home FC U10",
    "away_team": "Away FC U10",
    "venue": "DAMSON PARK 3G Pitch 1D - 09:30AM",
    "detail_url": "https://fulltime.thefa.com/displayFixture.html?id=1",
}
GEOCODED = {
    "latitude": 52.421303,
    "longitude": -1.7611479,
    "addressLine": "3 Damson Pkwy, Solihull B91 2PP, UK",
    "postalCode": "B91 2PP",
    "feature": "DAMSON PARK 3G Pitch 1D",
}


class ReviewKeyTest(unittest.TestCase):
    def test_pitch_suffixes_share_one_key(self) -> None:
        self.assertEqual(
            venue_review_key("DAMSON PARK 3G Pitch 1D"),
            venue_review_key("damson park pitch 2"),
        )

    def test_different_venues_differ(self) -> None:
        self.assertNotEqual(venue_review_key("DAMSON PARK"), venue_review_key("OTHER PARK"))


class ReviewedStoreTest(unittest.TestCase):
    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_reviewed_venues(Path(d) / "nope.json"), {})

    def test_corrupt_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_reviewed_venues(p), {})

    def test_mark_then_load_round_trips_and_creates_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "r.json"
            mark_venue_reviewed(p, "DAMSON PARK", "3 Damson Pkwy, B91 2PP")
            mark_venue_reviewed(p, "OTHER", "x")
            data = load_reviewed_venues(p)
            self.assertEqual(set(data), {"DAMSON PARK", "OTHER"})
            self.assertEqual(data["DAMSON PARK"]["resolved"], "3 Damson Pkwy, B91 2PP")
            json.loads(p.read_text(encoding="utf-8"))  # valid JSON on disk


class ReviewEmailTest(unittest.TestCase):
    def test_resolved_venue_email_has_what_admin_needs(self) -> None:
        subject, body = build_venue_review_email(
            "DAMSON PARK 3G Pitch 1D", FIXTURE, GEOCODED, geocoded=True
        )
        self.assertIn("DAMSON PARK", subject)
        self.assertIn("3 Damson Pkwy, Solihull B91 2PP, UK", body)
        self.assertIn("https://www.google.com/maps?q=52.421303,-1.7611479", body)
        self.assertIn(FIXTURE["detail_url"], body)  # where to read the real address
        self.assertIn("VENUE_ADDRESSES=DAMSON PARK=", body)  # cleaned name, ready to paste
        self.assertIn("11/10/26", body)

    def test_line_merges_existing_pins_so_it_is_one_paste(self) -> None:
        existing = {"OTHER GROUND": "1 HIGH ST, TOWN, AB1 2CD"}
        _, body = build_venue_review_email(
            "DAMSON PARK 3G Pitch 1D", FIXTURE, GEOCODED, geocoded=True, existing=existing
        )
        self.assertIn(
            "VENUE_ADDRESSES=OTHER GROUND=1 HIGH ST, TOWN, AB1 2CD;"
            "DAMSON PARK=<paste the address from the FA fixture page>",
            body,
        )

    def test_no_existing_pins_gives_a_single_entry(self) -> None:
        _, body = build_venue_review_email("DAMSON PARK", FIXTURE, GEOCODED, geocoded=True)
        self.assertIn(
            "VENUE_ADDRESSES=DAMSON PARK=<paste the address from the FA fixture page>", body
        )
        self.assertNotIn("VENUE_ADDRESSES=;", body)

    def test_guess_line_offered_only_for_a_correct_pin(self) -> None:
        existing = {"OTHER GROUND": "1 HIGH ST, TOWN, AB1 2CD"}
        _, body = build_venue_review_email(
            "DAMSON PARK", FIXTURE, GEOCODED, geocoded=True, existing=existing
        )
        self.assertIn("If the pin above is correct", body)
        self.assertIn(
            "VENUE_ADDRESSES=OTHER GROUND=1 HIGH ST, TOWN, AB1 2CD;"
            "DAMSON PARK=3 Damson Pkwy, Solihull B91 2PP, UK",
            body,
        )

    def test_guess_line_never_offered_when_nothing_was_located(self) -> None:
        _, body = build_venue_review_email(
            "NOWHERE FIELD", FIXTURE, {"feature": "NOWHERE FIELD"}, geocoded=False
        )
        self.assertNotIn("If the pin above is correct", body)

    def test_says_what_to_do_when_the_fa_address_has_no_postcode(self) -> None:
        for geocoded, location in ((True, GEOCODED), (False, {"feature": "X"})):
            _, body = build_venue_review_email("DAMSON PARK", FIXTURE, location, geocoded)
            self.assertIn("no postcode", body.lower())
            self.assertIn("Google Maps", body)
            self.assertIn("without a postcode the pin is ignored", body.lower())

    def test_tells_admin_to_recreate_container_and_fix_the_event(self) -> None:
        _, body = build_venue_review_email("DAMSON PARK", FIXTURE, GEOCODED, geocoded=True)
        self.assertIn("docker compose up -d", body)
        self.assertIn("recreate", body.lower())
        self.assertIn("Spond", body)
        self.assertIn("by hand", body)

    def test_failed_geocode_email_says_so(self) -> None:
        subject, body = build_venue_review_email(
            "NOWHERE FIELD", FIXTURE, {"feature": "NOWHERE FIELD"}, geocoded=False
        )
        self.assertIn("NOWHERE FIELD", subject)
        self.assertIn("could not be located", body.lower())
        self.assertNotIn("google.com/maps?q=", body)

    def test_no_detail_url_does_not_crash(self) -> None:
        fixture = dict(FIXTURE)
        fixture.pop("detail_url")
        _, body = build_venue_review_email("DAMSON PARK", fixture, GEOCODED, geocoded=True)
        self.assertIn("VENUE_ADDRESSES=", body)


class ReportUnpinnedVenueTest(unittest.TestCase):
    """main.report_unpinned_venue: admin-only, once per venue, silent on dry run."""

    def setUp(self) -> None:
        from unittest import mock

        import test_sync_routing  # stubs heavy imports, then imports main

        self.main = test_sync_routing.main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list[tuple[str, str, str]] = []

        def fake_send(subject: str, body: str, to: str) -> bool:
            self.sent.append((subject, body, to))
            return True

        patches = [
            mock.patch.object(self.main, "DATA_DIR", self.tmp.name),
            mock.patch.object(self.main, "DRY_RUN", False),
            mock.patch.object(self.main, "EMAIL_TO", "parents@example.com"),
            mock.patch.object(self.main, "EMAIL_TO_ADMIN", "admin@example.com"),
            mock.patch.object(self.main, "smtp_configured", lambda: True),
            mock.patch.object(self.main, "send_email", fake_send),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _report(self, venue: str = "DAMSON PARK 3G Pitch 1D") -> bool:
        return self.main.report_unpinned_venue(venue, FIXTURE, GEOCODED, True)

    def test_emails_admin_only_not_parents(self) -> None:
        self.assertTrue(self._report())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2], "admin@example.com")
        self.assertIn("Check venue pin", self.sent[0][0])

    def test_reports_each_venue_once_even_for_another_pitch(self) -> None:
        self.assertTrue(self._report("DAMSON PARK 3G Pitch 1D"))
        self.assertFalse(self._report("DAMSON PARK Pitch 2"))
        self.assertEqual(len(self.sent), 1)

    def test_different_venue_is_reported_separately(self) -> None:
        self._report("DAMSON PARK")
        self.assertTrue(self._report("OTHER GROUND"))
        self.assertEqual(len(self.sent), 2)

    def test_dry_run_sends_nothing_and_records_nothing(self) -> None:
        from unittest import mock

        with mock.patch.object(self.main, "DRY_RUN", True):
            self.assertFalse(self._report())
        self.assertEqual(self.sent, [])
        self.assertEqual(load_reviewed_venues(self.main.reviewed_venues_path()), {})

    def test_no_admin_email_means_no_send_and_retry_later(self) -> None:
        from unittest import mock

        with mock.patch.object(self.main, "EMAIL_TO_ADMIN", ""):
            self.assertFalse(self._report())
        self.assertEqual(self.sent, [])
        self.assertTrue(self._report())  # admin configured later: still reported

    def test_failed_send_is_retried_not_recorded(self) -> None:
        from unittest import mock

        with mock.patch.object(self.main, "send_email", lambda *a: False):
            self.assertFalse(self._report())
        self.assertTrue(self._report())

    def test_never_raises(self) -> None:
        from unittest import mock

        with mock.patch.object(self.main, "send_email", side_effect=RuntimeError("smtp down")):
            self.assertFalse(self._report())


if __name__ == "__main__":
    unittest.main()
