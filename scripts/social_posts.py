#!/usr/bin/env python3
"""Schedule LinkedIn posts (through Buffer) for publications newly added to the register."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sync_database import SupabaseClient
from update_publications import PubMedClient, node_text


BUFFER_API = "https://api.buffer.com"
SYDNEY = ZoneInfo("Australia/Sydney")
# Posts go out on weekdays at these Sydney times, so consecutive posts are three hours apart
# and stay within 9am-5pm.
POST_HOURS = (9, 12, 15)
POST_GAP = timedelta(hours=3)
# Leave Buffer a margin before the first post of a run.
MIN_LEAD = timedelta(minutes=15)
LINKEDIN_LIMIT = 3000
HASHTAGS = "#KidneyResearch #Nephrology #KidneyHealth"
MODEL = "claude-opus-5-5"

SUMMARY_PROMPT = """You write LinkedIn posts for the Centre for Kidney Research (CKR) at The Children's \
Hospital at Westmead, Sydney. Each post announces one newly published paper co-authored by CKR researchers.

Write only the summary paragraph of the post: two or three plain-language sentences (at most 450 \
characters) saying what the study looked at and what it found or proposes. The audience is clinicians, \
researchers, patients and families.

- Use only what the title and abstract state. Do not add numbers, claims or implications they do not contain.
- If there is no abstract, describe the topic from the title without stating any findings.
- Australian English, no hashtags, no emoji, no links, no author names, no title, no "we are proud" phrasing.
- Output the paragraph and nothing else."""


def sydney_slot(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), SYDNEY)


def next_slot(earliest: datetime) -> datetime:
    """First weekday posting time at or after `earliest`."""
    day = earliest.astimezone(SYDNEY).date()
    while True:
        if day.weekday() < 5:
            for hour in POST_HOURS:
                slot = sydney_slot(day, hour)
                if slot >= earliest:
                    return slot
        day += timedelta(days=1)


def first_earliest(now: datetime, last_scheduled: datetime | None) -> datetime:
    """Earliest time for this run's first post: after a short lead, and POST_GAP after anything queued."""
    earliest = now + MIN_LEAD
    if last_scheduled is not None:
        earliest = max(earliest, last_scheduled + POST_GAP)
    return earliest


def first_author_surname(authors: list[str]) -> str:
    if not authors:
        return ""
    return authors[0].split(",")[0].strip()


def paper_url(record: dict[str, object]) -> str:
    doi = str(record.get("doi") or "").strip()
    if doi:
        return "https://doi.org/" + doi
    pmid = str(record.get("pmid") or "").strip()
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""


def compose_post(record: dict[str, object], summary: str) -> str:
    authors = [str(name) for name in record.get("authors") or []]
    surname = first_author_surname(authors)
    byline = surname + (" et al." if len(authors) > 1 else "") if surname else ""
    citation = " · ".join(part for part in (byline, f"{record.get('journal')} ({record.get('year')})") if part)
    url = paper_url(record)
    lines = [
        "New publication from the Centre for Kidney Research",
        "",
        summary.strip(),
        "",
        f"“{str(record.get('title') or '').strip()}”",
        citation,
    ]
    if url:
        lines += ["", "Read the paper: " + url]
    lines += ["", HASHTAGS]
    return "\n".join(lines)


def fetch_abstract(pubmed: PubMedClient, pmid: str) -> str:
    if not pmid:
        return ""
    articles = pubmed.fetch_articles([pmid])
    if not articles:
        return ""
    parts = []
    for section in articles[0].findall(".//Abstract/AbstractText"):
        text = node_text(section)
        label = section.get("Label")
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return "\n".join(parts)


def write_summary(client: object, record: dict[str, object], abstract: str) -> str:
    content = f"Title: {record.get('title')}\nJournal: {record.get('journal')} ({record.get('year')})\n\n"
    content += f"Abstract:\n{abstract}" if abstract else "Abstract: (none available)"
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SUMMARY_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarise this abstract.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The summary was cut off.")
    summary = "".join(block.text for block in response.content if block.type == "text").strip()
    if not summary:
        raise RuntimeError("Claude returned an empty summary.")
    return summary


