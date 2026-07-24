"""Methods text -> `claimed` chain.

Half of the dual extraction. This module must never be given the repository, the code, or the
`observed` chain — see extract/prompts/claimed_v1.md for why.

The JSON schema handed to the model is built from schema/ontology/step_roles.yaml at call time, so
the role enum the model is constrained to and the role vocabulary the validator enforces cannot
drift apart.

    python extract/from_text.py --demo              # run the built-in fixture
    python extract/from_text.py --file methods.txt  # extract from a file
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import LLMClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY = ROOT / "schema" / "ontology"
PROMPTS = Path(__file__).resolve().parent / "prompts"

PROMPT_VERSION = "claimed_v1"

# Override with MEP_EXTRACTION_MAX_TOKENS when running a model with a tighter context.
EXTRACTION_MAX_TOKENS = int(os.getenv("MEP_EXTRACTION_MAX_TOKENS", "32000"))


def load_roles() -> list[dict]:
    doc = yaml.safe_load((ONTOLOGY / "step_roles.yaml").read_text())
    return doc["roles"]


def roles_block(roles: list[dict]) -> str:
    """Compact role reference for the prompt — id, stage, description, and example tools.

    `typical_tools` is the single strongest disambiguation signal we have and it is already
    curated in the ontology, so it belongs in the prompt. Omitting it sent Filtlong to `custom`
    on every run even though `trim` lists filtlong explicitly. The examples are illustrative, not
    exhaustive — the prompt says so — but they anchor the common cases hard.
    """
    lines = []
    for r in roles:
        desc = " ".join(r["description"].split())
        line = f"- `{r['id']}` ({r['stage']}): {desc}"
        tools = r.get("typical_tools") or []
        if tools:
            line += f"  [e.g. {', '.join(tools[:10])}]"
        lines.append(line)
    return "\n".join(lines)


def build_schema(roles: list[dict]) -> dict:
    """JSON Schema for one extracted chain, with the role enum bound to the ontology.

    `additionalProperties: false` and full `required` lists throughout: strict structured-output
    modes demand them, and they stop the model inventing fields we would then silently drop.
    Optional-in-spirit fields are modelled as nullable rather than omitted, since strict mode
    requires every property to appear in `required`.
    """
    role_ids = [r["id"] for r in roles]

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["completeness", "steps"],
        "properties": {
            "completeness": {
                "type": "string",
                "enum": ["complete", "partial", "sketch"],
                "description": (
                    "How fully the TEXT describes its own chain — not how confident you are, "
                    "and not whether the chain is scientifically complete. "
                    "complete = the text walks through the analysis start to finish in a clear "
                    "order, even if you think steps are scientifically missing; "
                    "partial = the text describes some stages but visibly skips others, e.g. it "
                    "jumps from raw reads to a final statistic; "
                    "sketch = tools are named without a usable order, as in a single sentence "
                    "listing software with no sequence. "
                    "A long, well-ordered narrative is `complete` — do not downgrade it to "
                    "`sketch` merely because it is long or because versions are absent."
                ),
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["order", "role", "tool_raw", "version", "params", "database", "evidence"],
                    "properties": {
                        "order": {"type": "integer", "minimum": 0},
                        "role": {"type": "string", "enum": role_ids},
                        "tool_raw": {
                            "type": ["string", "null"],
                            "description": (
                                "Tool NAME verbatim as written in the paper, without the version. "
                                "Null when the text names no tool for this step."
                            ),
                        },
                        "version": {
                            "type": ["string", "null"],
                            "description": (
                                "Version only if explicitly stated, without a leading 'v'. "
                                "Null otherwise."
                            ),
                        },
                        "params": {
                            "type": ["string", "null"],
                            "description": (
                                "Any stated setting that would change the result, copied as free "
                                "text. Capture these whenever the text gives them: truncation or "
                                "trimming lengths (e.g. 'truncated to 240 bp forward, 200 bp "
                                "reverse'), rarefaction or subsampling depth (e.g. 'rarefied to "
                                "8,000 reads per sample'), minimum contig or read length, "
                                "clustering or similarity identity, quality thresholds, "
                                "completeness/contamination cutoffs, FDR or significance levels, "
                                "permutation counts, and mode flags such as --meta. "
                                "Do NOT capture thread counts, memory, or values the text calls "
                                "default. Null only when the text gives no such setting for this "
                                "step."
                            ),
                        },
                        "database": {
                            "type": ["string", "null"],
                            "description": "Reference database and release, if stated. Null otherwise.",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "Verbatim quote from the input stating this step.",
                        },
                    },
                },
            },
        },
    }


def build_prompt(methods: str, roles: list[dict]) -> tuple[str, str]:
    """Return (system, user) rendered from the versioned prompt file."""
    raw = (PROMPTS / f"{PROMPT_VERSION}.md").read_text()
    system = raw.split("## System", 1)[1].split("## Roles", 1)[0].strip()
    user_tpl = raw.split("## User", 1)[1].strip()
    system = f"{system}\n\n## Roles\n\n{roles_block(roles)}"
    return system, user_tpl.replace("{methods}", methods)


# Trailing version suffix: "cutadapt v3.4", "DADA2 1.20", "SPAdes (v3.15.5)", "QIIME2 2021.4".
# Anchored to the end so an embedded digit that is part of the name survives — MetaBAT2, DADA2,
# CheckM2, MaxBin2, SemiBin2, vsearch2. Requires a dot or a leading v/V, so bare "2" never matches.
_VERSION_SUFFIX = re.compile(
    r"\s*\(?\s*(?:v(?:er(?:sion)?)?\.?\s*)?"      # optional v / ver / version
    r"(\d+(?:\.\d+)+[a-z0-9._-]*|v\d[a-z0-9._-]*)"  # 1.20 | 3.15.5 | 2021.4 | v3
    r"\s*\)?\s*$",
    re.IGNORECASE,
)


def split_version(tool_raw: str | None, version: str | None) -> tuple[str | None, str | None]:
    """Pull a version suffix out of `tool_raw` into `version`.

    Models reliably ignore the "name only" instruction for tools the paper writes as
    "cutadapt v3.4", and a `tool_raw` carrying a version silently fails alias matching later.
    The pattern is tight enough to handle deterministically, so we do — the prompt instruction
    stays as a first line of defence, this is the backstop.

    An explicit `version` from the model always wins; this only fills a gap.
    """
    if not tool_raw:
        return None, (version or None)

    name = tool_raw.strip()
    match = _VERSION_SUFFIX.search(name)
    if not match:
        return name or None, (version.lstrip("vV") if version else None)

    stripped = name[: match.start()].strip(" .,;(")
    # Refuse to consume the whole string — "3.15.5" alone is not a tool name, so keep the original.
    if not stripped:
        return name, (version.lstrip("vV") if version else None)

    found = match.group(1).lstrip("vV")
    return stripped, (version.lstrip("vV") if version else found)


def normalize_steps(chain: dict) -> dict:
    """Deterministic cleanup applied to every extracted chain before it is returned."""
    for step in chain.get("steps", []):
        name, ver = split_version(step.get("tool_raw"), step.get("version"))
        step["tool_raw"] = name
        step["version"] = ver
    return chain


def extract(methods: str, client: LLMClient | None = None) -> dict:
    """Extract a `claimed` chain. Returns the parsed chain plus provenance."""
    client = client or LLMClient()
    roles = load_roles()
    system, user = build_prompt(methods, roles)

    result = client.complete(
        user,
        system=system,
        schema=build_schema(roles),
        schema_name="claimed_chain",
        prompt_version=PROMPT_VERSION,
        # Generous on purpose. On a reasoning model this budget covers thinking AND output, and
        # gemma-4 spent 8189 tokens reasoning about a 10-step MAG chain before writing anything —
        # at an 8192 ceiling it returned zero content. Long chains are exactly the records worth
        # extracting, so the budget has to clear the worst case, not the median.
        max_tokens=EXTRACTION_MAX_TOKENS,
    )

    chain = normalize_steps(result.parsed)
    chain["source"] = "methods_text"
    return {
        "chain": chain,
        "provenance": result.provenance(),
        "stats": {
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "elapsed_s": result.elapsed_s,
        },
    }


# A realistic amplicon Methods paragraph, written for this test. It deliberately contains the
# things that make extraction hard and that the checks in __main__ probe for:
#   - primer removal stated separately from quality trimming (two distinct roles)
#   - a rarefaction depth and a DADA2 truncation length (result-changing params)
#   - a named database release (SILVA 138.1)
#   - NO host-filtering and NO chimera step, so a model that inserts standard practice is caught
DEMO_METHODS = """\
Sequence data processing. Raw paired-end reads were demultiplexed and primers targeting the V4
region were removed with cutadapt v3.4. Reads were then quality filtered and truncated to 240 bp
(forward) and 200 bp (reverse) using DADA2 v1.20, which was also used to infer amplicon sequence
variants. Taxonomy was assigned to representative sequences using the DADA2 naive Bayesian
classifier against the SILVA 138.1 SSU reference database. The resulting feature table was
rarefied to 8,000 sequences per sample prior to calculation of Shannon diversity and Bray-Curtis
dissimilarity in the vegan R package. Differences in community composition among treatments were
tested by PERMANOVA using adonis2 with 999 permutations.
"""


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, help="file containing Methods text")
    ap.add_argument("--demo", action="store_true", help="run the built-in fixture")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    if args.file:
        methods = args.file.read_text()
    elif args.demo:
        methods = DEMO_METHODS
    else:
        ap.error("need --file or --demo")

    kwargs = {}
    if args.provider:
        kwargs["provider"] = args.provider
    if args.model:
        kwargs["model"] = args.model
    client = LLMClient(**kwargs)

    print(f"model: {client.provider}:{client.model}\n")
    out = extract(methods, client)

    chain = out["chain"]
    print(f"completeness: {chain['completeness']}")
    print(f"{len(chain['steps'])} step(s):\n")
    for s in chain["steps"]:
        bits = [f"  {s['order']:>2}. {s['role']:<24} {s['tool_raw'] or '—'}"]
        if s.get("version"):
            # lstrip guards a model that ignored the no-leading-v instruction
            bits.append(f"v{s['version'].lstrip('vV')}")
        print(" ".join(bits))
        if s.get("params"):
            print(f"      params: {s['params']}")
        if s.get("database"):
            print(f"      db: {s['database']}")
        print(f"      “{' '.join(s['evidence'].split())[:110]}…”")

    print(f"\nprovenance: {out['provenance']}")
    print(f"stats: {out['stats']}")

    if args.demo:
        print("\n--- fixture checks ---")
        roles = [s["role"] for s in chain["steps"]]
        checks = [
            ("remove_primers is separate from trim", "remove_primers" in roles),
            ("denoise present (DADA2 ASVs)", "denoise" in roles),
            ("taxonomy_assign present", "taxonomy_assign" in roles),
            ("normalize present (rarefaction)", "normalize" in roles),
            ("diversity present", "diversity" in roles),
            ("ordination present (PERMANOVA)", "ordination" in roles),
            (
                "NO hallucinated filter_host (not in text)",
                "filter_host" not in roles,
            ),
            (
                "NO hallucinated chimera_removal (not in text)",
                "chimera_removal" not in roles,
            ),
            (
                "SILVA 138.1 captured as a database",
                any("silva" in (s.get("database") or "").lower() for s in chain["steps"]),
            ),
            (
                "a truncation length captured in params",
                any("240" in (s.get("params") or "") for s in chain["steps"]),
            ),
            (
                "rarefaction depth captured in params",
                any(
                    "8" in (s.get("params") or "") and "000" in (s.get("params") or "")
                    for s in chain["steps"]
                ),
            ),
            (
                "every step carries evidence",
                all(s.get("evidence") for s in chain["steps"]),
            ),
            # tool_raw feeds alias matching in normalize.py. A version glued onto the name, or an
            # empty string standing in for "no tool named", both break that join silently.
            # Not "contains no digit" — DADA2, MetaBAT2 and CheckM2 are digit-bearing names.
            # The invariant is that no *version suffix* survives in tool_raw.
            (
                "tool_raw carries no version suffix",
                all(
                    not _VERSION_SUFFIX.search(s.get("tool_raw") or "")
                    for s in chain["steps"]
                ),
            ),
            (
                "unattributed steps use null tool_raw, not empty string",
                all(s.get("tool_raw") != "" for s in chain["steps"]),
            ),
        ]
        passed = 0
        for label, ok in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            passed += ok
        print(f"\n{passed}/{len(checks)} checks passed")
        return 0 if passed == len(checks) else 1

    print(json.dumps(chain, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
