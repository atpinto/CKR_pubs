#!/usr/bin/env python3
"""Scopus citation counts for the CKR publication register.

Uses Elsevier's Scopus Search API (https://dev.elsevier.com). Records are looked up in
batches by DOI, or by PMID when the DOI is missing or contains characters that are unsafe in
a Scopus query. A count is only ever taken from a result whose DOI or PMID is exactly the
requested one, so a query that returns extra documents cannot attach the wrong number.

Access to Scopus data depends on your institution's subscription. A key used from outside
the institution's network usually needs an institutional token (SCOPUS_INSTTOKEN), which
Elsevier issues on request.

Lookups are best effort: a refused, failed or empty lookup never overwrites an existing count.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Sequence

from citations_common import (
    MAX_CONSECUTIVE_FAILURES,
    CitationBlocked,
    CitationLookupError,
    number_setting,
    parse_timestamp,
    utc_timestamp,
)


SCOPUS_COLUMNS = ["scopus_citations", "scopus_citations_checked_at", "scopus_url"]
SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
USER_AGENT = "CKR-Publication-Register/1.0"
# Without institutional entitlement Scopus rejects count > 25 (HTTP 400 "Exceeds the maximum number allowed
# for the service level"), so each request asks for 25 results and covers 12 records, leaving room for
# the occasional extra match.
BATCH_SIZE = 12
MAX_RESULTS = 25
DEFAULT_MAX_LOOKUPS = 2000  # records per run; a full pass of the collection is about 115 requests
DEFAULT_TIME_LIMIT_MINUTES = 30.0
DEFAULT_DELAY_SECONDS = 0.3  # the API allows about 9 requests a second
SAFE_DOI = re.compile(r"[A-Za-z0-9./_\-]+")
ATTRIBUTION_URL = "https://www.scopus.com/"


def query_term(row: Mapping[str, str]) -> tuple[str, str] | None:
    """('DOI', value) or ('PMID', value) for a record, or None when it has neither usable."""
    doi = (row.get("doi") or "").strip()
    if doi and SAFE_DOI.fullmatch(doi):
        return "DOI", doi
    pmid = (row.get("pmid") or "").strip()
    if pmid.isdigit():
        return "PMID", pmid
    return None


def scopus_priority(row: Mapping[str, str]) -> tuple[object, int, str]:
    """Never-checked records first (newest year first), then the stalest checks."""
    try:
        year = int(row.get("year") or 0)
    except ValueError:
        year = 0
    return (parse_timestamp(row.get("scopus_citations_checked_at") or ""), -year, row.get("id") or "")


def error_detail(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    status = (data.get("service-error") or {}).get("status") or {}
    return str(status.get("statusText") or (data.get("error-response") or {}).get("error-message") or "")


def parse_entries(data: object) -> tuple[dict[str, tuple[int, str]], dict[str, tuple[int, str]]]:
    """Index a Scopus Search response by lower-cased DOI and by PMID: (count, cited-by link)."""
    by_doi: dict[str, tuple[int, str]] = {}
    by_pmid: dict[str, tuple[int, str]] = {}
    results = data.get("search-results") if isinstance(data, dict) else None
    entries = results.get("entry") if isinstance(results, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("error"):
            continue  # an empty result set is reported as a single entry holding an error text
        try:
            count = int(str(entry.get("citedby-count")).strip())
        except ValueError:
            continue
        if count < 0:
            continue
        url = ""
        for link in entry.get("link") or []:
            if isinstance(link, dict) and link.get("@ref") == "scopus-citedby":
                url = str(link.get("@href") or "")
        if not url.startswith(ATTRIBUTION_URL):
            url = ""
        for index, key in ((by_doi, str(entry.get("prism:doi") or "").strip().lower()),
                           (by_pmid, str(entry.get("pubmed-id") or "").strip())):
            if key and (key not in index or count > index[key][0]):
                index[key] = (count, url)
    return by_doi, by_pmid


class ScopusClient:
    name = "Scopus"

    def __init__(self, api_key: str, insttoken: str = "", delay: float = DEFAULT_DELAY_SECONDS) -> None:
        self.api_key = api_key
        self.insttoken = insttoken
        self.delay = delay

    def pause(self) -> float:
        return self.delay

    def lookup(self, rows: Sequence[Mapping[str, str]]) -> dict[str, tuple[int, str] | None]:
        """Counts for a batch of records, keyed by record id; None when Scopus has no match."""
        terms = {str(row["id"]): query_term(row) for row in rows}
        wanted = {record_id: term for record_id, term in terms.items() if term}
        if not wanted:
            return {}
        query = " OR ".join(f"{kind}({value})" for kind, value in wanted.values())
        params = urllib.parse.urlencode({"query": query, "count": str(MAX_RESULTS)})
        headers = {"X-ELS-APIKey": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if self.insttoken:
            headers["X-ELS-Insttoken"] = self.insttoken
        request = urllib.request.Request(f"{SEARCH_URL}?{params}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error_detail(error.read())
            suffix = f": {detail}" if detail else ""
            if error.code in (401, 403):
                raise CitationBlocked(
                    f"Scopus refused the request (HTTP {error.code}{suffix}). Check the API key, and whether your "
                    "access needs an institutional token (SCOPUS_INSTTOKEN) or your institution's network."
                ) from error
            if error.code == 429:
                raise CitationBlocked(f"Scopus quota or rate limit reached (HTTP 429{suffix}).") from error
            raise CitationLookupError(f"Scopus returned HTTP {error.code}{suffix}.") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise CitationLookupError(f"Could not read the Scopus response: {error}") from error

        by_doi, by_pmid = parse_entries(data)
        found: dict[str, tuple[int, str] | None] = {}
        for record_id, (kind, value) in wanted.items():
            found[record_id] = by_doi.get(value.lower()) if kind == "DOI" else by_pmid.get(value)
        return found


def make_client(environ: Mapping[str, str] = os.environ) -> ScopusClient | None:
    """A client when SCOPUS_API_KEY is set, otherwise None (Scopus lookups are then skipped)."""
    key = (environ.get("SCOPUS_API_KEY") or "").strip()
    if not key:
        return None
    return ScopusClient(
        key,
        (environ.get("SCOPUS_INSTTOKEN") or "").strip(),
        number_setting(environ, "SCOPUS_DELAY_SECONDS", DEFAULT_DELAY_SECONDS, 0),
    )


def refresh_scopus(
    rows: list[dict[str, str]],
    client: ScopusClient,
    *,
    max_lookups: int,
    time_limit_seconds: float,
    apply: Callable[[dict[str, str], int | None, str, str], None],
    batch_size: int = BATCH_SIZE,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Look up the highest-priority records in batches and hand each result to ``apply``.

    ``apply(row, count, url, checked_at)``; ``count`` is None when Scopus has no match.
    Nothing is applied for refused or failed requests.
    """
    queue = sorted((row for row in rows if query_term(row)), key=scopus_priority)[: max(0, max_lookups)]
    stats: dict[str, object] = {
        "provider": client.name, "planned": len(queue), "looked_up": 0,
        "counted": 0, "not_found": 0, "failed": 0, "stopped": "",
    }
    started = clock()
    failures = 0
    for start in range(0, len(queue), batch_size):
        if clock() - started >= time_limit_seconds:
            stats["stopped"] = "time limit reached"
            break
        if start:
            sleep(client.pause())
        batch = queue[start : start + batch_size]
        try:
            results = client.lookup(batch)
        except CitationBlocked as error:
            stats["stopped"] = str(error)
            break
        except CitationLookupError as error:
            failures += 1
            stats["failed"] = int(stats["failed"]) + len(batch)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                stats["stopped"] = f"{failures} requests failed in a row (last: {error})"
                break
            continue
        failures = 0
        checked_at = utc_timestamp()
        for row in batch:
            match = results.get(str(row["id"]))
            apply(row, match[0] if match else None, match[1] if match else "", checked_at)
            stats["looked_up"] = int(stats["looked_up"]) + 1
            key = "counted" if match else "not_found"
            stats[key] = int(stats[key]) + 1
    return stats
