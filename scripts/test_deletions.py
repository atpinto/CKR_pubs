"""Tests for keeping deleted publications deleted. They make no network requests.

Run from the repository root:  python -m unittest discover -s scripts
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import sync_database as sd
import update_publications as up


def article(pmid: str) -> ET.Element:
    """A minimal PubMed record with a CKR Sydney affiliation."""
    return ET.fromstring(f"""
<PubmedArticle>
  <MedlineCitation>
    <PMID>{pmid}</PMID>
    <Article>
      <Journal><Title>Kidney Journal</Title><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>
      <ArticleTitle>Paper {pmid}</ArticleTitle>
      <AuthorList><Author><LastName>Smith</LastName><ForeName>Jo</ForeName>
        <AffiliationInfo><Affiliation>Centre for Kidney Research, Westmead, Sydney, Australia</Affiliation></AffiliationInfo>
      </Author></AuthorList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>""")


def fake_pubmed(search: list[str], fetched: list[str]) -> type:
    class FakePubMed:
        def search_ids(self) -> tuple[list[str], int]:
            return list(search), len(search)

        def fetch_articles(self, pmids: list[str]) -> list[ET.Element]:
            fetched.extend(pmids)
            return [article(pmid) for pmid in pmids]

    return FakePubMed


def db_record(record_id: str, pmid: str = "") -> dict[str, object]:
    """A publication as the Supabase API returns it (only the fields the tests need)."""
    return {"id": record_id, "pmid": pmid, "title": f"Stored {record_id}", "year": "2025", "journal": "J"}


def csv_ids(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row["id"] for row in csv.DictReader(handle)]


class FakeSupabase:
    """Stands in for SupabaseClient: the first fetch is the database at the start of the run, the
    second is the database just before the upsert."""

    def __init__(self, *snapshots: list[dict[str, object]], deleted: set[str] = frozenset()) -> None:
        self.snapshots = list(snapshots)
        self.deleted = set(deleted)
        self.upserted: list[dict[str, object]] | None = None
        self.has_scopus_columns = False

    def fetch_publications(self) -> list[dict[str, object]]:
        return self.snapshots.pop(0)

    def fetch_deleted_pmids(self) -> set[str]:
        return set(self.deleted)

    def export_columns(self) -> list[str]:
        return list(up.REQUIRED_COLUMNS)

    def upsert_publications(self, records: list[dict[str, object]]) -> None:
        self.upserted = records


def run_main(client: FakeSupabase, *args: str) -> tuple[int, str]:
    output = io.StringIO()
    with mock.patch.object(sd, "SupabaseClient", lambda: client), \
            mock.patch.object(sys, "argv", ["sync_database.py", *args]), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = sd.main()
    return code, output.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.csv = Path(self.directory.name) / "publications.csv"

    def test_deleted_pmids_are_not_added_back(self) -> None:
        sd.write_database_export(self.csv, [db_record("111", "111"), db_record("manual-1")])
        fetched: list[str] = []
        with mock.patch.object(up, "PubMedClient", fake_pubmed(["111", "222", "333"], fetched)):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = up.update(self.csv, {"222"})
        self.assertEqual(sorted(csv_ids(self.csv)), ["111", "333", "manual-1"])
        self.assertNotIn("222", fetched)
        self.assertEqual((summary["added"], summary["deleted_skipped"]), (1, 1))

    def test_record_in_database_is_refreshed_even_if_marked_deleted(self) -> None:
        # An editor added it back; the database copy wins over a leftover marker.
        sd.write_database_export(self.csv, [db_record("111", "111")])
        with mock.patch.object(up, "PubMedClient", fake_pubmed(["111"], [])):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = up.update(self.csv, {"111"})
        self.assertEqual(csv_ids(self.csv), ["111"])
        self.assertEqual(summary["deleted_skipped"], 0)

    def test_without_deletions_behaviour_is_unchanged(self) -> None:
        sd.write_database_export(self.csv, [db_record("111", "111")])
        with mock.patch.object(up, "PubMedClient", fake_pubmed(["111", "222"], [])):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = up.update(self.csv)
        self.assertEqual(sorted(csv_ids(self.csv)), ["111", "222"])
        self.assertEqual(summary["deleted_skipped"], 0)


class DeletedDuringRunTests(unittest.TestCase):
    def test_records_deleted_during_the_run_are_left_out(self) -> None:
        initial = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        latest = [{"id": "a"}, {"id": "c"}, {"id": "d"}]  # b deleted, d added by an editor
        refreshed = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "new-from-pubmed"}]
        kept, gone = sd.drop_deleted_during_run(refreshed, initial, latest)
        self.assertEqual([record["id"] for record in kept], ["a", "c", "new-from-pubmed"])
        self.assertEqual(gone, {"b"})


class FetchDeletedPmidsTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "secret"}
        with mock.patch.dict(os.environ, environment):
            self.client = sd.SupabaseClient()

    def test_reads_every_page(self) -> None:
        first = [{"pmid": str(n)} for n in range(sd.PAGE_SIZE)]
        with mock.patch.object(self.client, "request", side_effect=[first, [{"pmid": "x"}]]) as request:
            pmids = self.client.fetch_deleted_pmids()
        self.assertEqual(len(pmids), sd.PAGE_SIZE + 1)
        self.assertIn("/rest/v1/deleted_publications?", request.call_args_list[0].args[1])
        self.assertIn("offset=500", request.call_args_list[1].args[1])

    def test_missing_table_warns_and_returns_nothing(self) -> None:
        for body in (
            '{"code":"42P01","details":null,"hint":null,"message":"relation \\"public.deleted_publications\\" does not exist"}',
            '{"code":"PGRST205","details":null,"hint":null,"message":"Could not find the table '
            '\'public.deleted_publications\' in the schema cache"}',
        ):
            output = io.StringIO()
            error = RuntimeError(f"Supabase HTTP 404: {body}")
            with mock.patch.object(self.client, "request", side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(self.client.fetch_deleted_pmids(), set())
            self.assertIn("::warning::", output.getvalue())

    def test_other_errors_stop_the_run(self) -> None:
        with mock.patch.object(self.client, "request", side_effect=RuntimeError("Supabase HTTP 500: boom")):
            with self.assertRaises(RuntimeError):
                self.client.fetch_deleted_pmids()


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.csv = Path(self.directory.name) / "publications.csv"

    def test_weekly_update_keeps_deletions(self) -> None:
        initial = [db_record("111", "111"), db_record("444", "444"), db_record("manual-1")]
        latest = [db_record("111", "111"), db_record("manual-1")]  # 444 deleted while the run was going
        client = FakeSupabase(initial, latest, deleted={"222"})  # 222 deleted last week
        fetched: list[str] = []
        with mock.patch.object(up, "PubMedClient", fake_pubmed(["111", "222", "333", "444"], fetched)):
            code, output = run_main(client, "--csv", str(self.csv), "--no-citations")
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(str(record["id"]) for record in client.upserted), ["111", "333", "manual-1"])
        self.assertEqual(sorted(csv_ids(self.csv)), ["111", "333", "manual-1"])
        self.assertNotIn("222", fetched)
        self.assertIn("Left out 1 records that an editor deleted during this run.", output)
        self.assertIn("1 deleted by an editor and not added back.", output)

    def test_seed_skips_deleted_records(self) -> None:
        sd.write_database_export(self.csv, [db_record("111", "111"), db_record("222", "222"), db_record("manual-1")])
        client = FakeSupabase(deleted={"222"})
        code, output = run_main(client, "--csv", str(self.csv), "--seed")
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(str(record["id"]) for record in client.upserted), ["111", "manual-1"])
        self.assertIn("Skipped 1 records that an editor deleted.", output)


if __name__ == "__main__":
    unittest.main()
