#!/usr/bin/env python3
"""Pinned venues prefer the most precise result in the pinned postcode, and long
addresses are shortened at a comma boundary (keeping the postcode), not cut mid-word.

Regression: a pinned school address geocoded to a street-level point (Google
location_type GEOMETRIC_CENTER) was accepted straight away, although Places knew the
school itself in the same postcode. No network is used.

    python3 -m unittest test_geocode_precision
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import geocoding

PINNED = "EXAMPLE ACADEMY ASHBY ROAD, EXAMPLETON, EX1 1AA"


def google(location_type: str, postcode: str = "EX1 1AA", lat: float = 50.10) -> dict:
    return {
        "lat": lat,
        "lon": -1.10,
        "display": f"Ashby Rd, Exampleton {postcode}, UK",
        "postcode": postcode,
        "source": "google",
        "location_type": location_type,
    }


def places(postcode: str = "EX1 1AA") -> dict:
    return {
        "lat": 50.11,
        "lon": -1.11,
        "display": f"Example Academy, Ashby Rd, Exampleton {postcode}, UK",
        "postcode": postcode,
        "source": "google_places",
    }


class PinnedPrecisionTest(unittest.TestCase):
    def _run(self, google_result, places_result):
        self.places = mock.Mock(return_value=places_result)
        with mock.patch.multiple(
            geocoding,
            geocode_google=mock.Mock(return_value=google_result),
            geocode_google_places=self.places,
            geocode_postcode_io=mock.Mock(
                return_value={"lat": 1, "lon": 1, "display": "x", "postcode": "EX1 1AA", "source": "postcode.io"}
            ),
        ), redirect_stdout(io.StringIO()):
            return geocoding.geocode_pinned("EXAMPLE ACADEMY QEMS Pitch 2", PINNED, "k")

    def test_exact_google_result_is_used_without_asking_places(self) -> None:
        result, reason = self._run(google("ROOFTOP"), places())
        self.assertEqual(result["source"], "google")
        self.places.assert_not_called()
        self.assertIn("Google confirmed", reason)

    def test_street_level_google_result_is_upgraded_to_the_named_place(self) -> None:
        result, reason = self._run(google("GEOMETRIC_CENTER"), places())
        self.assertEqual(result["source"], "google_places")
        self.assertEqual(result["lat"], 50.11)
        self.assertIn("Places confirmed", reason)

    def test_places_query_is_the_name_without_pitch_suffix_plus_postcode(self) -> None:
        self._run(google("GEOMETRIC_CENTER"), places())
        self.places.assert_called_once_with("EXAMPLE ACADEMY QEMS, EX1 1AA, UK", "k")  # pitch suffix dropped

    def test_places_in_a_different_postcode_is_not_used(self) -> None:
        result, reason = self._run(google("GEOMETRIC_CENTER"), places(postcode="EX9 9ZZ"))
        self.assertEqual(result["source"], "google")  # street level, but in the right postcode
        self.assertIn("street level", reason)

    def test_no_places_result_keeps_the_street_level_google_pin(self) -> None:
        result, reason = self._run(google("RANGE_INTERPOLATED"), None)
        self.assertEqual(result["source"], "google")
        self.assertIn("street level", reason)

    def test_result_with_no_precision_info_is_treated_as_not_exact(self) -> None:
        g = google("")
        g.pop("location_type")
        result, _ = self._run(g, places())
        self.assertEqual(result["source"], "google_places")

    def test_google_in_the_wrong_postcode_still_loses_to_places(self) -> None:
        result, _ = self._run(google("ROOFTOP", postcode="EX2 2BB"), places())
        self.assertEqual(result["source"], "google_places")


class GeocodeGoogleReportsPrecisionTest(unittest.TestCase):
    def test_location_type_is_returned(self) -> None:
        payload = {
            "status": "OK",
            "results": [
                {
                    "geometry": {"location": {"lat": 50.1, "lng": -1.1}, "location_type": "GEOMETRIC_CENTER"},
                    "formatted_address": "Ashby Rd, Exampleton EX1 1AA, UK",
                    "address_components": [{"types": ["postal_code"], "long_name": "EX1 1AA"}],
                }
            ],
        }
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = geocoding.geocode_google("q", "k")
        self.assertEqual(result["location_type"], "GEOMETRIC_CENTER")


class ShortenAddressTest(unittest.TestCase):
    LONG = (
        "The Example Arena & Community Stadium, Example Rd, The Royal Town of Exampleton, "
        "Birmingham, Exampleton EX1 1AA, UK"
    )

    def test_short_address_is_unchanged(self) -> None:
        self.assertEqual(
            geocoding.shorten_address("395 Example Rd, Exampleton EX1 1AA, UK"),
            "395 Example Rd, Exampleton EX1 1AA, UK",
        )

    def test_long_address_drops_middle_parts_and_keeps_the_postcode(self) -> None:
        out = geocoding.shorten_address(self.LONG)
        self.assertEqual(out, "The Example Arena & Community Stadium, Example Rd, Exampleton EX1 1AA")
        self.assertLessEqual(len(out), 80)
        self.assertNotIn("UK", out)

    def test_postcode_segment_is_kept_even_when_the_name_is_long(self) -> None:
        out = geocoding.shorten_address(
            "An Extremely Long Example Community Sports And Leisure Stadium Complex, Rd, Exampleton EX1 1AA, UK"
        )
        self.assertTrue(out.endswith("Exampleton EX1 1AA"))
        self.assertLessEqual(len(out), 80)

    def test_without_a_postcode_it_cuts_at_a_comma_not_mid_word(self) -> None:
        text = (
            "Alpha Beta Gamma Delta Epsilon, Zeta Eta Theta Iota Kappa, "
            "Lambda Mu Nu Xi Omicron Pi Rho Sigma, Tau"
        )
        out = geocoding.shorten_address(text)
        self.assertEqual(out, "Alpha Beta Gamma Delta Epsilon, Zeta Eta Theta Iota Kappa")
        self.assertLessEqual(len(out), 80)

    def test_a_single_huge_part_is_hard_cut_as_a_last_resort(self) -> None:
        self.assertEqual(len(geocoding.shorten_address("x" * 200)), 80)

    def test_never_exceeds_the_limit(self) -> None:
        for text in (self.LONG, "a, " * 60, "EX1 1AA " + "word " * 40):
            self.assertLessEqual(len(geocoding.shorten_address(text)), 80, text)


if __name__ == "__main__":
    unittest.main()
