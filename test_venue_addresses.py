#!/usr/bin/env python3
"""Unit tests for VENUE_ADDRESSES (pinned FA addresses) and the pinned geocode path.

Regression: the FA list page says "EXAMPLE PARK 3G Pitch 1D - 09:30AM". The bare name
"EXAMPLE PARK" made web search and Google agree on Example Parkway (EX2 2BB), 2.4 km from
the ground (EX1 1AA). A pinned address must win over that guess. No network is used.
"""

import unittest
from unittest import mock

import geocoding
from venue_addresses import find_venue_address, parse_venue_addresses

PINNED = "EXAMPLE PARK, EXAMPLE PARK EXAMPLE PARKWAY, EXAMPLETON, EX1 1AA"

GOOD_PIN = {
    "lat": 50.1000,
    "lon": -1.1000,
    "display": "The Example Arena, Example Pkwy, Exampleton EX1 1AA, UK",
    "postcode": "EX1 1AA",
    "source": "google",
}
EXAMPLE_PKWY_STREET = {
    "lat": 50.0900,
    "lon": -1.0900,
    "display": "3 Example Pkwy, Exampleton EX2 2BB, UK",
    "postcode": "EX2 2BB",
    "source": "google",
}
POSTCODE_CENTROID = {
    "lat": 50.1001,
    "lon": -1.1001,
    "display": "EX1 1AA (Exampleton Ward, Exampleton)",
    "postcode": "EX1 1AA",
    "source": "postcode.io",
}


class ParseVenueAddressesTest(unittest.TestCase):
    def test_semicolon_separates_venues_and_commas_stay_in_address(self) -> None:
        raw = f"EXAMPLE PARK={PINNED};OTHER GROUND=1 HIGH ST, TOWN, AB1 2CD"
        self.assertEqual(
            parse_venue_addresses(raw),
            {
                "EXAMPLE PARK": PINNED,
                "OTHER GROUND": "1 HIGH ST, TOWN, AB1 2CD",
            },
        )

    def test_empty_and_malformed_entries_are_skipped(self) -> None:
        self.assertEqual(parse_venue_addresses(""), {})
        self.assertEqual(parse_venue_addresses(" ; ;NOEQUALS;=NONAME;NOADDR="), {})

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            parse_venue_addresses("  EXAMPLE PARK  =  EX1 1AA  "),
            {"EXAMPLE PARK": "EX1 1AA"},
        )

    def test_address_may_contain_equals(self) -> None:
        self.assertEqual(
            parse_venue_addresses("A=B=C"),
            {"A": "B=C"},
        )


class FindVenueAddressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = {"EXAMPLE PARK": PINNED}

    def test_matches_after_pitch_suffix_is_stripped(self) -> None:
        self.assertEqual(
            find_venue_address("EXAMPLE PARK 3G Pitch 1D", self.mapping), PINNED
        )

    def test_is_case_insensitive_on_both_sides(self) -> None:
        self.assertEqual(
            find_venue_address("example park", {"Example Park": PINNED}), PINNED
        )

    def test_unknown_venue_returns_none(self) -> None:
        self.assertIsNone(find_venue_address("SOMEWHERE ELSE", self.mapping))

    def test_no_partial_match(self) -> None:
        self.assertIsNone(find_venue_address("EXAMPLE PARK EXTENSION", self.mapping))

    def test_one_pin_covers_every_lettered_pitch_at_a_ground(self) -> None:
        # EXA1A and EXA2B are two pitches at one ground; a pin copied from either covers both
        mapping = {"THE EXAMPLE ARENA EXA1A": PINNED}
        for venue in ("THE EXAMPLE ARENA EXA1A", "THE EXAMPLE ARENA EXA2B", "THE EXAMPLE ARENA"):
            with self.subTest(venue=venue):
                self.assertEqual(find_venue_address(venue, mapping), PINNED)


class PinnedGeocodeTest(unittest.TestCase):
    """geocode_chosen(..., address=...) must trust the pinned postcode, not guesses."""

    def _patch(self, google_by_query, places=None):
        web = mock.Mock(side_effect=AssertionError("web search must not run when pinned"))
        return mock.patch.multiple(
            geocoding,
            search_postcode_web=web,
            geocode_google=mock.Mock(side_effect=google_by_query),
            geocode_google_places=mock.Mock(return_value=places),
            geocode_postcode_io=mock.Mock(return_value=POSTCODE_CENTROID),
        )

    def test_pinned_address_beats_street_level_guess(self) -> None:
        # Bare "EXAMPLE PARK ..." queries land on Example Parkway; the pinned address
        # (with its postcode) lands on the real ground. Only the latter must be used.
        def google(query, key):
            return GOOD_PIN if "EX1 1AA" in query else EXAMPLE_PKWY_STREET

        with self._patch(google):
            result, reason = geocoding.geocode_chosen(
                "EXAMPLE PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "EX1 1AA")
        self.assertEqual(result["display"], GOOD_PIN["display"])
        self.assertIn("pinned", reason.lower())

    def test_wrong_postcode_from_google_is_rejected_for_places(self) -> None:
        places = dict(GOOD_PIN, source="google_places")
        with self._patch(lambda q, k: EXAMPLE_PKWY_STREET, places=places):
            result, reason = geocoding.geocode_chosen(
                "EXAMPLE PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "EX1 1AA")
        self.assertEqual(result["source"], "google_places")

    def test_falls_back_to_pinned_postcode_centroid_not_a_wrong_pin(self) -> None:
        with self._patch(lambda q, k: EXAMPLE_PKWY_STREET, places=None):
            result, reason = geocoding.geocode_chosen(
                "EXAMPLE PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "EX1 1AA")
        self.assertEqual(result["source"], "postcode.io")

    def test_pinned_without_api_key_uses_postcode_centroid(self) -> None:
        with self._patch(lambda q, k: self.fail("Google must not be called")):
            result, _ = geocoding.geocode_chosen(
                "EXAMPLE PARK 3G Pitch 1D", address=PINNED, google_api_key=""
            )
        self.assertEqual(result["postcode"], "EX1 1AA")

    def test_geocode_venue_passes_address_through(self) -> None:
        def google(query, key):
            return GOOD_PIN if "EX1 1AA" in query else EXAMPLE_PKWY_STREET

        with self._patch(google), mock.patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "k"}):
            loc = geocoding.geocode_venue("EXAMPLE PARK 3G Pitch 1D", address=PINNED)
        self.assertEqual(loc["postalCode"], "EX1 1AA")
        self.assertEqual(loc["feature"], "EXAMPLE PARK 3G Pitch 1D")


if __name__ == "__main__":
    unittest.main()
