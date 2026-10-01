#!/usr/bin/env python3
"""Synchronize the CKR Supabase database, PubMed, and the CSV backup."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from scholar_citations import (
    CITATION_COLUMNS,
    DEFAULT_MAX_LOOKUPS,
    DEFAULT_TIME_LIMIT_MINUTES,
    make_provider,
    number_setting,
    refresh_citations,
)
from scopus_citations import (
    DEFAULT_MAX_LOOKUPS as SCOPUS_DEFAULT_MAX_LOOKUPS,
    DEFAULT_TIME_LIMIT_MINUTES as SCOPUS_DEFAULT_TIME_LIMIT_MINUTES,
    SCOPUS_COLUMNS,
    make_client as make_scopus_client,
    query_term,
    refresh_scopus,
)
from update_publications import REQUIRED_COLUMNS, compact_json, read_csv, row_to_record, update, write_csv


PAGE_SIZE = 500
ARRAY_COLUMNS = {
    "authors", "affiliations", "publication_types", "electronic_dates", "source_keywords"
}
ASSOCIATION_COLUMNS = {"projects", "programmes"}
JSON_COLUMNS = {"corrections", "project_evidence", "programme_evidence"}


class SupabaseClient:
    def __init__(self) -> None:
        self.url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        self.secret = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        if not self.url or not self.secret:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required.")
        # None until the first fetch shows which optional citation columns the database has.
        self.has_citation_columns: bool | None = None
        self.has_scopus_columns: bool | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        prefer: str = "",
    ) -> object | None:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "apikey": self.secret,
            "Accept": "application/json",
            "User-Agent": "CKR-Publication-Register/2.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"Supabase HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Could not reach Supabase: {error}") from error
        return json.loads(data) if data else None

    def export_columns(self) -> list[str]:
        """Columns this database has: the required ones plus any citation columns it was given."""
        columns = list(REQUIRED_COLUMNS)
        if self.has_citation_columns:
            columns += CITATION_COLUMNS
        if self.has_scopus_columns:
            columns += SCOPUS_COLUMNS
        return columns

    def fetch_publications(self) -> list[dict[str, object]]:
        """Fetch every record, including whichever citation columns the database has."""
        if self.has_citation_columns is not None:
            return self._fetch_pages(self.export_columns())
        # Try everything first, then fall back for databases that predate a set of columns.
        for scholar, scopus in ((True, True), (True, False), (False, False)):
            self.has_citation_columns, self.has_scopus_columns = scholar, scopus
            try:
                return self._fetch_pages(self.export_columns())
            except RuntimeError as error:
                message = str(error).lower()
                if (scholar or scopus) and "citations" in message and "does not exist" in message:
                    continue
                raise
        raise RuntimeError("Could not read the publication table.")

    def _fetch_pages(self, column_names: list[str]) -> list[dict[str, object]]:
        columns = ",".join(column_names)
        records: list[dict[str, object]] = []
        for offset in range(0, 1_000_000, PAGE_SIZE):
            query = urllib.parse.urlencode({
                "select": columns,
                "order": "id.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            })
            page = self.request("GET", f"/rest/v1/publications?{query}")
            if not isinstance(page, list):
                raise RuntimeError("Supabase returned an invalid publication response.")
            records.extend(page)
            if len(page) < PAGE_SIZE:
                break
        return records

    def upsert_publications(self, records: list[dict[str, object]]) -> None:
        for start in range(0, len(records), 100):
            batch = records[start:start + 100]
            self.request(
                "POST",
                "/rest/v1/publications?on_conflict=id",
                batch,
                "resolution=merge-duplicates,return=minimal",
            )

    def _patch(self, record_id: str, values: dict[str, object]) -> None:
        self.request(
            "PATCH",
            f"/rest/v1/publications?id=eq.{urllib.parse.quote(record_id, safe='')}",
            values,
            "return=minimal",
        )

    def update_citations(self, record_id: str, count: int | None, checked_at: str) -> None:
        """Write only the Google Scholar fields, so concurrent edits to a record are not overwritten."""
        values: dict[str, object] = {"citations_checked_at": checked_at}
        if count is not None:
            values["citations"] = count
        self._patch(record_id, values)

    def update_scopus(self, record_id: str, count: int | None, url: str, checked_at: str) -> None:
        """Write only the Scopus fields. A missing match keeps any earlier count."""
        values: dict[str, object] = {"scopus_citations_checked_at": checked_at}
        if count is not None:
            values["scopus_citations"] = count
            values["scopus_url"] = url or None
        self._patch(record_id, values)


def database_value_to_csv(column: str, value: object) -> str:
    if value is None:
        return ""
    if column in ("citations_checked_at", "scopus_citations_checked_at"):
        return str(value).replace("+00:00", "Z")
    if column in ARRAY_COLUMNS or column in JSON_COLUMNS:
        return compact_json(value)
    if column in ASSOCIATION_COLUMNS:
        return " | ".join(str(item) for item in (value or []))
    if column == "affiliation_review":
        return "true" if bool(value) else "false"
    return str(value)


def write_database_export(
    path: Path, records: list[dict[str, object]], columns: list[str] = REQUIRED_COLUMNS
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow({
                column: database_value_to_csv(column, record.get(column))
                for column in columns
            })


def read_database_import(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise RuntimeError("The CSV has no header.")
        missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise RuntimeError("The CSV is missing columns: " + ", ".join(missing))
        rows = list(reader)

    records: list[dict[str, object]] = []
    for row_number, row in enumerate(rows, start=2):
        record = row_to_record(row, row_number)
        record["collection_checked_at"] = row.get("collection_checked_at") or None
        # Older CSV backups have no citation columns; only carry them when present.
        if "citations" in reader.fieldnames:
            digits = (row.get("citations") or "").strip()
            record["citations"] = int(digits) if digits.isdigit() else None
        if "citations_checked_at" in reader.fieldnames:
            record["citations_checked_at"] = (row.get("citations_checked_at") or "").strip() or None
        if "scopus_citations" in reader.fieldnames:
            digits = (row.get("scopus_citations") or "").strip()
            record["scopus_citations"] = int(digits) if digits.isdigit() else None
        for column in ("scopus_citations_checked_at", "scopus_url"):
            if column in reader.fieldnames:
                record[column] = (row.get(column) or "").strip() or None
        records.append(record)
    return records


def preserve_latest_manual_associations(
    refreshed: list[dict[str, object]],
    latest: list[dict[str, object]],
) -> None:
    latest_by_id = {str(record.get("id")): record for record in latest}
    for record in refreshed:
        current = latest_by_id.get(str(record["id"]))
        if not current:
            continue
        for kind, evidence in (
            ("projects", "project_evidence"),
            ("programmes", "programme_evidence"),
        ):
            source = kind + "_source"
            if current.get(source) == "manual":
                record[kind] = current.get(kind) or []
                record[evidence] = {}
                record[source] = "manual"


def refresh_citation_counts(
    client: SupabaseClient, csv_path: Path, max_lookups: int, time_limit_minutes: float
) -> dict[str, object]:
    """Look up Google Scholar counts for the next records; save them to the database and CSV."""
    fieldnames, rows, _ = read_csv(csv_path)

    def store(row: dict[str, str], count: int | None, checked_at: str) -> None:
        # Database first, so the CSV never claims a value the database does not have.
        client.update_citations(row["id"], count, checked_at)
        if count is not None:
            row["citations"] = str(count)
        row["citations_checked_at"] = checked_at

    try:
        return refresh_citations(
            rows,
            make_provider(),
            max_lookups=max_lookups,
            time_limit_seconds=time_limit_minutes * 60,
            apply=store,
        )
    finally:
        # Keep whatever progress was made, even if the run stopped part-way.
        write_csv(csv_path, fieldnames, rows)


def citation_limits(args: argparse.Namespace) -> tuple[int, float]:
    """Lookups allowed and minutes allowed, from the command line, then the environment, then defaults."""
    max_lookups = args.citation_max_lookups
    if max_lookups is None:
        max_lookups = int(number_setting(os.environ, "CITATION_MAX_LOOKUPS", DEFAULT_MAX_LOOKUPS, 0))
    time_limit = args.citation_time_limit_minutes
    if time_limit is None:
        time_limit = number_setting(os.environ, "CITATION_TIME_LIMIT_MINUTES", DEFAULT_TIME_LIMIT_MINUTES, 1)
    return max_lookups, time_limit


def run_citations_only(client: SupabaseClient, args: argparse.Namespace) -> int:
    """Look up citation counts only: no PubMed, and no changes to the CSV backup.

    Meant for running on your own computer when Google Scholar blocks GitHub's servers.
    The next weekly run exports these counts into the CSV backup.
    """
    current = client.fetch_publications()
    if not current:
        raise RuntimeError("The database is empty. Run this command once with --seed first.")
    if not client.has_citation_columns:
        raise RuntimeError("The database has no citation columns yet. Run supabase/schema.sql in the Supabase SQL editor.")
    rows = [
        {
            "id": str(record["id"]),
            "title": str(record.get("title") or ""),
            "year": str(record.get("year") or ""),
            "citations_checked_at": database_value_to_csv("citations_checked_at", record.get("citations_checked_at")),
        }
        for record in current
    ]
    max_lookups, time_limit = citation_limits(args)
    saved = 0

    def store(row: dict[str, str], count: int | None, checked_at: str) -> None:
        nonlocal saved
        client.update_citations(row["id"], count, checked_at)
        saved += 1
        shown = "not found on Scholar" if count is None else f"cited by {count}"
        print(f"  [{saved}] {shown}: {row['title'][:70]}", flush=True)

    provider = make_provider()
    print(f"Looking up citations with {provider.name}: up to {max_lookups} records, stopping after {time_limit:g} minutes.")
    stats = refresh_citations(
        rows, provider, max_lookups=max_lookups, time_limit_seconds=time_limit * 60, apply=store
    )
    checked = sum(1 for row in rows if row["citations_checked_at"]) + int(stats["looked_up"])
    print(
        f"Done: {stats['looked_up']} of {stats['planned']} lookups saved; "
        f"{stats['counted']} counted, {stats['not_found']} not found, {stats['failed']} failed. "
        f"{checked} of {len(rows)} records have now been checked at least once."
    )
    if stats["stopped"]:
        print(f"Stopped early: {stats['stopped']}", file=sys.stderr)
        if not stats["looked_up"]:
            return 1
    return 0


def run_citation_stage(client: SupabaseClient, args: argparse.Namespace) -> None:
    """Best-effort citation refresh. Problems become warnings and never fail the sync."""
    if args.no_citations:
        print("Citation counts skipped (--no-citations).")
        return
    if not client.has_citation_columns:
        print(
            "::warning::Citation counts skipped: the database has no citation columns yet. "
            "Run supabase/schema.sql in the Supabase SQL editor."
        )
        return
    try:
        max_lookups, time_limit = citation_limits(args)
        stats = refresh_citation_counts(client, args.csv, max_lookups, time_limit)
    except Exception as error:
        print(f"::warning::Citation update skipped: {error}")
        return
    print(
        f"Citation counts ({stats['provider']}): {stats['looked_up']} of {stats['planned']} planned lookups done; "
        f"{stats['counted']} counted, {stats['not_found']} not found, {stats['failed']} failed."
    )
    if stats["stopped"]:
        print(f"::warning::Citation update stopped early: {stats['stopped']}")


def scopus_limits(args: argparse.Namespace) -> tuple[int, float]:
    """Records allowed and minutes allowed for Scopus lookups."""
    max_lookups = args.scopus_max_lookups
    if max_lookups is None:
        max_lookups = int(number_setting(os.environ, "SCOPUS_MAX_LOOKUPS", SCOPUS_DEFAULT_MAX_LOOKUPS, 0))
    time_limit = number_setting(os.environ, "SCOPUS_TIME_LIMIT_MINUTES", SCOPUS_DEFAULT_TIME_LIMIT_MINUTES, 1)
    return max_lookups, time_limit


def refresh_scopus_counts(
    client: SupabaseClient, csv_path: Path, scopus: object, max_lookups: int, time_limit_minutes: float
) -> dict[str, object]:
    """Look up Scopus counts for the next records; save them to the database and CSV."""
    fieldnames, rows, _ = read_csv(csv_path)

    def store(row: dict[str, str], count: int | None, url: str, checked_at: str) -> None:
        # Database first, so the CSV never claims a value the database does not have.
        client.update_scopus(row["id"], count, url, checked_at)
        if count is not None:
            row["scopus_citations"] = str(count)
            row["scopus_url"] = url
        row["scopus_citations_checked_at"] = checked_at

    try:
        return refresh_scopus(
            rows, scopus, max_lookups=max_lookups, time_limit_seconds=time_limit_minutes * 60, apply=store
        )
    finally:
        write_csv(csv_path, fieldnames, rows)


def run_scopus_stage(client: SupabaseClient, args: argparse.Namespace) -> None:
    """Best-effort Scopus refresh. Problems become warnings and never fail the sync."""
    if args.no_citations:
        return
    scopus = make_scopus_client()
    if scopus is None:
        print("Scopus counts skipped: SCOPUS_API_KEY is not set.")
        return
    if not client.has_scopus_columns:
        print(
            "::warning::Scopus counts skipped: the database has no Scopus columns yet. "
            "Run supabase/schema.sql in the Supabase SQL editor."
        )
        return
    try:
        max_lookups, time_limit = scopus_limits(args)
        stats = refresh_scopus_counts(client, args.csv, scopus, max_lookups, time_limit)
    except Exception as error:
        print(f"::warning::Scopus update skipped: {error}")
        return
    print(
        f"Scopus counts: {stats['looked_up']} of {stats['planned']} planned records done; "
        f"{stats['counted']} counted, {stats['not_found']} not in Scopus, {stats['failed']} failed."
    )
    if stats["stopped"]:
        print(f"::warning::Scopus update stopped early: {stats['stopped']}")


def run_scopus_only(client: SupabaseClient, args: argparse.Namespace) -> int:
    """Look up Scopus counts only: no PubMed, and no changes to the CSV backup."""
    scopus = make_scopus_client()
    if scopus is None:
        raise RuntimeError("SCOPUS_API_KEY is required for --scopus-only.")
    current = client.fetch_publications()
    if not current:
        raise RuntimeError("The database is empty. Run this command once with --seed first.")
    if not client.has_scopus_columns:
        raise RuntimeError("The database has no Scopus columns yet. Run supabase/schema.sql in the Supabase SQL editor.")
    rows = [
        {
            "id": str(record["id"]),
            "title": str(record.get("title") or ""),
            "year": str(record.get("year") or ""),
            "doi": str(record.get("doi") or ""),
            "pmid": str(record.get("pmid") or ""),
            "scopus_citations_checked_at": database_value_to_csv(
                "scopus_citations_checked_at", record.get("scopus_citations_checked_at")
            ),
        }
        for record in current
    ]
    max_lookups, time_limit = scopus_limits(args)
    saved = 0

    def store(row: dict[str, str], count: int | None, url: str, checked_at: str) -> None:
        nonlocal saved
        client.update_scopus(row["id"], count, url, checked_at)
        saved += 1
        if saved <= 5 or saved % 25 == 0:
            shown = "not in Scopus" if count is None else f"cited by {count}"
            print(f"  [{saved}] {shown}: {row['title'][:70]}", flush=True)

    eligible = sum(1 for row in rows if query_term(row))
    print(f"Looking up citations in Scopus: up to {max_lookups} of {eligible} records, in batches of 25.")
    stats = refresh_scopus(rows, scopus, max_lookups=max_lookups, time_limit_seconds=time_limit * 60, apply=store)
    checked = sum(1 for row in rows if row["scopus_citations_checked_at"] and query_term(row)) + int(stats["looked_up"])
    print(
        f"Done: {stats['looked_up']} of {stats['planned']} records saved; "
        f"{stats['counted']} counted, {stats['not_found']} not in Scopus, {stats['failed']} failed. "
        f"About {min(checked, eligible)} of {eligible} records have now been checked at least once."
    )
    if stats["stopped"]:
        print(f"Stopped early: {stats['stopped']}", file=sys.stderr)
        if not stats["looked_up"]:
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="publications.csv", type=Path, help="CSV backup path")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Import the CSV into an empty/new database without querying PubMed",
    )
    parser.add_argument(
        "--no-citations",
        action="store_true",
        help="Skip the Google Scholar and Scopus citation refresh for this run",
    )
    parser.add_argument(
        "--citations-only",
        action="store_true",
        help="Only look up Google Scholar citation counts: no PubMed update and no CSV changes "
        "(for running on your own computer)",
    )
    parser.add_argument(
        "--scopus-only",
        action="store_true",
        help="Only look up Scopus citation counts: no PubMed update and no CSV changes "
        "(needs SCOPUS_API_KEY; for running on your own computer)",
    )
    parser.add_argument(
        "--scopus-max-lookups",
        type=int,
        default=None,
        help=f"Maximum records to look up in Scopus this run (default {SCOPUS_DEFAULT_MAX_LOOKUPS}, "
        "or SCOPUS_MAX_LOOKUPS)",
    )
    parser.add_argument(
        "--citation-max-lookups",
        type=int,
        default=None,
        help=f"Maximum Google Scholar lookups this run (default {DEFAULT_MAX_LOOKUPS}, or CITATION_MAX_LOOKUPS)",
    )
    parser.add_argument(
        "--citation-time-limit-minutes",
        type=float,
        default=None,
        help=f"Stop looking up citations after this long (default {DEFAULT_TIME_LIMIT_MINUTES:g}, "
        "or CITATION_TIME_LIMIT_MINUTES)",
    )
    args = parser.parse_args()

    try:
        client = SupabaseClient()
        if args.citations_only or args.scopus_only:
            if args.citations_only and args.scopus_only:
                raise RuntimeError("Use --citations-only or --scopus-only, not both.")
            if args.seed or args.no_citations:
                raise RuntimeError("--citations-only and --scopus-only cannot be combined with --seed or --no-citations.")
            return run_citations_only(client, args) if args.citations_only else run_scopus_only(client, args)
        if args.seed:
            records = read_database_import(args.csv)
            if not records:
                raise RuntimeError("The CSV contains no publication records.")
            client.upsert_publications(records)
            print(f"Seeded {len(records)} publication records into Supabase.")
            return 0

        current = client.fetch_publications()
        if not current:
            raise RuntimeError("The database is empty. Run this command once with --seed first.")
        write_database_export(args.csv, current, client.export_columns())
        summary = update(args.csv)
        refreshed = read_database_import(args.csv)
        preserve_latest_manual_associations(refreshed, client.fetch_publications())
        client.upsert_publications(refreshed)
        print(
            "Database update complete: "
            f"{summary['records']} records, {summary['added']} added, "
            f"{summary['refreshed']} refreshed, "
            f"{summary['affiliation_review']} affiliations to review."
        )
        run_citation_stage(client, args)
        run_scopus_stage(client, args)
        return 0
    except Exception as error:
        print(f"Database synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
