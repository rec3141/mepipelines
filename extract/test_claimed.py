"""Run the `claimed` extractor against the hand-labeled fixture set.

This is the harness for Phase 3's exit criterion. It reports per-fixture and per-assertion results
so a model change shows up as a specific regression rather than a vibe.

    python extract/test_claimed.py                  # all fixtures
    python extract/test_claimed.py --id vague_opaque_suite
    python extract/test_claimed.py --model qwen/qwen3-coder-next
    python extract/test_claimed.py --repeat 3       # stability across identical runs
    python extract/test_claimed.py --json out.json  # save raw extractions for inspection

Assertions are deliberately coarse — role presence/absence, ordering, substring hits. Fine-grained
scoring against a gold chain comes when the labeled set is bigger; right now the job is catching
the failure modes that would poison the catalog, chiefly hallucinated steps.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from from_text import extract  # noqa: E402
from llm import LLMClient  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claimed.yaml"


def check(chain: dict, expect: dict) -> list[tuple[str, bool, str]]:
    """Evaluate one fixture's assertions. Returns (label, passed, detail) per assertion."""
    steps = chain.get("steps", [])
    roles = [s.get("role") for s in steps]
    tools = " | ".join((s.get("tool_raw") or "") for s in steps).lower()
    # params/database are where numeric choices and DB releases land; search both.
    freetext = " | ".join(
        f"{s.get('params') or ''} {s.get('database') or ''}" for s in steps
    ).lower()

    results: list[tuple[str, bool, str]] = []

    for role in expect.get("roles_present", []):
        results.append((f"role present: {role}", role in roles, ""))

    for role in expect.get("roles_absent", []):
        # The hallucination traps. A failure here is the expensive kind.
        hit = role in roles
        results.append((f"role ABSENT: {role}", not hit, "hallucinated" if hit else ""))

    # For clauses where more than one role assignment is genuinely defensible — "genes were
    # predicted and annotated with Prokka" supports predict_genes, annotate_function, or both.
    # Forcing a single reading would test our preference, not the extractor.
    for group in expect.get("roles_any_of", []):
        hit = [r for r in group if r in roles]
        results.append(
            (f"any of: {'/'.join(group)}", bool(hit), "" if hit else "none present")
        )

    for tool in expect.get("tools_present", []):
        results.append((f"tool present: {tool}", tool.lower() in tools, ""))

    for tool in expect.get("tools_absent", []):
        results.append((f"tool ABSENT: {tool}", tool.lower() not in tools, ""))

    # Numbers get written back in whatever form the model chose — "8,000", "8000", or inside a
    # JSON-ish string like '{"depth": 5000}'. The assertion is about whether the VALUE was
    # captured, not how it was punctuated, so strip thousands separators from both sides.
    def _loose(s: str) -> str:
        return s.lower().replace(",", "").replace(" ", "")

    for frag in expect.get("text_anywhere", []):
        found = frag.lower() in freetext or _loose(frag) in _loose(freetext)
        results.append((f"captured: {frag!r}", found, ""))

    if "min_steps" in expect:
        n = expect["min_steps"]
        results.append((f"at least {n} steps", len(steps) >= n, f"got {len(steps)}"))

    if "max_steps" in expect:
        n = expect["max_steps"]
        # Over-expansion on a vague paragraph is the QIIME2-opacity failure.
        results.append((f"at most {n} steps", len(steps) <= n, f"got {len(steps)}"))

    got_completeness = chain.get("completeness")
    if "completeness" in expect:
        want = expect["completeness"]
        results.append(
            (f"completeness == {want}", got_completeness == want, f"got {got_completeness}")
        )
    if "completeness_in" in expect:
        want = expect["completeness_in"]
        results.append(
            (
                f"completeness in {want}",
                got_completeness in want,
                f"got {got_completeness}",
            )
        )

    if "role_order" in expect:
        first, second = expect["role_order"]
        if first in roles and second in roles:
            ok = max(i for i, r in enumerate(roles) if r == first) < min(
                i for i, r in enumerate(roles) if r == second
            )
            detail = "" if ok else f"order was {' -> '.join(roles)}"
        else:
            ok, detail = False, f"one of {first}/{second} missing"
        results.append((f"order: {first} before {second}", ok, detail))

    return results


