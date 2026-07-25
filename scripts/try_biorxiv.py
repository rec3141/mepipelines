"""End-to-end smoke run: real bioRxiv preprints -> `claimed` chains.

Search Europe PMC for bioRxiv preprints, pull their Methods sections, and run the claimed
extractor over them. This is not a scored benchmark — there are no gold labels for these papers.
It answers three questions the synthetic fixtures cannot:

  1. Does Methods extraction survive real JATS from real preprints?
  2. Does the extractor produce sane chains on prose nobody wrote for it?
  3. How much of the tooling in real papers does `tools.yaml` already know?

(3) is the actionable one. Every unmatched tool is a candidate ontology entry, and the match rate
is a direct measure of how far the seeded alias table gets us.

    python scripts/try_biorxiv.py --query "metagenome assembly binning" --limit 5
    python scripts/try_biorxiv.py --ids PPR1175142 --show-methods
    python scripts/try_biorxiv.py --limit 8 --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extract"))
sys.path.insert(0, str(ROOT / "ingest"))

import europepmc  # noqa: E402
from from_text import extract  # noqa: E402
from llm import LLMClient  # noqa: E402

ONTOLOGY = ROOT / "schema" / "ontology"

DEFAULT_QUERY = (
    "metagenome AND (assembly OR binning OR amplicon) AND (microbiome OR microbial)"
)


def load_alias_index() -> tuple[dict[str, str], dict[str, str]]:
    """Return (tool_index, database_index), kept SEPARATE on purpose.

    Merging them lets a database name satisfy a tool lookup — "GTDB" scored as a tool for
    `taxonomy_assign`, "CARD" as a tool for `detect_amr`. Both are resources a step consumes, not
    software it runs, and counting them as recognised tools inflates the coverage number.
    """
    doc = yaml.safe_load((ONTOLOGY / "tools.yaml").read_text())

    def index(entries):
        out: dict[str, str] = {}
        for entry in entries:
            for alias in [entry["id"], entry.get("name", "")] + (entry.get("aliases") or []):
                if alias:
                    out[normalize(alias)] = entry["id"]
        return out

    return index(doc["tools"]), index(doc.get("databases", []))


def normalize(name: str) -> str:
    """Lowercase, strip punctuation and spaces — matches tools.yaml's stated alias convention."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _candidates(tool_raw: str) -> list[str]:
    """Normalized forms to try, longest first.

    Token-aligned rather than free substring. A raw `alias in name` test produced real
    false positives on actual papers: "Picard" matched `card`, "wfmash" matched `mash`, and
    "Naive Bayes classifier" matched `veba` — the letters hide inside "nai(veBa)yes". Anchoring to
    token boundaries removes that whole class.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", tool_raw) if t]
    out: list[str] = []
    # Contiguous runs, longest first, so "bwa mem" beats "bwa".
    for size in range(min(4, len(tokens)), 0, -1):
        for i in range(len(tokens) - size + 1):
            out.append(normalize("".join(tokens[i:i + size])))
    return out


def match_tool(tool_raw: str | None, index: dict[str, str]) -> str | None:
    """Map a raw tool name onto a canonical id, or None."""
    if not tool_raw:
        return None
    key = normalize(tool_raw)
    if key in index:
        return index[key]

    for cand in _candidates(tool_raw):
        if cand in index:
            return index[cand]
        # A trailing version digit glued to the name: "PhyloPhlan3" -> phylophlan. Only when the
        # unstripped form is unknown, so genuinely digit-bearing names (DADA2, MetaBAT2, CheckM2,
        # bwa-mem2) are never truncated.
        stripped = re.sub(r"\d+$", "", cand)
        if stripped != cand and len(stripped) >= 4 and stripped in index:
            return index[stripped]
    return None


def run_one(paper, client, alias_index, show_methods: bool) -> dict | None:
    print(f"\n{'=' * 78}\n{paper.id}  {paper.date[:10]}\n{paper.title[:76]}\n{'=' * 78}")

    try:
        xml = europepmc.fetch_fulltext(paper.id)
    except Exception as exc:  # noqa: BLE001
        print(f"  fetch failed: {type(exc).__name__}: {exc}")
        return None
    if xml is None:
        # Expected for ~60-70% of hits and unpredictable from metadata. Not a defect.
        print("  no full text XML in Europe PMC — skipped")
        return None

    methods = europepmc.extract_methods(xml)
    if not methods:
        # Common and worth counting: many preprints put methods in a supplement.
        print(f"  no methods section found ({len(xml)} chars of XML) — skipped")
        return None

    trimmed = europepmc.computational_subsections(methods)
    print(f"  methods {len(methods)} chars -> {len(trimmed)} after trim")
    if show_methods:
        print("\n" + "\n".join(f"  | {ln}" for ln in trimmed.splitlines()[:40]) + "\n")

    try:
        out = extract(trimmed, client)
    except Exception as exc:  # noqa: BLE001
        print(f"  extraction failed: {type(exc).__name__}: {exc}")
        return None

    chain = out["chain"]
    steps = chain.get("steps", [])
    print(f"  completeness={chain.get('completeness')}  steps={len(steps)}  "
          f"{out['stats']['elapsed_s']}s")

    known, unknown = [], []
    for s in steps:
        canonical = match_tool(s.get("tool_raw"), alias_index)
        s["_matched"] = canonical
        if s.get("tool_raw"):
            (known if canonical else unknown).append(s["tool_raw"])
        mark = "  " if canonical or not s.get("tool_raw") else "??"
        tool = s.get("tool_raw") or "—"
        ver = f" v{s['version']}" if s.get("version") else ""
        canon = f"  -> {canonical}" if canonical else ""
        print(f"   {mark} {s['order']:>2}. {s['role']:<22} {tool}{ver}{canon}")

    return {
        "paper": paper.as_dict(),
        "methods_chars": len(methods),
        "chain": chain,
        "provenance": out["provenance"],
        "stats": out["stats"],
        "tools_known": known,
        "tools_unknown": unknown,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--ids", nargs="+", help="specific Europe PMC IDs instead of a search")
    ap.add_argument("--show-methods", action="store_true")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    tool_index, db_index = load_alias_index()
    client = LLMClient(**({"model": args.model} if args.model else {}))
    print(f"model: {client.provider}:{client.model}")
    print(f"ontology: {len(tool_index)} tool aliases, {len(db_index)} database aliases")

    if args.ids:
        papers = []
        for pid in args.ids:
            hits = europepmc.search(f"EXT_ID:{pid}", 1)
            if hits:
                papers.append(hits[0])
            else:
                print(f"  {pid}: not found")
    else:
        print(f"query: {args.query}")
        papers = europepmc.search(args.query, args.limit)
    print(f"{len(papers)} paper(s)")

    results = [r for p in papers if (r := run_one(p, client, tool_index, args.show_methods))]

    print(f"\n{'=' * 78}\nSUMMARY")
    print(f"  {len(results)}/{len(papers)} papers yielded a chain")
    if results:
        steps = sum(len(r["chain"]["steps"]) for r in results)
        known = sum(len(r["tools_known"]) for r in results)
        unknown_all = [t for r in results for t in r["tools_unknown"]]
        total_named = known + len(unknown_all)
        rate = f"{known / total_named:.0%}" if total_named else "n/a"
        print(f"  {steps} steps, {total_named} named tools, {rate} matched to tools.yaml")
        print(f"  {sum(r['stats']['elapsed_s'] for r in results):.0f}s total")

        if unknown_all:
            print(f"\n  {len(set(unknown_all))} unmatched tool name(s) — ontology candidates:")
            for name in sorted(set(unknown_all), key=str.lower):
                print(f"    {name}")

    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
