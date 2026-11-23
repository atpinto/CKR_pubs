"""Removed publications stay removed across the weekly PubMed update."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_database as sd
import update_publications as up


def article(pmid: str) -> dict[str, object]:
    return {
        "id": pmid, "pmid": pmid, "title": f"Paper {pmid}", "authors": [], "year": "2026",
        "journal": "", "abbreviation": "", "volume": "", "issue": "", "start_page": "",
        "end_page": "", "doi": "", "pmcid": "", "issn": "", "language": "", "affiliations": [],
        "publication_types": [], "publication_date": "", "electronic_dates": [], "corrections": [],
        "projects": [], "programmes": [], "project_evidence": {}, "programme_evidence": {},
        "projects_source": "suggested", "programmes_source": "suggested", "source_keywords": [],
        "affiliation_review": False, "record_source": "PubMed", "_has_ckr_affiliation": True,
    }


class UpdateSkipsExcluded(unittest.TestCase):
    def run_update(self, existing: list[str], search: list[str], excluded: set[str]) -> tuple[list[str], dict]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "publications.csv"
            rows = [{**article(p), "_has_ckr_affiliation": None} for p in existing]
            sd.write_database_export(path, rows)
            fake = mock.Mock()
            fake.search_ids.return_value = (search, len(search))
            fake.fetch_articles.side_effect = lambda batch: batch
            with mock.patch.object(up, "PubMedClient", return_value=fake), \
                 mock.patch.object(up, "parse_article", side_effect=article):
                summary = up.update(path, excluded)
            with path.open(encoding="utf-8-sig") as handle:
                return sorted(row["pmid"] for row in csv.DictReader(handle)), summary

    def test_excluded_pmid_not_added(self) -> None:
        pmids, summary = self.run_update(["1"], ["1", "2", "3"], {"2"})
        self.assertEqual(pmids, ["1", "3"])
        self.assertEqual(summary["excluded"], 1)
        self.assertEqual(summary["added"], 1)

    def test_record_readded_by_editor_is_kept(self) -> None:
        # Present in the database again although an exclusion row lingers: keep refreshing it.
        pmids, summary = self.run_update(["1", "2"], ["1", "2"], {"2"})
        self.assertEqual(pmids, ["1", "2"])
        self.assertEqual(summary["excluded"], 0)


class DropRemovedDuringRun(unittest.TestCase):
    def test_drops_records_deleted_mid_run(self) -> None:
        started = [{"id": "1"}, {"id": "manual-9"}, {"id": "2"}]
        latest = [{"id": "1"}]
        refreshed = [{"id": "1", "pmid": "1"}, {"id": "manual-9", "pmid": ""},
                     {"id": "2", "pmid": "2"}, {"id": "3", "pmid": "3"}, {"id": "4", "pmid": "4"}]
        kept = sd.drop_removed_during_run(refreshed, started, latest, {"4"})
        self.assertEqual([r["id"] for r in kept], ["1", "3"])


class FetchExcluded(unittest.TestCase):
    def test_missing_table_is_empty(self) -> None:
        client = sd.SupabaseClient.__new__(sd.SupabaseClient)
        error = RuntimeError("Supabase HTTP 404: Could not find the table 'public.excluded_publications'")
        with mock.patch.object(client, "request", side_effect=error):
            self.assertEqual(client.fetch_excluded_pmids(), set())

    def test_other_errors_raise(self) -> None:
        client = sd.SupabaseClient.__new__(sd.SupabaseClient)
        with mock.patch.object(client, "request", side_effect=RuntimeError("Supabase HTTP 500")):
            with self.assertRaises(RuntimeError):
                client.fetch_excluded_pmids()

    def test_reads_pmids(self) -> None:
        client = sd.SupabaseClient.__new__(sd.SupabaseClient)
        with mock.patch.object(client, "request", return_value=[{"pmid": "5"}, {"pmid": "7"}]):
            self.assertEqual(client.fetch_excluded_pmids(), {"5", "7"})


if __name__ == "__main__":
    unittest.main()
