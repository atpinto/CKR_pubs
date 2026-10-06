#!/usr/bin/env python3
"""Schedule social media posts (LinkedIn, Facebook and X, through Buffer) for publications newly added
to the register."""

from __future__ import annotations

import argparse
import json
import re
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
# Post length limits per Buffer service. X counts every link as 23 characters.
TEXT_LIMITS = {"linkedin": 3000, "facebook": 5000, "twitter": 280}
X_LINK_LENGTH = 23
URL_PATTERN = re.compile(r"https?://\S+")
# Every post carries the centre's tag; Claude adds up to MAX_TOPIC_TAGS that fit the paper.
CENTRE_HASHTAG = "#KidneyResearch"
MAX_TOPIC_TAGS = 3
HASHTAG_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,39}$")
MODEL = "claude-opus-5-5"

SUMMARY_PROMPT = """You write social media posts for the Centre for Kidney Research (CKR) at The Children's \
Hospital at Westmead, Sydney. Each post announces one newly published paper co-authored by CKR researchers. \
CKR researchers publish on many topics besides kidney disease, such as ageing, public health and dermatology.

Return three things:

summary: two or three plain-language sentences (at most 450 characters) saying what the study looked at and \
what it found or proposes. The audience is clinicians, researchers, patients and families.
- Use only what the title and abstract state. Do not add numbers, claims or implications they do not contain.
- If there is no abstract, describe the topic from the title without stating any findings.
- Australian English, no hashtags, no emoji, no links, no author names, no title, no "we are proud" phrasing.

short_summary: one plain-language sentence of at most 180 characters for X (Twitter), saying what the \
study found or proposes, following the same rules.

hashtags: two or three widely used hashtags for the paper's actual topic, written as single words \
without the # sign (for example Nephrology, KidneyTransplant, HealthyAgeing, PublicHealth). Choose kidney \
hashtags only for papers about the kidney. Do not include KidneyResearch; it is added to every post."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "short_summary": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "short_summary", "hashtags"],
    "additionalProperties": False,
}


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


def since_timestamp(since: str) -> str:
    """SOCIAL_POSTS_SINCE as an exact timestamp. A plain date means midnight in Sydney: the Monday
    7am run happens on Sunday in UTC, so a UTC date would miss that run's papers."""
    value = since.strip()
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(0), SYDNEY).isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


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


def hashtag_line(topic_tags: list[str]) -> str:
    """The centre's tag plus up to MAX_TOPIC_TAGS valid, distinct topic tags."""
    tags = [CENTRE_HASHTAG]
    seen = {CENTRE_HASHTAG.lower()}
    for raw in topic_tags:
        word = str(raw).strip().lstrip("#")
        if not HASHTAG_WORD.match(word) or "#" + word.lower() in seen:
            continue
        tags.append("#" + word)
        seen.add("#" + word.lower())
        if len(tags) > MAX_TOPIC_TAGS:
            break
    return " ".join(tags)


def compose_post(record: dict[str, object], summary: str, topic_tags: list[str]) -> str:
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
    lines += ["", hashtag_line(topic_tags)]
    return "\n".join(lines)


def x_length(text: str) -> int:
    """Length as X counts it: every link is X_LINK_LENGTH characters."""
    return len(URL_PATTERN.sub("x" * X_LINK_LENGTH, text))


def compose_x_post(record: dict[str, object], short_summary: str) -> str:
    """A post that fits X's limit, shortening the sentence at a word boundary if needed."""
    url = paper_url(record)
    tail = ("\n\n" + url if url else "") + "\n\n" + CENTRE_HASHTAG
    sentence = short_summary.strip()
    text = f"New CKR paper: {sentence}{tail}"
    while x_length(text) > TEXT_LIMITS["twitter"] and " " in sentence:
        sentence = sentence.rsplit(" ", 1)[0].rstrip(",;:")
        text = f"New CKR paper: {sentence}\u2026{tail}"
    return text


def post_length(service: str, text: str) -> int:
    return x_length(text) if service == "twitter" else len(text)


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


def write_summary(client: object, record: dict[str, object], abstract: str) -> dict[str, object]:
    """Claude's summary paragraph, one-sentence summary for X, and topic hashtags for one paper."""
    content = f"Title: {record.get('title')}\nJournal: {record.get('journal')} ({record.get('year')})\n\n"
    content += f"Abstract:\n{abstract}" if abstract else "Abstract: (none available)"
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SUMMARY_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarise this abstract.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The summary was cut off.")
    data = json.loads(next(block.text for block in response.content if block.type == "text"))
    summary = str(data.get("summary") or "").strip()
    short_summary = str(data.get("short_summary") or "").strip()
    if not summary or not short_summary:
        raise RuntimeError("Claude returned an empty summary.")
    return {
        "summary": summary,
        "short_summary": short_summary,
        "hashtags": [str(tag) for tag in data.get("hashtags") or []],
    }


