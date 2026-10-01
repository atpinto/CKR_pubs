#!/usr/bin/env python3
"""Google Scholar citation counts for the CKR publication register.

Google Scholar has no official API, so two lookup routes are supported:

* SerpApi (set SERPAPI_API_KEY): a paid service that returns Scholar results as JSON.
* Direct requests to scholar.google.com (used when no key is set): free, but Google
  often blocks automated traffic, particularly from cloud servers such as GitHub Actions.

Lookups are best effort. A blocked, failed or ambiguous lookup never overwrites an
existing count, and nothing here can stop the PubMed synchronisation.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Callable, Mapping, Protocol


CITATION_COLUMNS = ["citations", "citations_checked_at"]
SCHOLAR_URL = "https://scholar.google.com/scholar"
SERPAPI_URL = "https://serpapi.com/search.json"
USER_AGENT = "Mozilla/5.0 (compatible; CKR-Publication-Register/1.0)"
DEFAULT_MAX_LOOKUPS = 100
DEFAULT_TIME_LIMIT_MINUTES = 60.0
DEFAULT_DELAY_SECONDS = 10.0
MATCH_DEPTH = 5  # how many top-ranked results are compared against the record title
MAX_CONSECUTIVE_FAILURES = 3
BLOCK_MARKERS = ("unusual traffic", "gs_captcha", "recaptcha")


class CitationBlocked(RuntimeError):
    """The service refused or limited the request; stop for this run."""


class CitationLookupError(RuntimeError):
    """One lookup failed in a way that may not affect the next one."""


class Provider(Protocol):
    name: str

    def lookup(self, title: str) -> int | None: ...

    def pause(self) -> float: ...


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp; empty or invalid values sort before every real one."""
    try:
        parsed = datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clean_query(title: str) -> str:
    return re.sub(r"\s+", " ", str(title).replace('"', " ")).strip().rstrip(".").strip()


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def titles_match(wanted: str, found: str) -> bool:
    """True when a Scholar result title is the same publication as the record title."""
    target = normalize_title(wanted)
    candidate = normalize_title(found)
    if not target or not candidate:
        return False
    if found.strip().endswith(("…", "...")):
        # Scholar shortens long titles, so accept a long enough shared prefix.
        return len(candidate) >= 25 and target.startswith(candidate)
    return candidate == target or SequenceMatcher(None, target, candidate).ratio() >= 0.93


def best_count(title: str, results: list[tuple[str, int]]) -> int | None:
    """Citation count of the first top-ranked result that matches the title, else None."""
    for found, cited_by in results[:MATCH_DEPTH]:
        if titles_match(title, found):
            return cited_by
    return None


_TITLE_MARKER = re.compile(r"^(?:\[(?:HTML|PDF|CITATION|C|BOOK|B)\]\s*)+", re.I)
_CITED_BY = re.compile(r"\s*Cited by\s+([\d,.\s]+?)\s*", re.I)
_NO_RESULTS = re.compile(r"did not match any (?:articles|documents)", re.I)