def run_fixture(fixture: dict, client: LLMClient, verbose: bool) -> dict:
    out = extract(fixture["methods"], client)
    chain = out["chain"]
    results = check(chain, fixture.get("expect", {}))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)

    print(f"\n{'=' * 78}\n{fixture['id']}  —  {passed}/{total} assertions\n{'=' * 78}")

    if verbose:
        print(f"completeness: {chain.get('completeness')}   steps: {len(chain.get('steps', []))}")
        for s in chain.get("steps", []):
            tool = s.get("tool_raw") or "—"
            ver = f" v{s['version']}" if s.get("version") else ""
            print(f"  {s['order']:>2}. {s['role']:<24} {tool}{ver}")
            for key in ("params", "database"):
                if s.get(key):
                    print(f"        {key}: {s[key]}")

    for label, ok, detail in results:
        if not ok or verbose:
            mark = "PASS" if ok else "FAIL"
            print(f"  {mark}  {label}" + (f"   ({detail})" if detail else ""))

    return {
        "id": fixture["id"],
        "passed": passed,
        "total": total,
        "results": results,
        "chain": chain,
        "provenance": out["provenance"],
        "stats": out["stats"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", action="append", help="run only these fixture ids")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--repeat", type=int, default=1, help="runs per fixture (stability check)")
    ap.add_argument("--json", type=Path, help="write raw extractions here")
    ap.add_argument("-q", "--quiet", action="store_true", help="only show failures")
    args = ap.parse_args()

    fixtures = yaml.safe_load(FIXTURES.read_text())
    if args.id:
        wanted = set(args.id)
        fixtures = [f for f in fixtures if f["id"] in wanted]
        if not fixtures:
            print(f"no fixtures matched {sorted(wanted)}")
            return 1

    kwargs = {}
    if args.provider:
        kwargs["provider"] = args.provider
    if args.model:
        kwargs["model"] = args.model
    client = LLMClient(**kwargs)

    print(f"model   : {client.provider}:{client.model}")
    print(f"fixtures: {len(fixtures)}  x{args.repeat}")

    runs, failures, errors = [], [], []
    for rep in range(args.repeat):
        if args.repeat > 1:
            print(f"\n########## run {rep + 1}/{args.repeat} ##########")
        for fixture in fixtures:
            # A long batch must survive one bad call. LM Studio unloads idle models, and a
            # reasoning model can blow its token ceiling on an unusually long chain — neither
            # should discard the runs that already succeeded.
            try:
                result = run_fixture(fixture, client, verbose=not args.quiet)
            except Exception as exc:  # noqa: BLE001 — report and carry on
                print(f"\n{'=' * 78}\n{fixture['id']}  —  ERROR\n{'=' * 78}")
                print(f"  {type(exc).__name__}: {exc}")
                errors.append((fixture["id"], f"{type(exc).__name__}: {exc}"))
                continue
            runs.append(result)
            failures.extend(
                (fixture["id"], label) for label, ok, _ in result["results"] if not ok
            )

    total_pass = sum(r["passed"] for r in runs)
    total_all = sum(r["total"] for r in runs)
    elapsed = sum(r["stats"]["elapsed_s"] for r in runs)
    tokens = sum(r["stats"]["completion_tokens"] for r in runs)
    reasoning = sum(r["stats"].get("reasoning_tokens", 0) for r in runs)

    print(f"\n{'=' * 78}")
    print(f"TOTAL  {total_pass}/{total_all} assertions across {len(runs)} run(s)")
    share = f" ({reasoning} reasoning, {reasoning / tokens:.0%})" if tokens and reasoning else ""
    print(f"       {elapsed:.0f}s, {tokens} output tokens{share}, {elapsed / max(len(runs), 1):.0f}s/run")

    if errors:
        print(f"\n{len(errors)} fixture(s) errored and were skipped:")
        for fid, msg in errors:
            print(f"  {fid}: {msg}")

    if failures:
        print(f"\n{len(failures)} failing assertion(s):")
        for (fid, label), n in Counter(failures).most_common():
            suffix = f"  (x{n}/{args.repeat})" if args.repeat > 1 else ""
            print(f"  {fid}: {label}{suffix}")

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {k: v for k, v in r.items() if k != "results"}
                    for r in runs
                ],
                indent=2,
            )
        )
        print(f"\nwrote {args.json}")

    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
