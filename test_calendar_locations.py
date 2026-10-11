#!/usr/bin/env python3
"""Calendar events get a real, geocoded location (not just the FA venue text).

Google Calendar's `location` is free text, so the pin is: the resolved full address in
`location` (Maps resolves it precisely) plus an exact-coordinates Map link in the
description. Geocoding is shared with Spond and cached per venue. No network.

    python3 -m unittest test_calendar_locations
"""

from __future__ import annotations

import asyncio
import io
import types
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
    "venue": "EXAMPLE PARK 3G Pitch 1D  - 09:30AM",
    "competition": "League",
}
GEOCODED = {
    "latitude": 50.09,
    "longitude": -1.09,
    "feature": "EXAMPLE PARK 3G Pitch 1D",
    "featureName": "EXAMPLE PARK 3G Pitch 1D",
    "address": "The Example Arena",
    "addressLine": "The Example Arena, Example Pkwy, Exampleton EX1 1AA, UK",
    "postalCode": "EX1 1AA",
    "country": "GB",
}
CENTROID = dict(GEOCODED, addressLine="EX1 1AA (Exampleton Ward, Exampleton)", address="EX1 1AA")


def _quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class CalendarLocationTextTest(unittest.TestCase):
    def test_uses_the_resolved_address(self) -> None:
        self.assertEqual(
            main.calendar_location_text(GEOCODED, "EXAMPLE PARK 3G Pitch 1D"),
            "The Example Arena, Example Pkwy, Exampleton EX1 1AA, UK",
        )

    def test_postcode_centroid_gets_the_venue_name_in_front(self) -> None:
        # "EX1 1AA (Exampleton Ward...)" alone would not tell Maps which ground
        self.assertEqual(
            main.calendar_location_text(CENTROID, "EXAMPLE PARK 3G Pitch 1D"),
            "EXAMPLE PARK 3G Pitch 1D, EX1 1AA",
        )

    def test_not_geocoded_falls_back_to_the_fa_name(self) -> None:
        self.assertEqual(main.calendar_location_text({}, "EXAMPLE PARK"), "EXAMPLE PARK")
        self.assertEqual(
            main.calendar_location_text({"feature": "EXAMPLE PARK"}, "EXAMPLE PARK"), "EXAMPLE PARK"
        )


class BuildCalendarEventTest(unittest.TestCase):
    def test_geocoded_location_and_exact_map_link(self) -> None:
        event, _ = _quiet(main.build_calendar_event, FIXTURE, GEOCODED)
        self.assertEqual(event["location"], GEOCODED["addressLine"])
        self.assertIn("Map: https://www.google.com/maps?q=50.09,-1.09", event["description"])
        self.assertIn("Venue: EXAMPLE PARK 3G Pitch 1D", event["description"])  # FA text kept

    def test_without_a_location_it_is_the_fa_venue_name_as_before(self) -> None:
        event, _ = _quiet(main.build_calendar_event, FIXTURE)
        self.assertEqual(event["location"], "EXAMPLE PARK 3G Pitch 1D")  # no trailing space
        self.assertNotIn("google.com/maps", event["description"])

    def test_failed_geocode_is_plain_text_and_has_no_map_link(self) -> None:
        event, _ = _quiet(main.build_calendar_event, FIXTURE, {"feature": "EXAMPLE PARK 3G Pitch 1D"})
        self.assertEqual(event["location"], "EXAMPLE PARK 3G Pitch 1D")
        self.assertNotIn("google.com/maps", event["description"])

    def test_fixture_with_no_venue_has_no_location(self) -> None:
        fixture = {k: v for k, v in FIXTURE.items() if k != "venue"}
        event, _ = _quiet(main.build_calendar_event, fixture, {})
        self.assertNotIn("location", event)