class _ScholarResults(HTMLParser):
    """Collect (title, cited-by count) pairs from a Scholar results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, int]] = []
        self._title: list[str] | None = None
        self._anchor: list[str] | None = None
        self._open = False  # inside a result block

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "div" and "gs_ri" in classes:
            self._open = True
            self.results.append(("", 0))
        elif self._open and tag == "h3" and "gs_rt" in classes:
            self._title = []
        elif self._open and tag == "a" and self._title is None:
            self._anchor = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._title is not None:
            text = _TITLE_MARKER.sub("", "".join(self._title).strip())
            self.results[-1] = (re.sub(r"\s+", " ", text).strip(), self.results[-1][1])
            self._title = None
        elif tag == "a" and self._anchor is not None:
            match = _CITED_BY.fullmatch("".join(self._anchor))
            digits = re.sub(r"\D", "", match.group(1)) if match else ""
            if digits:
                self.results[-1] = (self.results[-1][0], int(digits))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)
        elif self._anchor is not None:
            self._anchor.append(data)


def parse_scholar_results(page: str) -> list[tuple[str, int]]:
    parser = _ScholarResults()
    parser.feed(page)
    parser.close()
    return [(title, cited_by) for title, cited_by in parser.results if title]


class ScholarProvider:
    """Direct requests to scholar.google.com (free, but easily blocked)."""

    name = "Google Scholar (direct)"

    def __init__(self, delay: float = DEFAULT_DELAY_SECONDS) -> None:
        self.delay = delay

    def pause(self) -> float:
        return self.delay * random.uniform(0.7, 1.4)

    def lookup(self, title: str) -> int | None:
        query = urllib.parse.urlencode({"q": clean_query(title), "hl": "en"})
        request = urllib.request.Request(
            f"{SCHOLAR_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                final_url = response.geturl()
                page = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code in (403, 429, 503):
                raise CitationBlocked(f"Google Scholar refused the request (HTTP {error.code}).") from error
            raise CitationLookupError(f"Google Scholar returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise CitationLookupError(f"Could not reach Google Scholar: {error}") from error

        if "/sorry/" in final_url:
            raise CitationBlocked("Google Scholar is asking for a CAPTCHA (automated traffic was detected).")
        results = parse_scholar_results(page)
        if results:
            return best_count(title, results)
        lowered = page.lower()
        if any(marker in lowered for marker in BLOCK_MARKERS):
            raise CitationBlocked("Google Scholar is asking for a CAPTCHA (automated traffic was detected).")
        if _NO_RESULTS.search(page):
            return None
        raise CitationLookupError("Unrecognised Google Scholar page (blocked, consent page or layout change).")


class SerpApiProvider:
    """Google Scholar results through SerpApi (paid; needs SERPAPI_API_KEY)."""

    name = "Google Scholar via SerpApi"

    def __init__(self, api_key: str, delay: float = 0.5) -> None:
        self.api_key = api_key
        self.delay = delay

    def pause(self) -> float:
        return self.delay

    def lookup(self, title: str) -> int | None:
        query = urllib.parse.urlencode({
            "engine": "google_scholar",
            "q": clean_query(title),
            "hl": "en",
            "num": str(MATCH_DEPTH),
            "api_key": self.api_key,
        })
        request = urllib.request.Request(
            f"{SERPAPI_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (401, 402, 403, 429):
                raise CitationBlocked(
                    f"SerpApi refused the request (HTTP {error.code}). Check the API key and plan limits."
                ) from error
            raise CitationLookupError(f"SerpApi returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise CitationLookupError(f"Could not read the SerpApi response: {error}") from error

        message = str(data.get("error") or "") if isinstance(data, dict) else "unexpected response"
        if message:
            if "returned any results" in message.lower():
                return None
            raise CitationBlocked(f"SerpApi error: {message}")
        found: list[tuple[str, int]] = []
        for item in data.get("organic_results") or []:
            cited = ((item.get("inline_links") or {}).get("cited_by") or {}).get("total")
            found.append((str(item.get("title") or ""), cited if isinstance(cited, int) else 0))
        return best_count(title, found)


def number_setting(environ: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, not {raw!r}.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}.")
    return value


def make_provider(environ: Mapping[str, str] = os.environ) -> Provider:
    key = (environ.get("SERPAPI_API_KEY") or "").strip()
    if key:
        return SerpApiProvider(key)
    return ScholarProvider(number_setting(environ, "CITATION_DELAY_SECONDS", DEFAULT_DELAY_SECONDS, 1))


def citation_priority(row: Mapping[str, str]) -> tuple[datetime, int, str]:
    """Never-checked records first (newest year first), then the stalest checks."""
    try:
        year = int(row.get("year") or 0)
    except ValueError:
        year = 0
    return (parse_timestamp(row.get("citations_checked_at") or ""), -year, row.get("id") or "")


def refresh_citations(
    rows: list[dict[str, str]],
    provider: Provider,
    *,
    max_lookups: int,
    time_limit_seconds: float,
    apply: Callable[[dict[str, str], int | None, str], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Look up the highest-priority rows and hand each result to ``apply``.

    ``apply(row, count, checked_at)`` stores the result; ``count`` is None when no
    Scholar result matched the title. Nothing is applied for blocked or failed lookups.
    """
    queue = sorted((row for row in rows if (row.get("title") or "").strip()), key=citation_priority)
    queue = queue[: max(0, max_lookups)]
    stats: dict[str, object] = {
        "provider": provider.name, "planned": len(queue), "looked_up": 0,
        "counted": 0, "not_found": 0, "failed": 0, "stopped": "",
    }
    started = clock()
    failures = 0
    for index, row in enumerate(queue):
        if clock() - started >= time_limit_seconds:
            stats["stopped"] = "time limit reached"
            break
        if index:
            sleep(provider.pause())
        try:
            count = provider.lookup(row["title"])
        except CitationBlocked as error:
            stats["stopped"] = str(error)
            break
        except CitationLookupError as error:
            failures += 1
            stats["failed"] = int(stats["failed"]) + 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                stats["stopped"] = f"{failures} lookups failed in a row (last: {error})"
                break
            continue
        failures = 0
        apply(row, count, utc_timestamp())
        stats["looked_up"] = int(stats["looked_up"]) + 1
        stats["not_found" if count is None else "counted"] = int(stats["not_found" if count is None else "counted"]) + 1
    return stats
