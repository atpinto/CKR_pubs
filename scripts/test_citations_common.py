"""Tests for citations_common. Run from the repository root:  python -m unittest discover -s scripts"""

from __future__ import annotations

import unittest
from datetime import timezone

import citations_common as cc


class HelperTests(unittest.TestCase):
    def test_timestamp_round_trip(self) -> None:
        stamp = cc.utc_timestamp()
        self.assertTrue(stamp.endswith("Z"))
        self.assertEqual(cc.parse_timestamp(stamp).tzinfo, timezone.utc)

    def test_invalid_timestamps_sort_first(self) -> None:
        early = cc.parse_timestamp("")
        self.assertLess(early, cc.parse_timestamp("2020-01-01T00:00:00Z"))
        self.assertEqual(cc.parse_timestamp("garbage"), early)
        self.assertEqual(cc.parse_timestamp("2020-01-01T00:00:00").tzinfo, timezone.utc)

    def test_number_setting(self) -> None:
        self.assertEqual(cc.number_setting({}, "X", 5, 0), 5)
        self.assertEqual(cc.number_setting({"X": " 7 "}, "X", 5, 0), 7)
        with self.assertRaises(ValueError):
            cc.number_setting({"X": "abc"}, "X", 5, 0)
        with self.assertRaises(ValueError):
            cc.number_setting({"X": "-1"}, "X", 5, 0)


if __name__ == "__main__":
    unittest.main()
