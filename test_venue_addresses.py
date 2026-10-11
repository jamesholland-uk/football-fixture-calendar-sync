#!/usr/bin/env python3
"""Unit tests for VENUE_ADDRESSES (pinned FA addresses) and the pinned geocode path.

Regression: the FA list page says "DAMSON PARK 3G Pitch 1D - 09:30AM". The bare name
"DAMSON PARK" made web search and Google agree on Damson Parkway (B91 2PP), 2.4 km from
the ground (B92 9EJ). A pinned address must win over that guess. No network is used.
"""

import unittest
from unittest import mock

import geocoding
from venue_addresses import find_venue_address, parse_venue_addresses

PINNED = "DAMSON PARK, DAMSON PARK DAMSON PARKWAY, SOLIHULL, B92 9EJ"

ARMCO = {
    "lat": 52.4432,
    "lon": -1.7616,
    "display": "The ARMCO Arena, Damson Pkwy, Solihull B92 9EJ, UK",
    "postcode": "B92 9EJ",
    "source": "google",
}
DAMSON_PKWY_STREET = {
    "lat": 52.421303,
    "lon": -1.7611479,
    "display": "3 Damson Pkwy, Solihull B91 2PP, UK",
    "postcode": "B91 2PP",
    "source": "google",
}
POSTCODE_CENTROID = {
    "lat": 52.443184,
    "lon": -1.761603,
    "display": "B92 9EJ (Elmdon, Solihull)",
    "postcode": "B92 9EJ",
    "source": "postcode.io",
}


class ParseVenueAddressesTest(unittest.TestCase):
    def test_semicolon_separates_venues_and_commas_stay_in_address(self) -> None:
        raw = f"DAMSON PARK={PINNED};OTHER GROUND=1 HIGH ST, TOWN, AB1 2CD"
        self.assertEqual(
            parse_venue_addresses(raw),
            {
                "DAMSON PARK": PINNED,
                "OTHER GROUND": "1 HIGH ST, TOWN, AB1 2CD",
            },
        )

    def test_empty_and_malformed_entries_are_skipped(self) -> None:
        self.assertEqual(parse_venue_addresses(""), {})
        self.assertEqual(parse_venue_addresses(" ; ;NOEQUALS;=NONAME;NOADDR="), {})

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            parse_venue_addresses("  DAMSON PARK  =  B92 9EJ  "),
            {"DAMSON PARK": "B92 9EJ"},
        )

    def test_address_may_contain_equals(self) -> None:
        self.assertEqual(
            parse_venue_addresses("A=B=C"),
            {"A": "B=C"},
        )


class FindVenueAddressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = {"DAMSON PARK": PINNED}

    def test_matches_after_pitch_suffix_is_stripped(self) -> None:
        self.assertEqual(
            find_venue_address("DAMSON PARK 3G Pitch 1D", self.mapping), PINNED
        )

    def test_is_case_insensitive_on_both_sides(self) -> None:
        self.assertEqual(
            find_venue_address("damson park", {"Damson Park": PINNED}), PINNED
        )

    def test_unknown_venue_returns_none(self) -> None:
        self.assertIsNone(find_venue_address("SOMEWHERE ELSE", self.mapping))

    def test_no_partial_match(self) -> None:
        self.assertIsNone(find_venue_address("DAMSON PARK EXTENSION", self.mapping))


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
        # Bare "DAMSON PARK ..." queries land on Damson Parkway; the pinned address
        # (with its postcode) lands on the ARMCO Arena. Only the latter must be used.
        def google(query, key):
            return ARMCO if "B92 9EJ" in query else DAMSON_PKWY_STREET

        with self._patch(google):
            result, reason = geocoding.geocode_chosen(
                "DAMSON PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "B92 9EJ")
        self.assertEqual(result["display"], ARMCO["display"])
        self.assertIn("pinned", reason.lower())

    def test_wrong_postcode_from_google_is_rejected_for_places(self) -> None:
        places = dict(ARMCO, source="google_places")
        with self._patch(lambda q, k: DAMSON_PKWY_STREET, places=places):
            result, reason = geocoding.geocode_chosen(
                "DAMSON PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "B92 9EJ")
        self.assertEqual(result["source"], "google_places")

    def test_falls_back_to_pinned_postcode_centroid_not_a_wrong_pin(self) -> None:
        with self._patch(lambda q, k: DAMSON_PKWY_STREET, places=None):
            result, reason = geocoding.geocode_chosen(
                "DAMSON PARK 3G Pitch 1D", address=PINNED, google_api_key="k"
            )
        self.assertEqual(result["postcode"], "B92 9EJ")
        self.assertEqual(result["source"], "postcode.io")

    def test_pinned_without_api_key_uses_postcode_centroid(self) -> None:
        with self._patch(lambda q, k: self.fail("Google must not be called")):
            result, _ = geocoding.geocode_chosen(
                "DAMSON PARK 3G Pitch 1D", address=PINNED, google_api_key=""
            )
        self.assertEqual(result["postcode"], "B92 9EJ")

    def test_geocode_venue_passes_address_through(self) -> None:
        def google(query, key):
            return ARMCO if "B92 9EJ" in query else DAMSON_PKWY_STREET

        with self._patch(google), mock.patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "k"}):
            loc = geocoding.geocode_venue("DAMSON PARK 3G Pitch 1D", address=PINNED)
        self.assertEqual(loc["postalCode"], "B92 9EJ")
        self.assertEqual(loc["feature"], "DAMSON PARK 3G Pitch 1D")


if __name__ == "__main__":
    unittest.main()
