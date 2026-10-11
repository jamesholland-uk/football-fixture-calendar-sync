#!/usr/bin/env python3
"""Venue name handling: the FA text, the search name, and the label parents see.

FA writes e.g. "THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A  - 09:30AM" (two spaces
before the dash). EXA1A is a pitch code: useful in the Spond label, noise when searching.
No network.
"""

import unittest

from geocoding import clean_venue_name, fixture_venue_name, to_spond_location


class FixtureVenueNameTest(unittest.TestCase):
    def test_strips_time_suffix_and_the_trailing_space_it_leaves(self) -> None:
        self.assertEqual(
            fixture_venue_name("THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A  - 09:30AM"),
            "THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A",
        )

    def test_single_space_before_dash(self) -> None:
        self.assertEqual(fixture_venue_name("EXAMPLE PARK 3G Pitch 1D - 09:30AM"), "EXAMPLE PARK 3G Pitch 1D")

    def test_no_time_suffix(self) -> None:
        self.assertEqual(fixture_venue_name("  SOME GROUND  "), "SOME GROUND")

    def test_hyphens_inside_the_name_survive(self) -> None:
        self.assertEqual(
            fixture_venue_name("SAMPLE-HALL ACADEMY ABCD Pitch 2 - 10:00AM"),
            "SAMPLE-HALL ACADEMY ABCD Pitch 2",
        )

    def test_empty(self) -> None:
        self.assertEqual(fixture_venue_name(""), "")


class CleanVenueNameTest(unittest.TestCase):
    def test_pitch_code_with_trailing_letter_is_deliberately_kept(self) -> None:
        # EXA1A = pitch 1A at the ground. Stripping it was tried and reverted:
        # the unstripped name already resolved this ground correctly in production, and
        # there was no evidence the stripped name does as well. Change only with evidence
        # (run the real pipeline with a Google key on the venue before and after).
        self.assertEqual(
            clean_venue_name("THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A"),
            "THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A",
        )

    def test_existing_behaviour_is_unchanged(self) -> None:
        self.assertEqual(clean_venue_name("EXAMPLE PARK 3G Pitch 1D"), "EXAMPLE PARK")
        self.assertEqual(clean_venue_name("SOME STADIUM CV2"), "SOME STADIUM")
        self.assertEqual(
            clean_venue_name("NORTH TOWN ACADEMY 3G Pitch 1A/B"), "NORTH TOWN ACADEMY"
        )
        self.assertEqual(
            clean_venue_name("SAMPLE-HALL ACADEMY ABCD Pitch 2"), "SAMPLE-HALL ACADEMY ABCD"
        )

    def test_real_names_and_postcodes_are_not_over_stripped(self) -> None:
        # a postcode must never be eaten as if it were a pitch code
        for name in (
            "EXAMPLE PARK, EXAMPLE PARK EXAMPLE PARKWAY, EXAMPLETON, EX1 1AA",
            "ROYAL OAK B13",  # single letter + digits is not a pitch code
            "VILLA PARK",
            "KINGS NORTON 4",
        ):
            self.assertEqual(clean_venue_name(name), name)


class SpondLabelKeepsPitchCodeTest(unittest.TestCase):
    def test_label_keeps_pitch_code_and_has_no_stray_space(self) -> None:
        venue = fixture_venue_name("THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A  - 09:30AM")
        loc = to_spond_location(
            {"lat": 50.2, "lon": -1.2, "display": "Ground, Church Rd, EX3 3CC", "postcode": "EX3 3CC"},
            venue,
        )
        self.assertEqual(loc["feature"], "THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A")
        self.assertEqual(loc["featureName"], "THE EXAMPLE ARENA AND COMMUNITY STADIUM EXA1A")


if __name__ == "__main__":
    unittest.main()
