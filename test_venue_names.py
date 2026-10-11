#!/usr/bin/env python3
"""Venue name handling: the FA text, the search name, and the label parents see.

FA writes e.g. "THE AMBER ARENA AND COMMUNITY STADIUM BSM1A  - 09:30AM" (two spaces
before the dash). BSM1A is a pitch code: useful in the Spond label, noise when searching.
No network.
"""

import unittest

from geocoding import clean_venue_name, fixture_venue_name, to_spond_location


class FixtureVenueNameTest(unittest.TestCase):
    def test_strips_time_suffix_and_the_trailing_space_it_leaves(self) -> None:
        self.assertEqual(
            fixture_venue_name("THE AMBER ARENA AND COMMUNITY STADIUM BSM1A  - 09:30AM"),
            "THE AMBER ARENA AND COMMUNITY STADIUM BSM1A",
        )

    def test_single_space_before_dash(self) -> None:
        self.assertEqual(fixture_venue_name("DAMSON PARK 3G Pitch 1D - 09:30AM"), "DAMSON PARK 3G Pitch 1D")

    def test_no_time_suffix(self) -> None:
        self.assertEqual(fixture_venue_name("  SOME GROUND  "), "SOME GROUND")

    def test_hyphens_inside_the_name_survive(self) -> None:
        self.assertEqual(
            fixture_venue_name("LANDAU-FORTE ACADEMY QEMS Pitch 2 - 10:00AM"),
            "LANDAU-FORTE ACADEMY QEMS Pitch 2",
        )

    def test_empty(self) -> None:
        self.assertEqual(fixture_venue_name(""), "")


class CleanVenueNameTest(unittest.TestCase):
    def test_pitch_code_with_trailing_letter_is_deliberately_kept(self) -> None:
        # BSM1A = Boldmere St Michaels pitch 1A. Stripping it was tried and reverted:
        # the unstripped name already resolved this ground correctly in production, and
        # there was no evidence the stripped name does as well. Change only with evidence
        # (run the real pipeline with a Google key on the venue before and after).
        self.assertEqual(
            clean_venue_name("THE AMBER ARENA AND COMMUNITY STADIUM BSM1A"),
            "THE AMBER ARENA AND COMMUNITY STADIUM BSM1A",
        )

    def test_existing_behaviour_is_unchanged(self) -> None:
        self.assertEqual(clean_venue_name("DAMSON PARK 3G Pitch 1D"), "DAMSON PARK")
        self.assertEqual(clean_venue_name("SOME STADIUM CV2"), "SOME STADIUM")
        self.assertEqual(
            clean_venue_name("NORTH BIRMINGHAM ACADEMY 3G Pitch 1A/B"), "NORTH BIRMINGHAM ACADEMY"
        )
        self.assertEqual(
            clean_venue_name("LANDAU-FORTE ACADEMY QEMS Pitch 2"), "LANDAU-FORTE ACADEMY QEMS"
        )

    def test_real_names_and_postcodes_are_not_over_stripped(self) -> None:
        # a postcode must never be eaten as if it were a pitch code
        for name in (
            "DAMSON PARK, DAMSON PARK DAMSON PARKWAY, SOLIHULL, B92 9EJ",
            "ROYAL OAK B13",  # single letter + digits is not a pitch code
            "VILLA PARK",
            "KINGS NORTON 4",
        ):
            self.assertEqual(clean_venue_name(name), name)


class SpondLabelKeepsPitchCodeTest(unittest.TestCase):
    def test_label_keeps_pitch_code_and_has_no_stray_space(self) -> None:
        venue = fixture_venue_name("THE AMBER ARENA AND COMMUNITY STADIUM BSM1A  - 09:30AM")
        loc = to_spond_location(
            {"lat": 52.5, "lon": -1.8, "display": "Ground, Church Rd, B73 5RY", "postcode": "B73 5RY"},
            venue,
        )
        self.assertEqual(loc["feature"], "THE AMBER ARENA AND COMMUNITY STADIUM BSM1A")
        self.assertEqual(loc["featureName"], "THE AMBER ARENA AND COMMUNITY STADIUM BSM1A")


if __name__ == "__main__":
    unittest.main()
