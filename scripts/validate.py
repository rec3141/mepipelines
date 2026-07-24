#!/usr/bin/env python3
"""Validate catalog records against the JSON Schema and the ontologies.

JSON Schema handles structure. This script adds the cross-file checks it can't express:
role IDs, tool IDs, and archetype IDs must exist in schema/ontology/, step graphs must be
well-formed, and role/tool pairs must be compatible.

Unknown tools are reported as WARNINGS, not errors — the ontology is meant to grow from real
extraction, and a hard failure would push extractors toward mislabeling. Unknown *roles* are
errors, because the role vocabulary is the thing that makes chains comparable.

    python scripts/validate.py                    # validate the whole catalog
    python scripts/validate.py path/to/rec.yaml   # validate specific records
    python scripts/validate.py --strict           # treat warnings as errors (use in CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml jsonschema")

try:
    import jsonschema
except ImportError:
    sys.exit("jsonschema required: pip install pyyaml jsonschema")

import json
from collections import Counter


class DateAsStringLoader(yaml.SafeLoader):
    """YAML 1.1 auto-converts unquoted `2024-01-01` to datetime.date.

    Records are JSON-Schema-validated with `format: date`, i.e. strings, and the catalog is
    round-tripped through JSON when compiled. Keeping dates as strings end-to-end avoids a
    class of silent type drift, so the timestamp resolver is removed here.
    """


DateAsStringLoader.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "workflow.schema.json"
ONTOLOGY = ROOT / "schema" / "ontology"
CATALOG = ROOT / "catalog" / "workflows"


def load_yaml(path: Path):
    return yaml.load(path.read_text(), Loader=DateAsStringLoader)


def load_ontologies() -> dict:
    roles_doc = load_yaml(ONTOLOGY / "step_roles.yaml")
    tools_doc = load_yaml(ONTOLOGY / "tools.yaml")
    arch_doc = load_yaml(ONTOLOGY / "archetypes.yaml")

    roles = {r["id"]: r for r in roles_doc["roles"]}
    tools = {t["id"]: t for t in tools_doc["tools"]}
    archetypes = {a["id"]: a for a in arch_doc["archetypes"]}

    # Ontology self-consistency: every role a tool claims must exist.
    problems = []
    for tid, tool in tools.items():
        for role in tool.get("roles", []):
            if role not in roles:
                problems.append(f"tools.yaml: {tid} claims unknown role '{role}'")
    for aid, arch in archetypes.items():
        for key in ("required_roles", "expected_roles", "red_flag_missing"):
            for role in arch.get(key) or []:
                if role not in roles:
                    problems.append(f"archetypes.yaml: {aid}.{key} references unknown role '{role}'")

    return {"roles": roles, "tools": tools, "archetypes": archetypes, "problems": problems}


def check_chain(chain, label, onto, errors, warnings, rec_id):
    """Cross-file and graph checks for one chain (claimed or observed)."""
    if chain is None:
        return

    steps = chain.get("steps", [])
    step_ids = {s["step_id"] for s in steps if s.get("step_id")}
    seen_ids = []

    for i, step in enumerate(steps):
        where = f"{rec_id}:{label}[{i}]"

        role = step.get("role")
        if role not in onto["roles"]:
            errors.append(f"{where}: unknown role '{role}'")

        tool = step.get("tool")
        if tool:
            if tool not in onto["tools"]:
                warnings.append(
                    f"{where}: tool '{tool}' not in tools.yaml "
                    f"(tool_raw={step.get('tool_raw', '?')!r}) — add it if real"
                )
            elif role in onto["roles"]:
                declared = onto["tools"][tool].get("roles", [])
                if role not in declared:
                    warnings.append(
                        f"{where}: {tool} used at role '{role}' but tools.yaml lists "
                        f"{declared} — widen the tool's roles or check the extraction"
                    )
        elif not step.get("tool_raw"):
            warnings.append(f"{where}: step has neither `tool` nor `tool_raw`")

        sid = step.get("step_id")
        if sid:
            if sid in seen_ids:
                errors.append(f"{where}: duplicate step_id '{sid}'")
            seen_ids.append(sid)

        for dep in step.get("depends_on") or []:
            if dep not in step_ids:
                errors.append(f"{where}: depends_on references unknown step_id '{dep}'")
            elif dep == sid:
                errors.append(f"{where}: step depends on itself")

        alt = step.get("alternative_of")
        if alt is not None and alt not in step_ids:
            errors.append(f"{where}: alternative_of references unknown step_id '{alt}'")

        # depends_on must point backwards in topological order.
        if step.get("depends_on"):
            order_by_id = {s["step_id"]: s["order"] for s in steps if s.get("step_id")}
            for dep in step["depends_on"]:
                if dep in order_by_id and order_by_id[dep] >= step["order"]:
                    errors.append(
                        f"{where}: depends_on '{dep}' has order "
                        f"{order_by_id[dep]} >= this step's {step['order']}"
                    )

        # Evidence discipline: an observed step with no evidence is unverifiable. Collapsed to one
        # line per record by summarize_warnings — a chain-wide gap is one problem, not twenty.
        if label == "observed" and not step.get("evidence"):
            warnings.append(f"{rec_id}:observed: step has no evidence locator [{i}]")


def flatten_schema_errors(err):
    """Yield the most specific errors under `err`.

    A failure inside a `oneOf`/`anyOf` branch reports the whole instance in its message, which for a
    workflow chain is hundreds of lines of unreadable dict repr. The useful information is in
    `err.context` — the per-branch sub-errors. Descend to those instead.
    """
    if err.context:
        # Report only the branch that got furthest, matching jsonschema's own best_match heuristic.
        best = max(err.context, key=lambda e: len(list(e.absolute_path)))
        yield from flatten_schema_errors(best)
    else:
        yield err


def check_record(rec, path, schema, onto, errors, warnings):
    rec_id = rec.get("id", path.stem)

    for top in sorted(jsonschema.Draft202012Validator(schema).iter_errors(rec), key=str):
        for err in flatten_schema_errors(top):
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            msg = err.message if len(err.message) < 200 else err.message[:200] + " …"
            errors.append(f"{rec_id}: schema violation at {loc}: {msg}")

    # Underscore-prefixed files are fixtures, exempt from the id/filename convention.
    is_fixture = path.name.startswith("_")
    if rec.get("id") and not is_fixture and rec["id"] != path.stem:
        warnings.append(f"{rec_id}: id does not match filename '{path.name}'")

    for arch in rec.get("archetype") or []:
        if arch not in onto["archetypes"]:
            errors.append(f"{rec_id}: unknown archetype '{arch}'")

    check_chain(rec.get("claimed"), "claimed", onto, errors, warnings, rec_id)
    check_chain(rec.get("observed"), "observed", onto, errors, warnings, rec_id)

    prov = rec.get("provenance", {})
    if not prov.get("sources") and not prov.get("code"):
        errors.append(f"{rec_id}: provenance has neither sources nor code")

    # A record with no code cannot have divergence — there is nothing to diff against.
    if rec.get("observed") is None and rec.get("divergence"):
        errors.append(
            f"{rec_id}: divergence recorded but observed is null — "
            "nothing was compared, so the diff is unsupported"
        )

    # Tier consistency with the evidence actually present.
    tier = rec.get("vetting", {}).get("tier")
    has_code = bool(prov.get("code"))
    if tier in ("reference", "usable") and not has_code:
        errors.append(f"{rec_id}: tier '{tier}' requires attached code")
    if tier == "paper_only" and has_code:
        warnings.append(f"{rec_id}: tier 'paper_only' but code is attached")

    # Archetype red flags — the soundness signal, surfaced at validation time.
    chain = rec.get("observed") or rec.get("claimed") or {}
    present = {s.get("role") for s in chain.get("steps", [])}
    for arch in rec.get("archetype") or []:
        spec = onto["archetypes"].get(arch, {})
        for role in spec.get("required_roles") or []:
            if role not in present:
                warnings.append(
                    f"{rec_id}: archetype '{arch}' requires role '{role}', absent from chain"
                )
        for role in spec.get("red_flag_missing") or []:
            if role not in present:
                warnings.append(
                    f"{rec_id}: SOUNDNESS — archetype '{arch}' expects '{role}'; "
                    "absence should be addressed in vetting.adjudication.soundness_notes"
                )


def summarize_warnings(warnings: list[str]) -> list[str]:
    """Collapse warnings that differ only by a trailing `[index]` into one counted line."""
    grouped: Counter[str] = Counter()
    order: list[str] = []
    for w in warnings:
        key = w.rsplit(" [", 1)[0] if w.endswith("]") and " [" in w else w
        if key not in grouped:
            order.append(key)
        grouped[key] += 1
    return [f"{k} (x{grouped[k]})" if grouped[k] > 1 else k for k in order]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = ap.parse_args()

    schema = json.loads(SCHEMA.read_text())
    onto = load_ontologies()

    errors: list[str] = list(onto["problems"])
    warnings: list[str] = []

    paths = args.paths or sorted(CATALOG.glob("*.yaml"))
    if not paths:
        print("no catalog records found — nothing to validate")
        return 0

    for path in paths:
        try:
            rec = load_yaml(path)
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: unparseable YAML: {exc}")
            continue
        if not isinstance(rec, dict):
            errors.append(f"{path.name}: top level is not a mapping")
            continue
        check_record(rec, path, schema, onto, errors, warnings)

    warnings = summarize_warnings(warnings)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    print(
        f"\n{len(paths)} record(s) — {len(errors)} error(s), {len(warnings)} warning(s)"
    )
    if errors:
        return 1
    if warnings and args.strict:
        print("--strict: failing on warnings")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
