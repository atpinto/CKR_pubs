"""Tests for social_posts. Run from the repository root:  python -m unittest discover -s scripts"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import social_posts as sp


def sydney(day: int, hour: int, minute: int = 0) -> datetime:
    # October 2026: Monday 5th, Friday 9th, Saturday 10th.
    return datetime(2026, 10, day, hour, minute, tzinfo=sp.SYDNEY)


class ScheduleTests(unittest.TestCase):
    def test_monday_run_starts_at_nine(self) -> None:
        earliest = sp.first_earliest(sydney(5, 7, 20), None)
        self.assertEqual(sp.next_slot(earliest), sydney(5, 9))

    def test_posts_are_three_hours_apart_and_skip_weekends(self) -> None:
        slot = sp.next_slot(sydney(9, 9))
        slots = []
        for _ in range(5):
            slots.append(slot)
            slot = sp.next_slot(slot + sp.POST_GAP)
        self.assertEqual(slots, [sydney(9, 9), sydney(9, 12), sydney(9, 15), sydney(12, 9), sydney(12, 12)])

    def test_waits_for_posts_already_queued(self) -> None:
        last = sydney(5, 15).astimezone(timezone.utc)
        earliest = sp.first_earliest(sydney(5, 7).astimezone(timezone.utc), last)
        self.assertEqual(sp.next_slot(earliest), sydney(6, 9))

    def test_lead_time_skips_an_imminent_slot(self) -> None:
        earliest = sp.first_earliest(sydney(5, 8, 50), None)
        self.assertEqual(sp.next_slot(earliest), sydney(5, 12))

    def test_slots_respect_daylight_saving(self) -> None:
        # Sydney moves to daylight time on Sunday 4 October 2026.
        self.assertEqual(sp.next_slot(sydney(5, 0)).utcoffset(), timedelta(hours=11))

    def test_start_date_is_midnight_in_sydney(self) -> None:
        self.assertEqual(sp.since_timestamp("2026-10-05"), "2026-10-05T00:00:00+11:00")
        # The Monday 5 October run added its paper at 22:50 UTC on Sunday 4 October.
        added = datetime(2026, 10, 4, 22, 50, tzinfo=timezone.utc)
        self.assertGreaterEqual(added, datetime.fromisoformat(sp.since_timestamp("2026-10-05")))

    def test_full_timestamp_is_kept(self) -> None:
        self.assertEqual(sp.since_timestamp("2026-10-05T00:00:00Z"), "2026-10-05T00:00:00+00:00")


class ComposeTests(unittest.TestCase):
    record = {
        "title": "Growing old before growing up.",
        "authors": ["Guha, Chandana", "Guha, Ria"],
        "journal": "Kidney international",
        "year": "2026",
        "doi": "10.1016/j.kint.2026.01.001",
        "pmid": "42760016",
    }

    def test_post_layout(self) -> None:
        text = sp.compose_post(self.record, "A short summary.", ["Nephrology", "PaediatricHealth"])
        self.assertIn("A short summary.", text)
        self.assertIn("Guha et al. · Kidney international (2026)", text)
        self.assertIn("Read the paper: https://doi.org/10.1016/j.kint.2026.01.001", text)
        self.assertTrue(text.endswith("#KidneyResearch #Nephrology #PaediatricHealth"))

    def test_pubmed_link_without_doi(self) -> None:
        record = {**self.record, "doi": "", "authors": ["Guha, Chandana"]}
        text = sp.compose_post(record, "Summary.", [])
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/42760016/", text)
        self.assertIn("Guha · Kidney", text)

    def test_hashtags_are_cleaned(self) -> None:
        line = sp.hashtag_line(["#PublicHealth", "publichealth", "Kidney Research", "KidneyResearch", "Ageing", "A", "Frailty", "Extra"])
        self.assertEqual(line, "#KidneyResearch #PublicHealth #Ageing #Frailty")

    def test_centre_tag_alone_when_none_suggested(self) -> None:
        self.assertEqual(sp.hashtag_line([]), "#KidneyResearch")


class XPostTests(unittest.TestCase):
    record = {"doi": "10.1016/j.healthplace.2026.103741", "pmid": "42800220"}

    def test_x_post_layout(self) -> None:
        text = sp.compose_x_post(self.record, "Few ageing studies measure housing quality.")
        self.assertEqual(
            text,
            "New CKR paper: Few ageing studies measure housing quality.\n\n"
            "https://doi.org/10.1016/j.healthplace.2026.103741\n\n#KidneyResearch",
        )

    def test_links_count_as_23_characters(self) -> None:
        self.assertEqual(sp.x_length("a https://doi.org/10.1016/j.healthplace.2026.103741"), 2 + 23)

    def test_long_sentence_is_shortened_to_fit(self) -> None:
        text = sp.compose_x_post(self.record, "word " * 80)
        self.assertLessEqual(sp.x_length(text), 280)
        self.assertIn("\u2026\n\nhttps://doi.org/", text)

    def test_services_get_their_own_text(self) -> None:
        record = {**self.record, "title": "T", "authors": ["Guha, C"], "journal": "J", "year": "2026"}
        written = {"summary": "Long summary.", "short_summary": "Short.", "hashtags": ["Ageing"]}
        self.assertTrue(sp.post_text("twitter", record, written).startswith("New CKR paper: Short."))
        self.assertIn("Long summary.", sp.post_text("facebook", record, written))
        self.assertEqual(sp.post_text("facebook", record, written), sp.post_text("linkedin", record, written))


class ChannelTests(unittest.TestCase):
    def test_new_channels_only(self) -> None:
        self.assertEqual(sp.channels_to_post({"li"}, ["li", "fb", "x"]), ["fb", "x"])

    def test_empty_channel_covers_all(self) -> None:
        self.assertEqual(sp.channels_to_post({""}, ["li", "fb"]), [])


if __name__ == "__main__":
    unittest.main()
