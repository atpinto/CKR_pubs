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

from update_publications import REQUIRED_COLUMNS, compact_json, row_to_record, update


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
        columns = ",".join(REQUIRED_COLUMNS)
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


def database_value_to_csv(column: str, value: object) -> str:
    if value is None:
        return ""
    if column in ARRAY_COLUMNS or column in JSON_COLUMNS:
        return compact_json(value)
    if column in ASSOCIATION_COLUMNS:
        return " | ".join(str(item) for item in (value or []))
    if column == "affiliation_review":
        return "true" if bool(value) else "false"
    return str(value)


def write_database_export(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow({
                column: database_value_to_csv(column, record.get(column))
                for column in REQUIRED_COLUMNS
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="publications.csv", type=Path, help="CSV backup path")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Import the CSV into an empty/new database without querying PubMed",
    )
    args = parser.parse_args()

    try:
        client = SupabaseClient()
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
        write_database_export(args.csv, current)
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
        return 0
    except Exception as error:
        print(f"Database synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