class BufferClient:
    def __init__(self) -> None:
        self.key = os.environ.get("BUFFER_API_KEY", "").strip()
        self.organization_id = os.environ.get("BUFFER_ORGANIZATION_ID", "").strip()
        self.channel_id = os.environ.get("BUFFER_CHANNEL_ID", "").strip()
        if not self.key:
            raise RuntimeError("BUFFER_API_KEY is required.")

    def require_channel(self) -> None:
        if not self.organization_id or not self.channel_id:
            raise RuntimeError(
                "BUFFER_ORGANIZATION_ID and BUFFER_CHANNEL_ID are required. "
                "Run this script with --list-channels to find them."
            )

    def query(self, document: str, variables: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps({"query": document, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(
            BUFFER_API,
            data=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "User-Agent": "CKR-Publication-Register/2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"Buffer HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Could not reach Buffer: {error}") from error
        if payload.get("errors"):
            raise RuntimeError("Buffer error: " + "; ".join(e.get("message", "") for e in payload["errors"]))
        return payload.get("data") or {}

    def list_channels(self) -> list[tuple[dict[str, object], dict[str, object]]]:
        data = self.query("query { account { organizations { id name } } }")
        found = []
        for organization in data["account"]["organizations"]:
            channels = self.query(
                "query($org: OrganizationId!) { channels(input: {organizationId: $org}) "
                "{ id name displayName service isQueuePaused } }",
                {"org": organization["id"]},
            )["channels"]
            found += [(organization, channel) for channel in channels]
        return found

    def last_scheduled(self) -> datetime | None:
        data = self.query(
            "query($org: OrganizationId!, $channel: ChannelId!) { posts(first: 1, input: {"
            "organizationId: $org, filter: {status: [scheduled], channelIds: [$channel]}, "
            "sort: [{field: dueAt, direction: desc}]}) { edges { node { dueAt } } } }",
            {"org": self.organization_id, "channel": self.channel_id},
        )
        edges = data["posts"]["edges"]
        if not edges or not edges[0]["node"].get("dueAt"):
            return None
        return datetime.fromisoformat(edges[0]["node"]["dueAt"].replace("Z", "+00:00"))

    def schedule(self, text: str, due_at: datetime, url: str, title: str) -> tuple[str | None, str]:
        """Returns (post id, "") on success, or (None, error kind: message) when Buffer refuses."""
        linkedin: dict[str, object] = {}
        if url:
            linkedin["linkAttachment"] = {"url": url, "title": title}
        data = self.query(
            "mutation($input: CreatePostInput!) { createPost(input: $input) {"
            " ... on PostActionSuccess { post { id dueAt } }"
            " ... on MutationError { __typename message } } }",
            {"input": {
                "text": text,
                "channelId": self.channel_id,
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "aiAssisted": True,
                "metadata": {"linkedin": linkedin},
            }},
        )
        result = data["createPost"]
        if "post" in result:
            return result["post"]["id"], ""
        return None, f"{result.get('__typename', 'MutationError')}: {result.get('message', '')}"


class SocialPostLog:
    """The social_posts table records every publication already handled, so nothing posts twice."""

    def __init__(self, supabase: SupabaseClient) -> None:
        self.supabase = supabase

    def handled_ids(self) -> set[str]:
        rows = self.supabase.request("GET", "/rest/v1/social_posts?select=publication_id&limit=100000")
        return {str(row["publication_id"]) for row in rows or []}

    def candidates(self, since: str) -> list[dict[str, object]]:
        query = urllib.parse.urlencode({
            "select": "id,pmid,title,authors,year,journal,doi,created_at",
            "created_at": f"gte.{since}",
            "order": "created_at.asc,id.asc",
        })
        return self.supabase.request("GET", f"/rest/v1/publications?{query}") or []

    def record(self, publication_id: str, status: str, **values: object) -> None:
        self.supabase.request(
            "POST",
            "/rest/v1/social_posts?on_conflict=publication_id",
            {"publication_id": publication_id, "status": status, **values},
            "resolution=merge-duplicates,return=minimal",
        )

    def release(self, publication_id: str) -> None:
        self.supabase.request(
            "DELETE", f"/rest/v1/social_posts?publication_id=eq.{urllib.parse.quote(publication_id, safe='')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Write the posts and print them; schedule nothing")
    parser.add_argument("--list-channels", action="store_true", help="Print Buffer organisation and channel IDs")
    parser.add_argument("--max-posts", type=int, default=10, help="Most posts to schedule in one run (default 10)")
    args = parser.parse_args()

    try:
        if args.list_channels:
            for organization, channel in BufferClient().list_channels():
                print(
                    f"{channel['service']:<10} {channel.get('displayName') or channel['name']}\n"
                    f"  BUFFER_ORGANIZATION_ID={organization['id']}  ({organization['name']})\n"
                    f"  BUFFER_CHANNEL_ID={channel['id']}"
                )
            return 0

        since = os.environ.get("SOCIAL_POSTS_SINCE", "").strip()
        if not since:
            raise RuntimeError(
                "SOCIAL_POSTS_SINCE is required (e.g. 2026-10-05): only publications added to the "
                "register on or after this date are posted, so the existing collection is never announced."
            )
        min_year = datetime.now(SYDNEY).year - 1

        log = SocialPostLog(SupabaseClient())
        handled = log.handled_ids()
        pending = [record for record in log.candidates(since) if str(record["id"]) not in handled]
        if not pending:
            print("No new publications to post.")
            return 0

        if not args.dry_run:
            for record in [r for r in pending if not str(r.get("year", "")).isdigit() or int(r["year"]) < min_year]:
                log.record(str(record["id"]), "skipped", note=f"published {record.get('year')}, before {min_year}")
                print(f"Skipped (older paper, {record.get('year')}): {record.get('title')}")
        pending = [r for r in pending if str(r.get("year", "")).isdigit() and int(r["year"]) >= min_year]
        pending = pending[: max(args.max_posts, 0)]
        if not pending:
            print("No new publications to post.")
            return 0

        import anthropic

        claude = anthropic.Anthropic()
        pubmed = PubMedClient()
        buffer = None
        last = None
        if not args.dry_run:
            buffer = BufferClient()
            buffer.require_channel()
            last = buffer.last_scheduled()
        elif os.environ.get("BUFFER_API_KEY", "").strip():
            # Read-only check that the Buffer key and channel work; the dry run still schedules nothing.
            check = BufferClient()
            check.require_channel()
            last = check.last_scheduled()
            print(f"Buffer connection OK; last post already queued: {last or 'none'}.\n")
        earliest = first_earliest(datetime.now(timezone.utc), last)

        scheduled = failed = 0
        for record in pending:
            record_id = str(record["id"])
            title = str(record.get("title") or "")
            try:
                summary = write_summary(claude, record, fetch_abstract(pubmed, str(record.get("pmid") or "")))
                text = compose_post(record, summary)
                if len(text) > LINKEDIN_LIMIT:
                    raise RuntimeError(f"Post is {len(text)} characters, over LinkedIn's {LINKEDIN_LIMIT}.")
            except Exception as error:
                failed += 1
                print(f"::warning::Could not write a post for {record_id} ({title[:60]}): {error}")
                continue

            slot = next_slot(earliest)
            when = slot.astimezone(SYDNEY).strftime("%a %d %b %Y %H:%M")
            if args.dry_run:
                print(f"--- would post {when} (Sydney) ---\n{text}\n")
                earliest = slot + POST_GAP
                continue

            # Claim the record before calling Buffer: a crash between the two leaves it marked
            # 'scheduling' (check it by hand) rather than posting it twice next week.
            log.record(record_id, "scheduling", post_text=text, due_at=slot.isoformat())
            post_id, error = buffer.schedule(text, slot, paper_url(record), title)
            if post_id is None:
                if error.startswith("LimitReachedError"):
                    # Buffer's queue is full: release the claim so next week's run tries again.
                    log.release(record_id)
                    print(f"::warning::Buffer queue is full; remaining posts wait for the next run. ({error})")
                    break
                log.record(record_id, "failed", note=error)
                failed += 1
                print(f"::warning::Buffer refused the post for {record_id}: {error}")
                continue
            log.record(record_id, "scheduled", buffer_post_id=post_id)
            scheduled += 1
            earliest = slot + POST_GAP
            print(f"Scheduled for {when} (Sydney): {title}")

        if not args.dry_run:
            print(f"Social posts: {scheduled} scheduled, {failed} failed.")
        return 0
    except Exception as error:
        print(f"Social posting failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
