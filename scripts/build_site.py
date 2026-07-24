"""Build the static chain explorer in site/.

The explorer is the survey's front end: point it at extraction output and it renders every chain,
the per-role consensus, and the ontology's blind spots. It is regenerated from data, never edited
by hand — `site/index.html` is the app, `site/data.js` is generated and gitignored-by-convention
only in the sense that it is cheap to rebuild (it IS committed, because GitHub Pages serves it).

Data is emitted as `data.js` assigning `window.MEP_DATA`, not `data.json` fetched at runtime.
That keeps the page working from `file://` as well as from Pages — a fetch() of a relative JSON
path is blocked by CORS on file://, which would make local inspection impossible.

    python scripts/build_site.py --from extractions.json
    python scripts/build_site.py --from a.json --from b.json      # merge several runs
    python scripts/build_site.py --catalog                        # (Phase 6) from catalog records
    python -m http.server -d site 8000                            # preview locally

Publishing: GitHub Pages serving from the `site/` directory on the default branch, or copy
site/ to a gh-pages branch. Nothing here needs a build toolchain — it is one HTML file plus data.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from try_biorxiv import load_alias_index, match_tool  # noqa: E402

ONTOLOGY = ROOT / "schema" / "ontology"
SITE = ROOT / "site"

# The ontology has nine stages; the validated categorical palette carries eight slots. `acquire`
# and `preprocess` both handle raw reads before any biological inference, so they merge into one
# band rather than dropping a stage or cycling a ninth hue (which the palette rules forbid).
STAGE_MERGE = {"acquire": "reads", "preprocess": "reads"}
STAGE_ORDER = ["reads", "amplicon", "assembly", "reference", "annotate", "ecology", "specialty", "deliver"]

# Roles seen fewer than this many times say more about sampling noise than about the field.
MIN_USES_FOR_CONSENSUS = 3


def clean(s: str | None) -> str:
    """Strip inline markup and collapse whitespace. Preprint titles carry <i> tags."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def load_ontology() -> tuple[dict, dict]:
    doc = yaml.safe_load((ONTOLOGY / "step_roles.yaml").read_text())
    stage_of = {r["id"]: r["stage"] for r in doc["roles"]}
    substitutable = {r["id"]: bool(r.get("substitutable")) for r in doc["roles"]}
    return stage_of, substitutable


def build(records: list[dict]) -> dict:
    stage_of, substitutable = load_ontology()
    alias_index = load_alias_index()

    def stage(role: str) -> str:
        s = stage_of.get(role, "specialty")
        return STAGE_MERGE.get(s, s)

    papers, empty = [], 0
    for rec in records:
        steps = rec.get("chain", {}).get("steps", [])
        if not steps:
            empty += 1          # counted and surfaced, not silently dropped
            continue
        meta = rec.get("paper", {})
        papers.append({
            "id": meta.get("id", ""),
            "title": clean(meta.get("title")),
            "date": (meta.get("date") or "")[:10],
            "doi": meta.get("doi", ""),
            "completeness": rec["chain"].get("completeness"),
            "steps": [{
                "role": s["role"],
                "stage": stage(s["role"]),
                "tool": clean(s.get("tool_raw")) or None,
                # Recomputed here, never trusted from the input file: the ontology grows between
                # runs and a stale match would understate coverage.
                "matched": match_tool(s.get("tool_raw"), alias_index),
                "version": s.get("version"),
                "params": clean(s.get("params"))[:200] or None,
            } for s in steps],
        })

    papers.sort(key=lambda p: -len(p["steps"]))

    per_role: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in papers:
        for s in p["steps"]:
            key = s["matched"] or (s["tool"] or "").lower()
            if key:
                per_role[s["role"]][key] += 1

    roles = []
    for role, counter in per_role.items():
        n = sum(counter.values())
        # `custom` is by definition heterogeneous — its concentration is a statement about our
        # ontology's gaps, not about the field's habits, so it is reported separately.
        if n < MIN_USES_FOR_CONSENSUS or role == "custom":
            continue
        top, top_n = counter.most_common(1)[0]
        roles.append({
            "role": role,
            "stage": stage(role),
            "n": n,
            "concentration": round(top_n / n, 3),
            "top": top,
            "substitutable": substitutable.get(role, True),
            "tools": [{"tool": t, "n": v} for t, v in counter.most_common()],
        })
    roles.sort(key=lambda r: (-r["concentration"], -r["n"]))

    named = [s for p in papers for s in p["steps"] if s["tool"]]
    unmatched = sorted({s["tool"] for s in named if not s["matched"]}, key=str.lower)

    return {
        "papers": papers,
        "roles": roles,
        "stageOrder": STAGE_ORDER,
        "unmatched": unmatched,
        "summary": {
            "papersPlotted": len(papers),
            "papersEmpty": empty,
            "steps": sum(len(p["steps"]) for p in papers),
            "namedTools": len(named),
            "matchRate": round(sum(1 for s in named if s["matched"]) / len(named), 3) if named else 0,
            "distinctRoles": len({s["role"] for p in papers for s in p["steps"]}),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="sources", action="append", type=Path, default=[],
                    help="extraction JSON from scripts/try_biorxiv.py --json (repeatable)")
    ap.add_argument("--catalog", action="store_true",
                    help="(not yet implemented) build from catalog/workflows/*.yaml")
    ap.add_argument("--out", type=Path, default=SITE / "data.js")
    args = ap.parse_args()

    if args.catalog:
        # Deliberately not stubbed with silence: the catalog has no committed records yet, and
        # emitting an empty site would look like a working build rather than an empty input.
        print("--catalog is not implemented yet: no vetted records exist to build from.")
        print("Use --from with extraction output until Phase 6.")
        return 2

    if not args.sources:
        ap.error("need --from <extractions.json> (repeatable)")

    records: list[dict] = []
    seen: set[str] = set()
    for path in args.sources:
        loaded = json.loads(path.read_text())
        for rec in loaded:
            pid = rec.get("paper", {}).get("id")
            if pid and pid in seen:      # merging runs must not double-count a paper
                continue
            if pid:
                seen.add(pid)
            records.append(rec)
        print(f"  read {len(loaded):>3} record(s) from {path}")

    payload = build(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "// Generated by scripts/build_site.py — do not edit.\n"
        "window.MEP_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n"
    )

    s = payload["summary"]
    print(f"\nwrote {args.out.relative_to(ROOT)} ({args.out.stat().st_size:,} bytes)")
    print(f"  {s['papersPlotted']} papers · {s['steps']} steps · {s['distinctRoles']} roles")
    print(f"  {s['namedTools']} named tools, {s['matchRate']:.0%} matched, "
          f"{len(payload['unmatched'])} unmatched")
    if s["papersEmpty"]:
        print(f"  {s['papersEmpty']} record(s) had no steps and were excluded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
