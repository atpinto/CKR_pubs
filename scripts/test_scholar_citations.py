"""Tests for scholar_citations. They use saved sample responses and make no network requests.

Run from the repository root:  python -m unittest discover -s scripts
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

import scholar_citations as sc


SCHOLAR_PAGE = """
<html><body><div id="gs_res_ccl_mid">
<div class="gs_r gs_or gs_scl"><div class="gs_ri">
  <h3 class="gs_rt"><span class="gs_ctu"><span class="gs_ct1">[HTML]</span><span class="gs_ct2">[HTML]</span></span>
    <a href="https://example.org/a">Data collection on housing conditions in ageing cohort studies: a meta-research study</a></h3>
  <div class="gs_a">S Khalatbari-Soltani - Health &amp; place, 2026 - Elsevier</div>
  <div class="gs_fl gs_flb"><a href="#" class="gs_or_sav">Save</a>
    <a href="/scholar?cites=123&amp;as_sdt=5">Cited by 1,234</a>
    <a href="/scholar?q=related:abc">Related articles</a> <a href="/scholar?cluster=9">All 3 versions</a></div>
</div></div>
<div class="gs_r gs_or gs_scl"><div class="gs_ri">
  <h3 class="gs_rt"><span class="gs_ctc"><span class="gs_ct1">[CITATION]</span><span class="gs_ct2">[C]</span></span> A citation only entry</h3>
  <div class="gs_fl"><a href="/scholar?cites=77">Cited by 7</a></div>
</div></div>
<div class="gs_r gs_or gs_scl"><div class="gs_ri">
  <h3 class="gs_rt"><a href="https://example.org/c">Never cited paper</a></h3>
  <div class="gs_fl"><a href="/scholar?q=related:zzz">Related articles</a></div>
