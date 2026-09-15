#!/usr/bin/env python3
"""Refresh publications.csv from PubMed while preserving curated associations."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


PUBMED_QUERY = '("Centre for Kidney Research"[Affiliation] OR "Center for Kidney Research"[Affiliation])'
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
BATCH_SIZE = 100
REQUIRED_COLUMNS = [
    "id", "pmid", "title", "authors", "year", "journal", "abbreviation", "volume", "issue",
    "start_page", "end_page", "doi", "pmcid", "issn", "language", "affiliations",
    "publication_types", "publication_date", "electronic_dates", "corrections", "projects",
    "programmes", "project_evidence", "programme_evidence", "projects_source",
    "programmes_source", "source_keywords", "affiliation_review", "record_source",
    "collection_checked_at",
]

PROJECT_PATTERNS = {
    "NAVKIDS2": re.compile(r"\bNAVKIDS(?:\s*\(?2\)?|²)?\b", re.I),
    "KCAD": re.compile(r"\bKCAD\b|\bKids with CKD\b", re.I),
    "ARDAC": re.compile(r"\bARDAC\b|\bAntecedents of Renal Disease in Aboriginal Children\b", re.I),
    "TACKLE-IT": re.compile(r"\bTACKLE[ -]?IT\b", re.I),
}
PROGRAMME_PATTERNS = {
    "SONG": re.compile(r"\bSONG\b|\bStandardi[sz]ed Outcomes in Nephrology\b", re.I),
    "BEAT-CKD": re.compile(r"\bBEAT[ -]?CKD\b|\bBetter Evidence and Translation in Chronic Kidney Disease\b", re.I),
    "CARI": re.compile(r"\bCARI\b|\bCaring for Austral(?:ians|asians) (?:and|&) New Zealanders with Kidney Impairment\b", re.I),
}
CKR_AFFILIATION = re.compile(r"\bcent(?:re|er) for kidney research\b", re.I)
CKR_LOCATION = re.compile(r"\b(?:Sydney|Westmead|New South Wales|NSW)\b", re.I)


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return normalize_space("".join(node.itertext()))


def find_text(root: ET.Element | None, path: str) -> str:
    return node_text(root.find(path) if root is not None else None)


def parse_json(value: str, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def array_field(value: str) -> list[str]:
    parsed = parse_json(value, None)
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item)]
    return [item.strip() for item in re.split(r"\s*\|\s*|\r?\n", value or "") if item.strip()]


def association_field(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"\s*\|\s*", value or "") if item.strip()]


def boolean_field(value: str) -> bool:
    return str(value or "").lower() in {"true", "1", "yes"}


def row_to_record(row: dict[str, str], row_number: int) -> dict[str, object]:
    pmid = (row.get("pmid") or "").strip()
    return {
        "id": (row.get("id") or pmid or f"manual-{row_number}").strip(),
        "pmid": pmid,
        "title": (row.get("title") or "").strip(),
        "authors": array_field(row.get("authors", "")),
        "year": (row.get("year") or "").strip(),
        "journal": (row.get("journal") or "").strip(),
        "abbreviation": (row.get("abbreviation") or "").strip(),
        "volume": (row.get("volume") or "").strip(),
        "issue": (row.get("issue") or "").strip(),
        "start_page": (row.get("start_page") or "").strip(),
        "end_page": (row.get("end_page") or "").strip(),
        "doi": (row.get("doi") or "").strip(),
        "pmcid": (row.get("pmcid") or "").strip(),
        "issn": (row.get("issn") or "").strip(),
        "language": (row.get("language") or "").strip(),
        "affiliations": array_field(row.get("affiliations", "")),
        "publication_types": array_field(row.get("publication_types", "")),
        "publication_date": (row.get("publication_date") or "").strip(),
        "electronic_dates": array_field(row.get("electronic_dates", "")),
        "corrections": parse_json(row.get("corrections", ""), []),
        "projects": association_field(row.get("projects", "")),
        "programmes": association_field(row.get("programmes", "")),
        "project_evidence": parse_json(row.get("project_evidence", ""), {}),
        "programme_evidence": parse_json(row.get("programme_evidence", ""), {}),
        "projects_source": "manual" if row.get("projects_source") == "manual" else "suggested",
        "programmes_source": "manual" if row.get("programmes_source") == "manual" else "suggested",
        "source_keywords": array_field(row.get("source_keywords", "")),
        "affiliation_review": boolean_field(row.get("affiliation_review", "")),
        "record_source": (row.get("record_source") or ("PubMed" if pmid else "Manual")).strip(),
    }


def validate_records(records: list[dict[str, object]]) -> None:
    if not records:
        raise ValueError("The CSV contains no publication records.")
    ids: set[str] = set()
    pmids: set[str] = set()
    for index, record in enumerate(records, start=2):
        record_id = str(record["id"])
        pmid = str(record["pmid"])
        if not record_id or not record["title"] or not record["year"]:
            raise ValueError(f"CSV row {index} requires id, title and year.")
        if record_id in ids:
            raise ValueError(f"Duplicate record id: {record_id}")
        if pmid and not pmid.isdigit():
            raise ValueError(f"Invalid PMID on CSV row {index}: {pmid}")
        if pmid and pmid in pmids:
            raise ValueError(f"Duplicate PMID: {pmid}")
        ids.add(record_id)
        if pmid:
            pmids.add(pmid)


class PubMedClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("NCBI_API_KEY", "").strip()
        self.email = os.environ.get("NCBI_EMAIL", "").strip()
        self.delay = 0.12 if self.api_key else 0.38
        self.last_request = 0.0

    def request(self, endpoint: str, parameters: dict[str, str]) -> bytes:
        params = {"db": "pubmed", "tool": "CKR_Publication_Register", **parameters}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        url = EUTILS_BASE + endpoint + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            elapsed = time.monotonic() - self.last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            request = urllib.request.Request(url, headers={"User-Agent": "CKR-Publication-Register/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = response.read()
                self.last_request = time.monotonic()
                return payload
            except (urllib.error.URLError, TimeoutError) as error:
                self.last_request = time.monotonic()
                if attempt == 2:
                    raise RuntimeError(f"PubMed request failed after three attempts: {error}") from error
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("PubMed request failed.")

    def search_ids(self) -> tuple[list[str], int]:
        payload = self.request("esearch.fcgi", {"term": PUBMED_QUERY, "retmode": "json", "retmax": "9999"})
        data = json.loads(payload)
        result = data.get("esearchresult") or {}
        ids = result.get("idlist") or []
        count = int(result.get("count") or 0)
        if not count or len(ids) != count or len(ids) != len(set(ids)):
            raise RuntimeError(
                f"PubMed search returned {len(ids)} identifiers for {count} results. "
                "The CSV was not changed."
            )
        return ids, count

    def fetch_articles(self, pmids: list[str]) -> list[ET.Element]:
        payload = self.request("efetch.fcgi", {"id": ",".join(pmids), "retmode": "xml"})
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as error:
            raise RuntimeError("PubMed returned invalid XML. The CSV was not changed.") from error
        if root.find(".//ERROR") is not None:
            raise RuntimeError("PubMed returned an error. The CSV was not changed.")
        return root.findall(".//PubmedArticle")


def suggested_associations(title: str, abstract: str, keywords: list[str], collective: str) -> dict[str, object]:
    result: dict[str, object] = {
        "projects": [],
        "programmes": [],
        "project_evidence": {},
        "programme_evidence": {},
    }
    sources = {
        "title": title,
        "abstract": abstract,
        "keywords": " ".join(keywords),
        "collective author": collective,
    }
    for kind, patterns, evidence_key in (
        ("projects", PROJECT_PATTERNS, "project_evidence"),
        ("programmes", PROGRAMME_PATTERNS, "programme_evidence"),
    ):
        names = result[kind]
        evidence_map = result[evidence_key]
        assert isinstance(names, list) and isinstance(evidence_map, dict)
        for name, pattern in patterns.items():
            evidence = []
            for source, content in sources.items():
                match = pattern.search(content)
                if match:
                    evidence.append(f"{source}: {match.group(0)}")
            if evidence:
                names.append(name)
                evidence_map[name] = evidence
    return result


def parse_article(item: ET.Element) -> dict[str, object]:
    citation = item.find("./MedlineCitation")
    article = citation.find("./Article") if citation is not None else None
    if citation is None or article is None:
        raise RuntimeError("PubMed returned an unsupported article format.")

    pmid = find_text(citation, "./PMID")
    title = find_text(article, "./ArticleTitle")
    abstract = " ".join(node_text(node) for node in article.findall("./Abstract/AbstractText"))
    keywords = [node_text(node) for node in citation.findall("./KeywordList/Keyword") if node_text(node)]
    collective = " ".join(node_text(node) for node in article.findall("./AuthorList/Author/CollectiveName"))

    authors = []
    for author in article.findall("./AuthorList/Author"):
        corporate = find_text(author, "./CollectiveName")
        if corporate:
            # Match the browser importer and RIS convention for corporate authors.
            authors.append(corporate + ",")
            continue
        last = find_text(author, "./LastName")
        first = find_text(author, "./ForeName") or find_text(author, "./Initials")
        suffix = find_text(author, "./Suffix")
        name = last + (f", {first}" if first else "") + (f" {suffix}" if suffix else "")
        if name:
            authors.append(name)

    article_ids = {}
    for identifier in item.findall("./PubmedData/ArticleIdList/ArticleId"):
        article_ids[identifier.get("IdType", "")] = node_text(identifier)

    pub_date_node = article.find("./Journal/JournalIssue/PubDate")
    publication_date = "".join(node_text(child) for child in (list(pub_date_node) if pub_date_node is not None else []))
    if not publication_date:
        publication_date = node_text(pub_date_node)
    year = find_text(article, "./Journal/JournalIssue/PubDate/Year")
    if not year:
        match = re.search(r"\b(?:19|20)\d{2}\b", publication_date)
        year = match.group(0) if match else ""

    start_page = find_text(article, "./Pagination/StartPage") or find_text(article, "./Pagination/MedlinePgn")
    end_page = find_text(article, "./Pagination/EndPage")
    if "-" in start_page and not end_page:
        start_page, end_page = start_page.split("-", 1)
    if not start_page:
        for location in article.findall("./ELocationID"):
            if location.get("EIdType") == "pii":
                start_page = node_text(location)
                break

    all_affiliations = []
    for affiliation in article.findall(".//Affiliation"):
        value = node_text(affiliation)
        if value and value not in all_affiliations:
            all_affiliations.append(value)
    ckr_affiliations = [
        value for value in all_affiliations if CKR_AFFILIATION.search(value) and CKR_LOCATION.search(value)
    ]

    electronic_dates = []
    for article_date in article.findall("./ArticleDate"):
        parts = [find_text(article_date, f"./{name}") for name in ("Year", "Month", "Day")]
        electronic_dates.append("-".join(parts))

    corrections = []
    for correction in citation.findall("./CommentsCorrectionsList/CommentsCorrections"):
        corrections.append({"type": correction.get("RefType"), "pmid": find_text(correction, "./PMID")})

    associations = suggested_associations(title, abstract, keywords, collective)
    record: dict[str, object] = {
        "id": pmid,
        "pmid": pmid,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": find_text(article, "./Journal/Title"),
        "abbreviation": find_text(article, "./Journal/ISOAbbreviation"),
        "volume": find_text(article, "./Journal/JournalIssue/Volume"),
        "issue": find_text(article, "./Journal/JournalIssue/Issue"),
        "start_page": start_page,
        "end_page": end_page,
        "doi": article_ids.get("doi", ""),
        "pmcid": article_ids.get("pmc", ""),
        "issn": find_text(article, "./Journal/ISSN"),
        "language": find_text(article, "./Language"),
        "affiliations": ckr_affiliations or all_affiliations,
        "publication_types": [node_text(node) for node in article.findall("./PublicationTypeList/PublicationType")],
        "publication_date": publication_date,
        "electronic_dates": electronic_dates,
        "corrections": corrections,
        **associations,
        "projects_source": "suggested",
        "programmes_source": "suggested",
        "source_keywords": keywords,
        "affiliation_review": False,
        "record_source": "PubMed",
        "_has_ckr_affiliation": bool(ckr_affiliations),
    }
    if not pmid or not title or not year or not record["journal"]:
        raise RuntimeError(f"PubMed record {pmid or '(unknown)'} is incomplete. The CSV was not changed.")
    return record


def preserve_curated_associations(old: dict[str, object] | None, fresh: dict[str, object]) -> dict[str, object]:
    if old is None:
        return fresh
    fresh["id"] = old["id"]
    for kind, evidence_key in (
        ("projects", "project_evidence"),
        ("programmes", "programme_evidence"),
    ):
        if old[f"{kind}_source"] == "manual":
            fresh[kind] = old[kind]
            fresh[evidence_key] = old[evidence_key]
            fresh[f"{kind}_source"] = "manual"
    return fresh


def record_to_row(
    record: dict[str, object], checked_at: str, fieldnames: list[str], base: dict[str, str] | None = None
) -> dict[str, str]:
    row = {name: (base or {}).get(name, "") for name in fieldnames}
    values: dict[str, str] = {
        "id": str(record["id"]),
        "pmid": str(record["pmid"]),
        "title": str(record["title"]),
        "authors": compact_json(record["authors"]),
        "year": str(record["year"]),
        "journal": str(record["journal"]),
        "abbreviation": str(record["abbreviation"]),
        "volume": str(record["volume"]),
        "issue": str(record["issue"]),
        "start_page": str(record["start_page"]),
        "end_page": str(record["end_page"]),
        "doi": str(record["doi"]),
        "pmcid": str(record["pmcid"]),
        "issn": str(record["issn"]),
        "language": str(record["language"]),
        "affiliations": compact_json(record["affiliations"]),
        "publication_types": compact_json(record["publication_types"]),
        "publication_date": str(record["publication_date"]),
        "electronic_dates": compact_json(record["electronic_dates"]),
        "corrections": compact_json(record["corrections"]),
        "projects": " | ".join(record["projects"]),
        "programmes": " | ".join(record["programmes"]),
        "project_evidence": compact_json(record["project_evidence"]),
        "programme_evidence": compact_json(record["programme_evidence"]),
        "projects_source": str(record["projects_source"]),
        "programmes_source": str(record["programmes_source"]),
        "source_keywords": compact_json(record["source_keywords"]),
        "affiliation_review": "true" if record["affiliation_review"] else "false",
        "record_source": str(record["record_source"]),
        "collection_checked_at": checked_at,
    }
    for key, value in values.items():
        if key in row:
            row[key] = value
    return row


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]], list[dict[str, object]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError("The CSV is missing required columns: " + ", ".join(missing))
        rows = list(reader)
    records = [row_to_record(row, index) for index, row in enumerate(rows, start=2)]
    validate_records(records)
    return fieldnames, rows, records


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def update(csv_path: Path) -> dict[str, int]:
    fieldnames, existing_rows, existing_records = read_csv(csv_path)
    by_pmid = {str(record["pmid"]): record for record in existing_records if record["pmid"]}
    raw_by_id = {row["id"]: row for row in existing_rows}
    local_records = [record for record in existing_records if not record["pmid"]]

    client = PubMedClient()
    search_ids, source_count = client.search_ids()
    wanted = list(dict.fromkeys(search_ids + list(by_pmid)))
    refreshed: dict[str, dict[str, object]] = {}
    added = 0
    changed = 0
    review = 0

    for start in range(0, len(wanted), BATCH_SIZE):
        batch = wanted[start : start + BATCH_SIZE]
        print(f"Fetching PubMed records {start + 1}-{min(start + BATCH_SIZE, len(wanted))} of {len(wanted)}")
        articles = client.fetch_articles(batch)
        parsed = [parse_article(article) for article in articles]
        seen = [str(record["pmid"]) for record in parsed]
        if len(seen) != len(set(seen)) or set(seen) != set(batch):
            missing = sorted(set(batch) - set(seen))
            raise RuntimeError("PubMed omitted or duplicated requested records: " + ", ".join(missing[:10]))
        for candidate in parsed:
            pmid = str(candidate["pmid"])
            old = by_pmid.get(pmid)
            has_ckr = bool(candidate.pop("_has_ckr_affiliation"))
            if has_ckr:
                candidate = preserve_curated_associations(old, candidate)
                refreshed[pmid] = candidate
                if old is None:
                    added += 1
                elif candidate != old:
                    changed += 1
            elif old is not None:
                retained = dict(old)
                retained["affiliation_review"] = True
                refreshed[pmid] = retained
                review += 1

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result_records = list(refreshed.values()) + local_records
    validate_records(result_records)
    result_records.sort(key=lambda record: (int(str(record["year"])), str(record["id"])), reverse=True)
    output_rows = [
        record_to_row(record, checked_at, fieldnames, raw_by_id.get(str(record["id"])))
        for record in result_records
    ]
    write_csv(csv_path, fieldnames, output_rows)
    return {
        "source_count": source_count,
        "records": len(output_rows),
        "added": added,
        "refreshed": changed,
        "affiliation_review": review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="publications.csv", type=Path, help="Path to publications.csv")
    args = parser.parse_args()
    try:
        summary = update(args.csv)
    except Exception as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1
    print(
        "Update complete: "
        f"{summary['records']} records, {summary['added']} added, "
        f"{summary['refreshed']} refreshed, {summary['affiliation_review']} affiliations to review "
        f"({summary['source_count']} PubMed search results)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
