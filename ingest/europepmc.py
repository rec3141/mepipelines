"""Europe PMC ingest lane — bioRxiv preprints with retrievable full text.

Europe PMC is preferred over bioRxiv's own API because bioRxiv serves metadata and abstracts but
not full text, and `claimed` extraction needs the Methods section. Europe PMC indexes bioRxiv
preprints under `SRC:"PPR"` and serves JATS XML for the open-access subset.

Two API details worth writing down, both cost time to find:

  - The full-text endpoint is `/{id}/fullTextXML` with NO source prefix. The documented-looking
    `/PPR/{id}/fullTextXML` returns 404 with an empty body, which reads like "no full text" rather
    than "wrong URL".
  - `HAS_FT:Y` does not mean the XML is retrievable. Filter on `OPEN_ACCESS:Y AND IN_EPMC:Y` —
    but that is not sufficient either. Measured full-text availability among search hits is only
    28-40% ("metagenome": 11/40; "microbiome AND amplicon": 16/40), and no field in the search
    response predicts which records will resolve. Over-request by roughly 3x and expect 404s.

Responses are cached under ingest/.cache/ keyed by ID. Ingest must be re-runnable without
re-hitting the API, and extraction gets re-run far more often than ingest does.

    python ingest/europepmc.py --search "metagenome assembly binning" --limit 10
    python ingest/europepmc.py --fetch PPR1175142
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("httpx required: pip install -r requirements.txt")

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CACHE = Path(__file__).resolve().parent / ".cache"

# Europe PMC asks for a contactable UA. Being identifiable is the price of using an open API.
HEADERS = {"User-Agent": "mepipelines/0.1 (https://github.com/rec3141/mepipelines)"}

# Be a good citizen: Europe PMC publishes no hard rate limit, so pace requests deliberately.
REQUEST_DELAY_S = 0.4

# Section headings that introduce a methods section. bioRxiv preprints are inconsistent — some use
# "Materials and Methods", some "Methods", some bury it under "STAR Methods" or "Experimental
# Procedures". Matched case-insensitively against <title> text.
METHODS_TITLE = re.compile(
    r"^\s*(?:\d+\.?\s*)?"
    r"(materials?\s+and\s+methods?|methods?\s+and\s+materials?|methods?|"
    r"experimental\s+procedures?|star\s*\+?\s*methods?|"
    r"materials?|methodology)"
    r"\s*$",
    re.IGNORECASE,
)

# Within a methods section, these subsections carry the bioinformatics chain. Used to trim wet-lab
# and ethics prose when a full methods section is too long to hand to the model whole.
COMPUTATIONAL_HINT = re.compile(
    r"bioinformatic|sequenc\w+\s+(?:read\s+)?process|read\s+process|data\s+process|"
    r"assembl|binning|annotation|taxonom|amplicon|metagenom|statistic|analys[ie]s|"
    r"quality\s+control|quality\s+filter|computational",
    re.IGNORECASE,
)


@dataclass
class Preprint:
    id: str
    doi: str
    title: str
    authors: str
    date: str
    journal: str
    is_open_access: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _cache_path(kind: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:120]
    return CACHE / kind / f"{safe}.cache"


def _get(url: str, params: dict | None, cache_kind: str, cache_key: str, is_json: bool):
    """GET with on-disk caching. Cache hits do not touch the network or the rate limiter."""
    path = _cache_path(cache_kind, cache_key)
    if path.exists():
        raw = path.read_text()
        return json.loads(raw) if is_json else raw

    time.sleep(REQUEST_DELAY_S)
    response = httpx.get(url, params=params, headers=HEADERS, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    raw = response.text

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    return json.loads(raw) if is_json else raw


def search(query: str, limit: int = 25, preprints_only: bool = True) -> list[Preprint]:
    """Search Europe PMC, restricted to records whose full text we can actually retrieve."""
    clauses = [f"({query})", "OPEN_ACCESS:Y", "IN_EPMC:Y"]
    if preprints_only:
        clauses.insert(1, 'SRC:"PPR"')
    full_query = " AND ".join(clauses)

    data = _get(
        f"{BASE}/search",
        {
            "query": full_query,
            "format": "json",
            "pageSize": min(limit, 100),
            "resultType": "core",
        },
        "search",
        f"{full_query}|{limit}",
        is_json=True,
    )

    out = []
    for r in data.get("resultList", {}).get("result", [])[:limit]:
        out.append(
            Preprint(
                id=r.get("id", ""),
                doi=r.get("doi", ""),
                title=" ".join((r.get("title") or "").split()),
                authors=r.get("authorString", ""),
                date=r.get("firstPublicationDate", "") or r.get("pubYear", ""),
                journal=r.get("journalTitle", "") or r.get("bookOrReportDetails", {}).get("publisher", "preprint"),
                is_open_access=r.get("isOpenAccess") == "Y",
            )
        )
    return out


def fetch_fulltext(paper_id: str) -> str | None:
    """JATS XML for one record, or None when Europe PMC has no XML for it.

    Note the endpoint takes NO source prefix: `/{id}/fullTextXML`, not `/PPR/{id}/fullTextXML`.

    A 404 here is an ORDINARY OUTCOME, not an error — measured availability is only 28-40% of
    search hits (see module docstring), and crucially *nothing in the search metadata predicts
    it*. Two records identical on `inEPMC`, `isOpenAccess`, `hasTextMinedTerms`, `hasPDF` and
    `fullTextIdList` can differ on whether the XML exists. The only reliable test is the fetch, so
    callers must over-request and expect roughly two thirds to yield nothing.
    """
    try:
        return _get(f"{BASE}/{paper_id}/fullTextXML", None, "fulltext", paper_id, is_json=False)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _strip_tags(xml_fragment: str) -> str:
    """JATS -> plain text, preserving sentence boundaries.

    Deliberately regex-based rather than a real XML parse: we want a robust best-effort on
    fragments that may be malformed, and we only need prose, not structure. Tables and figures are
    dropped entirely — a table of sample metadata is noise for chain extraction and burns context.
    """
    text = re.sub(r"<(table-wrap|fig|supplementary-material)\b.*?</\1>", " ", xml_fragment, flags=re.DOTALL)
    text = re.sub(r"<xref\b.*?</xref>", "", text, flags=re.DOTALL)   # citation markers
    text = re.sub(r"</(p|title|sec)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&#x2019;", "'").replace("&#x2018;", "'")
        .replace("&#x201c;", '"').replace("&#x201d;", '"')
        .replace("&#x2013;", "-").replace("&#x2014;", "-").replace("&#xa0;", " ")
    )
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_methods(xml: str) -> str | None:
    """Pull the Methods section out of a JATS document.

    Returns None when no methods-like section is found — a real and common outcome for preprints
    that put methods in a supplement. Returning None rather than the whole paper matters: handing
    an extractor an entire article invites it to assemble a chain from the Results narrative.
    """
    # <sec> blocks are nested; find each top-level-ish sec and test its first <title>.
    for match in re.finditer(r"<sec\b[^>]*>", xml):
        start = match.start()
        title_match = re.search(r"<title>(.*?)</title>", xml[start : start + 4000], re.DOTALL)
        if not title_match:
            continue
        title = _strip_tags(title_match.group(1)).strip()
        if not METHODS_TITLE.match(title):
            continue

        # Walk to the matching </sec>, counting nesting.
        depth, i = 0, start
        for tag in re.finditer(r"<sec\b[^>]*>|</sec>", xml[start:]):
            depth += 1 if tag.group(0).startswith("<sec") else -1
            if depth == 0:
                i = start + tag.end()
                break
        else:
            i = len(xml)

        body = _strip_tags(xml[start:i])
        if len(body) > 200:
            return body
    return None


def computational_subsections(methods: str, max_chars: int = 9000) -> str:
    """Trim a methods section toward its computational parts.

    Long methods sections are mostly wet lab, ethics, and sampling. Those cost context and give the
    extractor more chances to mint spurious steps. When a section is short enough, it is returned
    unchanged — trimming is a last resort, not a default, because a heading-based filter can drop
    a chain step that lives under an unexpected subheading.
    """
    if len(methods) <= max_chars:
        return methods

    paragraphs = [p for p in methods.split("\n") if p.strip()]
    kept = [p for p in paragraphs if COMPUTATIONAL_HINT.search(p)]
    trimmed = "\n".join(kept) if kept else methods
    return trimmed[:max_chars]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--search", help="Europe PMC query string")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--fetch", help="fetch and show the methods section for one ID")
    ap.add_argument("--all-sources", action="store_true", help="do not restrict to preprints")
    args = ap.parse_args()

    if args.fetch:
        xml = fetch_fulltext(args.fetch)
        if xml is None:
            print(f"{args.fetch}: no full text XML in Europe PMC")
            return 1
        methods = extract_methods(xml)
        if not methods:
            print(f"{args.fetch}: no methods section found ({len(xml)} chars of XML)")
            return 1
        print(f"{args.fetch}: methods {len(methods)} chars\n")
        print(methods[:4000])
        return 0

    if args.search:
        hits = search(args.search, args.limit, preprints_only=not args.all_sources)
        print(f"{len(hits)} result(s)\n")
        for p in hits:
            print(f"  {p.id}  {p.date[:10]:<11} {p.title[:80]}")
        return 0

    ap.error("need --search or --fetch")


if __name__ == "__main__":
    sys.exit(main())
