"""Tests for scopus_citations. They use saved sample responses and make no network requests.

Run from the repository root:  python -m unittest discover -s scripts
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

import scopus_citations as sc
from scholar_citations import CitationBlocked, CitationLookupError


def entry(doi: str = "", pmid: str = "", count: object = 5, url: str = "https://www.scopus.com/inward/citedby.uri?x=1") -> dict:
    data: dict = {"citedby-count": str(count), "link": [{"@ref": "self", "@href": "https://api.elsevier.com/x"},
                                                         {"@ref": "scopus-citedby", "@href": url}]}
    if doi:
        data["prism:doi"] = doi
    if pmid:
        data["pubmed-id"] = pmid
    return data


def response(*entries: dict) -> dict:
    return {"search-results": {"entry": list(entries)}}


class FakeResponse:
    def __init__(self, data: object) -> None:
        self._body = json.dumps(data).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "e", {}, io.BytesIO(body))  # type: ignore[arg-type]


def row(id_: str, doi: str = "", pmid: str = "", year: str = "2020", checked: str = "") -> dict:
    return {"id": id_, "doi": doi, "pmid": pmid, "year": year, "scopus_citations_checked_at": checked}


class QueryTermTests(unittest.TestCase):
    def test_doi_preferred(self) -> None:
        self.assertEqual(sc.query_term(row("a", "10.1/abc", "12")), ("DOI", "10.1/abc"))

    def test_unsafe_doi_falls_back_to_pmid(self) -> None:
        self.assertEqual(sc.query_term(row("a", "10.1016/S0140(20)1", "12")), ("PMID", "12"))

    def test_none_when_nothing_usable(self) -> None:
        self.assertIsNone(sc.query_term(row("a", "", "")))


class ParseTests(unittest.TestCase):
    def test_indexes_and_lowercases(self) -> None:
        by_doi, by_pmid = sc.parse_entries(response(entry("10.1/ABC", "7", 9)))
        self.assertEqual(by_doi["10.1/abc"][0], 9)
        self.assertEqual(by_pmid["7"][0], 9)

    def test_error_entry_and_bad_count_skipped(self) -> None:
        by_doi, _ = sc.parse_entries(response({"error": "Result set was empty"}, entry("10.1/a", count="x")))
        self.assertEqual(by_doi, {})

    def test_foreign_url_dropped_and_max_kept(self) -> None:
        by_doi, _ = sc.parse_entries(response(entry("10.1/a", count=2, url="https://evil.example/x"), entry("10.1/a", count=4)))
        self.assertEqual(by_doi["10.1/a"][0], 4)
        self.assertTrue(by_doi["10.1/a"][1].startswith("https://www.scopus.com/"))
        by_doi, _ = sc.parse_entries(response(entry("10.1/b", url="https://evil.example/x")))
        self.assertEqual(by_doi["10.1/b"][1], "")

    def test_garbage(self) -> None:
        self.assertEqual(sc.parse_entries("nope"), ({}, {}))


class ClientTests(unittest.TestCase):
    def lookup(self, result: object, rows=None, **kw):
        client = sc.ScopusClient("KEY", **kw)
        seen = {}

        def opener(request, timeout=0):
            seen["request"] = request
            if isinstance(result, Exception):
                raise result
            return FakeResponse(result)

        with mock.patch("urllib.request.urlopen", opener):
            out = client.lookup(rows or [row("a", "10.1/a", "1"), row("b", "", "22")])
        return out, seen["request"]

    def test_matches_exact_ids_only(self) -> None:
        out, request = self.lookup(response(entry("10.1/A", "1", 3), entry("10.9/other", "99", 50)))
        self.assertEqual(out["a"][0], 3)
        self.assertIsNone(out["b"])
        self.assertEqual(request.get_header("X-els-apikey"), "KEY")
        self.assertIsNone(request.get_header("X-els-insttoken"))
        self.assertIn("DOI%2810.1%2Fa%29", request.full_url)
        self.assertIn("PMID%2822%29", request.full_url)

    def test_insttoken_header(self) -> None:
        _, request = self.lookup(response(), insttoken="T")
        self.assertEqual(request.get_header("X-els-insttoken"), "T")

    def test_auth_and_quota_block(self) -> None:
        for code in (401, 403, 429):
            with self.assertRaises(CitationBlocked):
                self.lookup(http_error(code))

    def test_other_errors(self) -> None:
        with self.assertRaises(CitationLookupError):
            self.lookup(http_error(500))
        with self.assertRaises(CitationLookupError):
            self.lookup(urllib.error.URLError("down"))

    def test_make_client(self) -> None:
        self.assertIsNone(sc.make_client({}))
        client = sc.make_client({"SCOPUS_API_KEY": " k ", "SCOPUS_INSTTOKEN": "t"})
        self.assertEqual((client.api_key, client.insttoken), ("k", "t"))


class FakeClient:
    name = "Scopus"

    def __init__(self, behaviour) -> None:
        self.behaviour, self.calls = behaviour, []

    def pause(self) -> float:
        return 0

    def lookup(self, rows):
        self.calls.append([r["id"] for r in rows])
        return self.behaviour(rows)


class RefreshTests(unittest.TestCase):
    def run_refresh(self, rows, client, **kw):
        applied = []
        stats = sc.refresh_scopus(rows, client, max_lookups=kw.pop("max_lookups", 100), time_limit_seconds=1e9,
                                  apply=lambda r, c, u, t: applied.append((r["id"], c, u)),
                                  sleep=lambda s: None, **kw)
        return stats, applied

    def test_priority_never_checked_newest_first(self) -> None:
        rows = [row("old", "10.1/o", year="2010"), row("new", "10.1/n", year="2024"),
                row("done", "10.1/d", year="2025", checked="2026-01-01T00:00:00Z"), row("nothing")]
        client = FakeClient(lambda batch: {str(r["id"]): (1, "https://www.scopus.com/u") for r in batch})
        stats, applied = self.run_refresh(rows, client)
        self.assertEqual([a[0] for a in applied], ["new", "old", "done"])
        self.assertEqual(stats["counted"], 3)

    def test_not_found_applies_none(self) -> None:
        stats, applied = self.run_refresh([row("a", "10.1/a")], FakeClient(lambda b: {"a": None}))
        self.assertEqual(applied, [("a", None, "")])
        self.assertEqual(stats["not_found"], 1)

    def test_batches_and_limit(self) -> None:
        rows = [row(str(i), f"10.1/{i}") for i in range(30)]
        client = FakeClient(lambda b: {str(r["id"]): (1, "") for r in b})
        stats, _ = self.run_refresh(rows, client, batch_size=10, max_lookups=25)
        self.assertEqual([len(c) for c in client.calls], [10, 10, 5])
        self.assertEqual(stats["looked_up"], 25)

    def test_blocked_stops_without_applying(self) -> None:
        def blocked(batch):
            raise CitationBlocked("no")
        stats, applied = self.run_refresh([row("a", "10.1/a")], FakeClient(blocked))
        self.assertEqual(applied, [])
        self.assertEqual(stats["stopped"], "no")

    def test_repeated_failures_stop(self) -> None:
        def fail(batch):
            raise CitationLookupError("x")
        client = FakeClient(fail)
        stats, applied = self.run_refresh([row(str(i), f"10.1/{i}") for i in range(10)], client, batch_size=1)
        self.assertEqual(applied, [])
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(stats["stopped"])


if __name__ == "__main__":
    unittest.main()