</div></div>
</div></body></html>
"""
CAPTCHA_PAGE = "<html><body><div id='gs_captcha_ccl'>Our systems have detected unusual traffic</div></body></html>"
EMPTY_PAGE = "<html><body><p>Your search - xyz - did not match any articles.</p></body></html>"
TITLE = "Data collection on housing conditions in ageing cohort studies: a meta-research study."


class FakeResponse:
    def __init__(self, body: str, url: str = "https://scholar.google.com/scholar?q=x") -> None:
        self._body, self._url = body.encode("utf-8"), url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.org", code, "error", {}, io.BytesIO(b""))  # type: ignore[arg-type]


def patched_urlopen(result: object):
    def opener(request: object, timeout: float = 0) -> FakeResponse:
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FakeResponse)
        return result

    return mock.patch("scholar_citations.urllib.request.urlopen", opener)


class TitleMatching(unittest.TestCase):
    def test_ignores_case_punctuation_accents_and_trailing_period(self) -> None:
        self.assertTrue(sc.titles_match("Café culture: a Study.", "cafe culture - a study"))

    def test_rejects_a_different_title(self) -> None:
        self.assertFalse(sc.titles_match("Dialysis outcomes in children", "Dialysis outcomes in adults"))

    def test_accepts_scholar_truncated_titles(self) -> None:
        self.assertTrue(sc.titles_match(TITLE, "Data collection on housing conditions in ageing cohort stud…"))
        self.assertFalse(sc.titles_match(TITLE, "Data collection …"))

    def test_empty_titles_never_match(self) -> None:
        self.assertFalse(sc.titles_match("", ""))
        self.assertFalse(sc.titles_match("北京", "北京"))


class ScholarParsing(unittest.TestCase):
    def test_extracts_titles_and_counts(self) -> None:
        results = sc.parse_scholar_results(SCHOLAR_PAGE)
        self.assertEqual(
            results,
            [
                ("Data collection on housing conditions in ageing cohort studies: a meta-research study", 1234),
                ("A citation only entry", 7),
                ("Never cited paper", 0),
            ],
        )


class ScholarProvider(unittest.TestCase):
    provider = sc.ScholarProvider(delay=1)

    def test_returns_the_matching_count(self) -> None:
        with patched_urlopen(FakeResponse(SCHOLAR_PAGE)):
            self.assertEqual(self.provider.lookup(TITLE), 1234)

    def test_match_without_cited_by_link_is_zero(self) -> None:
        with patched_urlopen(FakeResponse(SCHOLAR_PAGE)):
            self.assertEqual(self.provider.lookup("Never cited paper."), 0)

    def test_no_matching_title_is_not_found(self) -> None:
        with patched_urlopen(FakeResponse(SCHOLAR_PAGE)):
            self.assertIsNone(self.provider.lookup("A completely different title about kidneys"))

    def test_page_with_no_results_is_not_found(self) -> None:
        with patched_urlopen(FakeResponse(EMPTY_PAGE)):
            self.assertIsNone(self.provider.lookup(TITLE))

    def test_captcha_page_blocks(self) -> None:
        with patched_urlopen(FakeResponse(CAPTCHA_PAGE)), self.assertRaises(sc.CitationBlocked):
            self.provider.lookup(TITLE)

    def test_sorry_redirect_blocks(self) -> None:
        with patched_urlopen(FakeResponse("", "https://www.google.com/sorry/index?continue=x")):
            with self.assertRaises(sc.CitationBlocked):
                self.provider.lookup(TITLE)

    def test_rate_limit_blocks(self) -> None:
        with patched_urlopen(http_error(429)), self.assertRaises(sc.CitationBlocked):
            self.provider.lookup(TITLE)

    def test_unrecognised_page_is_a_failed_lookup_not_a_missing_count(self) -> None:
        with patched_urlopen(FakeResponse("<html><body>Before you continue to Google</body></html>")):
            with self.assertRaises(sc.CitationLookupError):
                self.provider.lookup(TITLE)

    def test_network_error_is_a_failed_lookup(self) -> None:
        with patched_urlopen(urllib.error.URLError("offline")), self.assertRaises(sc.CitationLookupError):
            self.provider.lookup(TITLE)


def serp_response(payload: dict) -> FakeResponse:
    return FakeResponse(json.dumps(payload), "https://serpapi.com/search.json")


class SerpApiProvider(unittest.TestCase):
    provider = sc.SerpApiProvider("key", delay=0)

    def test_reads_cited_by_total(self) -> None:
        payload = {"organic_results": [
            {"title": "Another paper", "inline_links": {"cited_by": {"total": 3}}},
            {"title": TITLE, "inline_links": {"cited_by": {"total": 42, "cites_id": "1"}}},
        ]}
        with patched_urlopen(serp_response(payload)):
            self.assertEqual(self.provider.lookup(TITLE), 42)

    def test_match_without_cited_by_is_zero(self) -> None:
        with patched_urlopen(serp_response({"organic_results": [{"title": TITLE, "inline_links": {}}]})):
            self.assertEqual(self.provider.lookup(TITLE), 0)

    def test_no_results_message_is_not_found(self) -> None:
        with patched_urlopen(serp_response({"error": "Google hasn't returned any results for this query."})):
            self.assertIsNone(self.provider.lookup(TITLE))

    def test_account_errors_stop_the_run(self) -> None:
        with patched_urlopen(serp_response({"error": "Invalid API key."})), self.assertRaises(sc.CitationBlocked):
            self.provider.lookup(TITLE)
        with patched_urlopen(http_error(429)), self.assertRaises(sc.CitationBlocked):
            self.provider.lookup(TITLE)

    def test_bad_json_is_a_failed_lookup(self) -> None:
        with patched_urlopen(FakeResponse("not json")), self.assertRaises(sc.CitationLookupError):
            self.provider.lookup(TITLE)


class FakeProvider:
    name = "fake"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.titles: list[str] = []

    def pause(self) -> float:
        return 0

    def lookup(self, title: str) -> int | None:
        self.titles.append(title)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def row(identifier: str, year: str, checked: str = "", citations: str = "") -> dict[str, str]:
    return {"id": identifier, "title": f"Title {identifier}", "year": year,
            "citations": citations, "citations_checked_at": checked}


def run(rows: list[dict[str, str]], provider: FakeProvider, **options: object):
    applied: list[tuple[str, int | None]] = []
    settings = {"max_lookups": 100, "time_limit_seconds": 3600, "sleep": lambda _s: None}
    settings.update(options)
    stats = sc.refresh_citations(rows, provider, apply=lambda r, c, _t: applied.append((r["id"], c)), **settings)  # type: ignore[arg-type]
    return stats, applied


class Refresh(unittest.TestCase):
    def test_priority_is_never_checked_newest_first_then_oldest_check(self) -> None:
        rows = [
            row("old-checked", "2020", "2026-01-01T00:00:00Z", "5"),
            row("new-unchecked", "2026"),
            row("recent-checked", "2019", "2026-09-01T00:00:00+00:00", "9"),
            row("old-unchecked", "2015"),
        ]
        provider = FakeProvider([1, 2, 3, 4])
        _, applied = run(rows, provider)
        self.assertEqual([item for item, _ in applied], ["new-unchecked", "old-unchecked", "old-checked", "recent-checked"])

    def test_max_lookups_limits_the_run(self) -> None:
        provider = FakeProvider([1, 2])
        stats, applied = run([row(str(n), "2020") for n in range(5)], provider, max_lookups=2)
        self.assertEqual(len(applied), 2)
        self.assertEqual(stats["planned"], 2)

    def test_not_found_is_applied_as_none(self) -> None:
        stats, applied = run([row("a", "2020")], FakeProvider([None]))
        self.assertEqual(applied, [("a", None)])
        self.assertEqual((stats["not_found"], stats["counted"]), (1, 0))

    def test_block_stops_immediately_and_applies_nothing_further(self) -> None:
        provider = FakeProvider([5, sc.CitationBlocked("captcha"), 7])
        stats, applied = run([row(str(n), "2020") for n in range(3)], provider)
        self.assertEqual(len(applied), 1)
        self.assertEqual(stats["stopped"], "captcha")
        self.assertEqual(len(provider.titles), 2)

    def test_three_failures_in_a_row_stop_the_run(self) -> None:
        failure = sc.CitationLookupError("boom")
        stats, applied = run([row(str(n), "2020") for n in range(6)], FakeProvider([failure] * 3 + [1, 1, 1]))
        self.assertEqual(applied, [])
        self.assertIn("3 lookups failed in a row", str(stats["stopped"]))

    def test_a_success_resets_the_failure_streak(self) -> None:
        failure = sc.CitationLookupError("boom")
        stats, applied = run([row(str(n), "2020") for n in range(5)], FakeProvider([failure, failure, 1, failure, failure]))
        self.assertEqual(len(applied), 1)
        self.assertEqual(stats["stopped"], "")
        self.assertEqual(stats["failed"], 4)

    def test_time_limit_stops_the_run(self) -> None:
        ticks = iter([0, 0, 10, 10, 20, 20, 30, 30, 40, 40])
        stats, applied = run([row(str(n), "2020") for n in range(5)], FakeProvider([1] * 5),
                             time_limit_seconds=15, clock=lambda: next(ticks))
        self.assertEqual(stats["stopped"], "time limit reached")
        self.assertLess(len(applied), 5)

    def test_rows_without_a_title_are_skipped(self) -> None:
        blank = row("blank", "2020")
        blank["title"] = "  "
        _, applied = run([blank, row("ok", "2020")], FakeProvider([1]))
        self.assertEqual(applied, [("ok", 1)])


class Settings(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        self.assertEqual(sc.number_setting({}, "X", 100, 0), 100)
        self.assertEqual(sc.number_setting({"X": " 25 "}, "X", 100, 0), 25)

    def test_rejects_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            sc.number_setting({"X": "many"}, "X", 100, 0)
        with self.assertRaises(ValueError):
            sc.number_setting({"X": "-1"}, "X", 100, 0)

    def test_provider_choice_follows_the_api_key(self) -> None:
        self.assertIsInstance(sc.make_provider({}), sc.ScholarProvider)
        self.assertIsInstance(sc.make_provider({"SERPAPI_API_KEY": "abc"}), sc.SerpApiProvider)

    def test_timestamps_sort_blank_first_and_mix_formats(self) -> None:
        self.assertLess(sc.parse_timestamp(""), sc.parse_timestamp("2026-01-01T00:00:00Z"))
        self.assertEqual(sc.parse_timestamp("2026-01-01T00:00:00Z"), sc.parse_timestamp("2026-01-01T00:00:00+00:00"))
        self.assertEqual(sc.parse_timestamp("2026-01-01T00:00:00Z"), datetime(2026, 1, 1, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