class ResolveVenueLocationTest(unittest.TestCase):
    def setUp(self) -> None:
        main._LOCATION_CACHE.clear()
        self.addCleanup(main._LOCATION_CACHE.clear)
        self.geocode = MagicMock(return_value=GEOCODED)
        self.report = MagicMock()
        for p in (
            patch.object(main, "geocode_venue", self.geocode),
            patch.object(main, "report_unpinned_venue", self.report),
            patch.object(main, "VENUE_ADDRESSES", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _resolve(self, fixture=FIXTURE, is_home=True):
        return _quiet(main.resolve_venue_location, fixture, is_home)[0]

    def test_geocodes_the_clean_venue_name(self) -> None:
        self.assertEqual(self._resolve(), GEOCODED)
        self.geocode.assert_called_once_with("EXAMPLE PARK 3G Pitch 1D", "", address=None)

    def test_away_game_passes_the_opponent_as_hint(self) -> None:
        self._resolve(is_home=False)
        self.geocode.assert_called_once_with("EXAMPLE PARK 3G Pitch 1D", "Home FC U10", address=None)

    def test_pinned_venue_is_used_and_not_reported(self) -> None:
        pinned = "EXAMPLE PARK, EXAMPLETON, EX1 1AA"
        with patch.object(main, "VENUE_ADDRESSES", {"EXAMPLE PARK": pinned}):
            self._resolve()
        self.geocode.assert_called_once_with("EXAMPLE PARK 3G Pitch 1D", "", address=pinned)
        self.report.assert_not_called()

    def test_unpinned_venue_is_reported_to_the_admin(self) -> None:
        self._resolve()
        self.report.assert_called_once()
        self.assertEqual(self.report.call_args.args[3], True)  # geocoded

    def test_second_call_for_the_same_venue_is_cached(self) -> None:
        self._resolve()
        self._resolve()
        self.geocode.assert_called_once()

    def test_failure_is_plain_text_reported_and_not_cached(self) -> None:
        self.geocode.return_value = None
        self.assertEqual(self._resolve(), {"feature": "EXAMPLE PARK 3G Pitch 1D"})
        self.assertEqual(self.report.call_args.args[3], False)
        self.geocode.return_value = GEOCODED
        self.assertEqual(self._resolve(), GEOCODED)  # retried, not stuck on the failure
        self.assertEqual(self.geocode.call_count, 2)

    def test_no_venue_means_empty_location(self) -> None:
        self.assertEqual(self._resolve({k: v for k, v in FIXTURE.items() if k != "venue"}), {})
        self.geocode.assert_not_called()


class SyncUsesGeocodedLocationTest(unittest.TestCase):
    def setUp(self) -> None:
        main._LOCATION_CACHE.clear()
        self.addCleanup(main._LOCATION_CACHE.clear)
        self.service = MagicMock()
        self.service.events().insert().execute.return_value = {"id": "new-id"}
        self.service.events.reset_mock()
        self.geocode = MagicMock(return_value=GEOCODED)
        for p in (
            patch.multiple(
                main,
                DRY_RUN=False,
                CALENDAR_IDS=["cal@group.calendar.google.com"],
                SPOND_GROUP_IDS=[""],
                VENUE_ADDRESSES={},
            ),
            patch.object(main, "geocode_venue", self.geocode),
            patch.object(main, "report_unpinned_venue"),
            patch.object(main, "get_calendar_service", return_value=self.service),
            patch.object(main, "save_synced_fixtures"),
            patch.object(main, "send_email_notification", return_value=False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_new_fixture_creates_event_with_the_real_location(self) -> None:
        with patch.object(main, "load_synced_fixtures", return_value={}):
            errors, _ = _quiet(main.sync_to_calendar, [(0, FIXTURE)])
        self.assertEqual(errors, [])
        body = self.service.events().insert.call_args.kwargs["body"]
        self.assertEqual(body["location"], GEOCODED["addressLine"])
        self.assertIn("google.com/maps?q=50.09,-1.09", body["description"])

    def test_changed_fixture_updates_event_with_the_real_location(self) -> None:
        existing = {main.get_fixture_date_key(FIXTURE, 0): {"event_id": "evt-9", "hash": "stale"}}
        with patch.object(main, "load_synced_fixtures", return_value=existing):
            errors, _ = _quiet(main.sync_to_calendar, [(0, FIXTURE)])
        self.assertEqual(errors, [])
        body = self.service.events().update.call_args.kwargs["body"]
        self.assertEqual(body["location"], GEOCODED["addressLine"])

    def test_geocoding_crash_never_blocks_the_calendar_event(self) -> None:
        self.geocode.side_effect = RuntimeError("geocoder down")
        with patch.object(main, "load_synced_fixtures", return_value={}):
            errors, _ = _quiet(main.sync_to_calendar, [(0, FIXTURE)])
        self.assertEqual(errors, [])
        body = self.service.events().insert.call_args.kwargs["body"]
        self.assertEqual(body["location"], "EXAMPLE PARK 3G Pitch 1D")

    def test_spond_and_calendar_for_one_fixture_geocode_once(self) -> None:
        spond_seen: list[dict] = []

        def fake_spond(fixture, group_id, host_ids=None):
            spond_seen.append(main.resolve_venue_location(fixture, main.fixture_is_home(fixture)[1]))
            return True

        with patch.multiple(main, SPOND_GROUP_IDS=["g1"]), patch.object(
            main, "create_spond_event_sync", fake_spond
        ), patch.object(main, "load_synced_fixtures", return_value={}):
            _quiet(main.sync_to_calendar, [(0, FIXTURE)])
        self.assertEqual(spond_seen, [GEOCODED])
        self.geocode.assert_called_once()


class SpondUsesTheSharedResolverTest(unittest.TestCase):
    def test_spond_event_location_comes_from_the_resolver(self) -> None:
        class FakeSpond:
            def __init__(self, username, password):
                async def close():
                    return None

                self.clientsession = types.SimpleNamespace(close=close)

            async def login(self):
                return None

            async def get_groups(self):
                return [{"id": "g1", "name": "Team", "members": [{"id": "m1"}]}]

        resolver = MagicMock(return_value=GEOCODED)
        with patch.multiple(
            main, DRY_RUN=True, SPOND_EMAIL="e@example.com", SPOND_PASSWORD="p"
        ), patch.object(main, "spond", types.SimpleNamespace(Spond=FakeSpond)), patch.object(
            main, "resolve_venue_location", resolver
        ):
            ok, out = _quiet(lambda: asyncio.run(main.create_spond_event(FIXTURE, "g1", [])))
        self.assertTrue(ok)
        resolver.assert_called_once()
        self.assertEqual(resolver.call_args.args[0], FIXTURE)
        self.assertIn("'latitude': 50.09", out)
        self.assertIn("[DRY RUN] Would create Spond event", out)


if __name__ == "__main__":
    unittest.main()
