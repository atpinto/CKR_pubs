#!/usr/bin/env python3
"""Small helpers shared by the citation lookups."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping


MAX_CONSECUTIVE_FAILURES = 3


class CitationBlocked(RuntimeError):
    """The service refused or limited the request; stop for this run."""


class CitationLookupError(RuntimeError):
    """One lookup failed in a way that may not affect the next one."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp; empty or invalid values sort before every real one."""
    try:
        parsed = datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