def post_text(service: str, record: dict[str, object], written: dict[str, object]) -> str:
    if service == "twitter":
        return compose_x_post(record, str(written["short_summary"]))
    return compose_post(record, str(written["summary"]), list(written["hashtags"]))


class BufferClient:
    def __init__(self) -> None:
        self.key = os.environ.get("BUFFER_API_KEY", "").strip()
        self.organization_id = os.environ.get("BUFFER_ORGANIZATION_ID", "").strip()
        # BUFFER_CHANNEL_IDS lists every channel to post to; BUFFER_CHANNEL_ID is the older single-channel form.
        configured = os.environ.get("BUFFER_CHANNEL_IDS", "") or os.environ.get("BUFFER_CHANNEL_ID", "")
        self.channel_ids = [part.strip() for part in configured.split(",") if part.strip()]
        if not self.key:
            raise RuntimeError("BUFFER_API_KEY is required.")

    def channel_services(self) -> dict[str, str]:
        """{channel id: service} for the configured channels, checked against the organisation."""
        if not self.organization_id or not self.channel_ids:
            raise RuntimeError(
                "BUFFER_ORGANIZATION_ID and BUFFER_CHANNEL_IDS are required. "
                "Run this script with --list-channels to find them."
            )
        channels = self.query(
            "query($org: OrganizationId!) { channels(input: {organizationId: $org}) { id service } }",
            {"org": self.organization_id},
        )["channels"]
        known = {str(channel["id"]): str(channel["service"]) for channel in channels}
        services = {}
        for channel_id in self.channel_ids:
            service = known.get(channel_id)
            if service is None:
                raise RuntimeError(f"Buffer channel {channel_id} is not in organisation {self.organization_id}.")
            if service not in TEXT_LIMITS:
                raise RuntimeError(f"Buffer channel {channel_id} is {service}; only {', '.join(TEXT_LIMITS)} are supported.")
            services[channel_id] = service
        return services

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

    def last_scheduled(self, channel_id: str) -> datetime | None:
        data = self.query(
            "query($org: OrganizationId!, $channel: ChannelId!) { posts(first: 1, input: {"
            "organizationId: $org, filter: {status: [scheduled], channelIds: [$channel]}, "
            "sort: [{field: dueAt, direction: desc}]}) { edges { node { dueAt } } } }",
            {"org": self.organization_id, "channel": channel_id},
        )
        edges = data["posts"]["edges"]
        if not edges or not edges[0]["node"].get("dueAt"):
            return None
        return datetime.fromisoformat(edges[0]["node"]["dueAt"].replace("Z", "+00:00"))

    def schedule(
        self, channel_id: str, service: str, text: str, due_at: datetime, url: str, title: str
    ) -> tuple[str | None, str]:
        """Returns (post id, "") on success, or (None, error kind: message) when Buffer refuses."""
        metadata: dict[str, object] = {}
        link = {"linkAttachment": {"url": url, "title": title}} if url else {}
        if service == "linkedin":
            metadata["linkedin"] = link
        elif service == "facebook":
            metadata["facebook"] = {"type": "post", **link}
        data = self.query(
            "mutation($input: CreatePostInput!) { createPost(input: $input) {"
            " ... on PostActionSuccess { post { id dueAt } }"
            " ... on MutationError { __typename message } } }",
            {"input": {
                "text": text,
                "channelId": channel_id,
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "aiAssisted": True,
                "metadata": metadata,
            }},
        )
        result = data["createPost"]
        if "post" in result:
            return result["post"]["id"], ""
        return None, f"{result.get('__typename', 'MutationError')}: {result.get('message', '')}"


class SocialPostLog:
    """The social_posts table records every publication already handled on each channel, so nothing
    posts twice. A row with an empty channel_id covers all channels (skipped papers, and papers posted
    before there was more than one channel)."""

    def __init__(self, supabase: SupabaseClient) -> None:
        self.supabase = supabase

    def handled(self) -> dict[str, set[str]]:
        rows = self.supabase.request("GET", "/rest/v1/social_posts?select=publication_id,channel_id&limit=100000")
        handled: dict[str, set[str]] = {}
        for row in rows or []:
            handled.setdefault(str(row["publication_id"]), set()).add(str(row.get("channel_id") or ""))
        return handled

    def candidates(self, since: str) -> list[dict[str, object]]:
        query = urllib.parse.urlencode({
            "select": "id,pmid,title,authors,year,journal,doi,created_at",
            "created_at": f"gte.{since_timestamp(since)}",
            "order": "created_at.asc,id.asc",
        })
        return self.supabase.request("GET", f"/rest/v1/publications?{query}") or []

    def record(self, publication_id: str, channel_id: str, status: str, **values: object) -> None:
        self.supabase.request(
            "POST",
            "/rest/v1/social_posts?on_conflict=publication_id,channel_id",
            {"publication_id": publication_id, "channel_id": channel_id, "status": status, **values},
            "resolution=merge-duplicates,return=minimal",
        )

    def release(self, publication_id: str, channel_id: str) -> None:
        query = urllib.parse.urlencode({"publication_id": f"eq.{publication_id}", "channel_id": f"eq.{channel_id}"})
        self.supabase.request("DELETE", f"/rest/v1/social_posts?{query}")


