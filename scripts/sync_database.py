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
        # None until the first fetch shows whether the database has the citation columns.
        self.has_citation_columns: bool | None = None

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

    def fetch_publications(self) -> list[dict[str, object]]:
        """Fetch every record, including citation columns when the database has them."""
        if self.has_citation_columns is not False:
            try:
                records = self._fetch_pages(REQUIRED_COLUMNS + CITATION_COLUMNS)
                self.has_citation_columns = True
                return records
            except RuntimeError as error:
                message = str(error).lower()
                if not ("citations" in message and "does not exist" in message):
                    raise
                # The database predates the citation columns: carry on without them.
                self.has_citation_columns = False
        return self._fetch_pages(REQUIRED_COLUMNS)

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

    def update_citations(self, record_id: str, count: int | None, checked_at: str) -> None:
        """Write only the citation fields, so concurrent edits to a record are not overwritten."""
        values: dict[str, object] = {"citations_checked_at": checked_at}
        if count is not None:
            values["citations"] = count
        self.request(
            "PATCH",
            f"/rest/v1/publications?id=eq.{urllib.parse.quote(record_id, safe='')}",
            values,
            "return=minimal",
        )


def database_value_to_csv(column: str, value: object) -> str:
    if value is None:
        return ""
    if column == "citations_checked_at":
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
        help="Skip the Google Scholar citation refresh for this run",
    )
    parser.add_argument(
        "--citations-only",
        action="store_true",
        help="Only look up Google Scholar citation counts: no PubMed update and no CSV changes "
        "(for running on your own computer)",
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
        if args.citations_only:
            if args.seed or args.no_citations:
                raise RuntimeError("--citations-only cannot be combined with --seed or --no-citations.")
            return run_citations_only(client, args)
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
        write_database_export(
            args.csv,
            current,
            REQUIRED_COLUMNS + CITATION_COLUMNS if client.has_citation_columns else REQUIRED_COLUMNS,
        )
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
        return 0
    except Exception as error:
        print(f"Database synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
