#!/usr/bin/env python3
"""Unit tests for SPOND_HOST_IDS parsing (multiple hosts per team)."""

import unittest

from spond_hosts import parse_spond_host_slots


class ParseSpondHostSlotsTest(unittest.TestCase):
    def test_multiple_hosts_per_team(self) -> None:
        self.assertEqual(
            parse_spond_host_slots("idA|idB,idC|idD"),
            [["idA", "idB"], ["idC", "idD"]],
        )

    def test_mixed_single_and_multiple(self) -> None:
        self.assertEqual(
            parse_spond_host_slots("idA|idB,idC"),
            [["idA", "idB"], ["idC"]],
        )

    def test_one_host_per_team_still_works(self) -> None:
        self.assertEqual(
            parse_spond_host_slots("idA,idB"),
            [["idA"], ["idB"]],
        )

    def test_three_hosts_on_one_team(self) -> None:
        self.assertEqual(
            parse_spond_host_slots("idA|idB|idC"),
            [["idA", "idB", "idC"]],
        )

    def test_empty_and_blank_slots(self) -> None:
        self.assertEqual(parse_spond_host_slots(""), [[]])
        self.assertEqual(parse_spond_host_slots(",idC"), [[], ["idC"]])
        self.assertEqual(parse_spond_host_slots("idA|idB,"), [["idA", "idB"], []])

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            parse_spond_host_slots(" idA | idB , idC "),
            [["idA", "idB"], ["idC"]],
        )


if __name__ == "__main__":
    unittest.main()