def channels_to_post(handled: set[str], channel_ids: list[str]) -> list[str]:
    """Configured channels this publication has not been handled on yet."""
    if "" in handled:
        return []
    return [channel_id for channel_id in channel_ids if channel_id not in handled]


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
                    f"  channel id: {channel['id']}"
                )
            print("\nSet BUFFER_CHANNEL_IDS to the channel ids to post to, separated by commas.")
            return 0

        since = os.environ.get("SOCIAL_POSTS_SINCE", "").strip()
        if not since:
            raise RuntimeError(
                "SOCIAL_POSTS_SINCE is required (e.g. 2026-10-05): only publications added to the "
                "register on or after this date are posted, so the existing collection is never announced."
            )
        min_year = datetime.now(SYDNEY).year - 1

        log = SocialPostLog(SupabaseClient())
        buffer = BufferClient()
        services = buffer.channel_services()
        channel_ids = list(services)
        handled = log.handled()
        pending = [
            record for record in log.candidates(since)
            if channels_to_post(handled.get(str(record["id"]), set()), channel_ids)
        ]
        if not pending:
            print("No new publications to post.")
            return 0

        if not args.dry_run:
            for record in [r for r in pending if not str(r.get("year", "")).isdigit() or int(r["year"]) < min_year]:
                log.record(str(record["id"]), "", "skipped", note=f"published {record.get('year')}, before {min_year}")
                print(f"Skipped (older paper, {record.get('year')}): {record.get('title')}")
        pending = [r for r in pending if str(r.get("year", "")).isdigit() and int(r["year"]) >= min_year]
        pending = pending[: max(args.max_posts, 0)]
        if not pending:
            print("No new publications to post.")
            return 0

        import anthropic

        claude = anthropic.Anthropic()
        pubmed = PubMedClient()
        now = datetime.now(timezone.utc)
        earliest: dict[str, datetime] = {}
        for channel_id, service in services.items():
            last = buffer.last_scheduled(channel_id)
            earliest[channel_id] = first_earliest(now, last)
            print(f"Buffer {service} channel OK; last post already queued: {last or 'none'}.")
        print()
        full: set[str] = set()

        scheduled = failed = 0
        for record in pending:
            record_id = str(record["id"])
            title = str(record.get("title") or "")
            todo = [
                channel_id for channel_id in channels_to_post(handled.get(record_id, set()), channel_ids)
                if channel_id not in full
            ]
            if not todo:
                continue
            try:
                written = write_summary(claude, record, fetch_abstract(pubmed, str(record.get("pmid") or "")))
            except Exception as error:
                failed += len(todo)
                print(f"::warning::Could not write posts for {record_id} ({title[:60]}): {error}")
                continue

            for channel_id in todo:
                service = services[channel_id]
                text = post_text(service, record, written)
                if post_length(service, text) > TEXT_LIMITS[service]:
                    failed += 1
                    print(f"::warning::The {service} post for {record_id} is over {TEXT_LIMITS[service]} characters.")
                    continue
                slot = next_slot(earliest[channel_id])
                when = slot.astimezone(SYDNEY).strftime("%a %d %b %Y %H:%M")
                if args.dry_run:
                    print(f"--- {service}: would post {when} (Sydney) ---\n{text}\n")
                    earliest[channel_id] = slot + POST_GAP
                    continue

                # Claim the record before calling Buffer: a crash between the two leaves it marked
                # 'scheduling' (check it by hand) rather than posting it twice next week.
                log.record(record_id, channel_id, "scheduling", post_text=text, due_at=slot.isoformat())
                post_id, error = buffer.schedule(channel_id, service, text, slot, paper_url(record), title)
                if post_id is None:
                    if error.startswith("LimitReachedError"):
                        # This channel's queue is full: release the claim so next week's run tries again.
                        log.release(record_id, channel_id)
                        full.add(channel_id)
                        print(f"::warning::Buffer {service} queue is full; its remaining posts wait for the next run.")
                        continue
                    log.record(record_id, channel_id, "failed", note=error)
                    failed += 1
                    print(f"::warning::Buffer refused the {service} post for {record_id}: {error}")
                    continue
                log.record(record_id, channel_id, "scheduled", buffer_post_id=post_id)
                scheduled += 1
                earliest[channel_id] = slot + POST_GAP
                print(f"Scheduled on {service} for {when} (Sydney): {title}")

        if not args.dry_run:
            print(f"Social posts: {scheduled} scheduled, {failed} failed.")
        if failed:
            # Fail the run so GitHub emails the owner; any other posts were still scheduled.
            print(f"{failed} post(s) could not be written or scheduled; see the warnings above.", file=sys.stderr)
            return 1
        return 0
    except Exception as error:
        print(f"Social posting failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
