# mepipelines

A survey of **how microbial ecology papers compose bioinformatics tools into workflows** — catalogued,
normalized, vetted, and shipped as versioned data an agent can query when handed a new dataset.

Companion to [`omc`](../omc). Where omc runs pipelines, this repo answers *which pipeline, and on
what evidence*.

## The idea in one paragraph

Tool registries already exist. Registries of **chains** do not. The knowledge that matters in this
field is not "DADA2 exists" — it is that DADA2 comes after primer removal, that MetaBAT2 output is
worthless without CheckM2, and that half the long-read MAG papers polish before binning while the
other half polish after. So the unit of record here is a composition: an ordered graph of
`(role, tool, version, notable params)` steps. And every composition is captured **twice** — once
from what the paper's Methods section *claims*, once from what the attached repo *actually runs* —
with the diff between them stored as a first-class field.

That diff is the interesting part. The gap between stated and actual method is, itself, a survey result.

## Status

Phase 0 complete: schema, ontologies, validator, one worked exemplar.
Phase 3: the `claimed` extractor runs against a local model and passes its fixture checks.
Phase 1: the Europe PMC lane fetches real bioRxiv preprints end to end.
No catalog records committed yet. See [`docs/roadmap.md`](docs/roadmap.md).

## Layout

```
schema/
  workflow.schema.json         # the record format
  ontology/
    step_roles.yaml            # ~50 roles — the slots that make chains comparable
    archetypes.yaml            # 14 composition shapes + soundness red flags
    tools.yaml                 # ~150 tools + reference DBs, with aliases
catalog/
  workflows/                   # one YAML per surveyed workflow — the source of truth
  analysis/                    # (Phase 5) consensus chains, drift, coverage map
ingest/
  europepmc.py                 # bioRxiv preprints via Europe PMC; JATS -> Methods text (working)
extract/
  llm.py                       # LM Studio + OpenRouter behind one interface; carries provenance
  from_text.py                 # Methods text -> claimed chain (working)
  prompts/                     # versioned prompts, referenced by adjudication records
  test_split_version.py        # unit tests, no model needed
scripts/
  validate.py                  # schema + ontology + graph + soundness checks
  try_biorxiv.py               # end-to-end: real preprints -> chains, + ontology coverage
docs/
  design.md                    # why it's built this way
  roadmap.md                   # phased plan
  omc-integration.md           # the seam into omc's pipeline selection
```

## Quick start

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/validate.py            # validate the catalog
./.venv/bin/python scripts/validate.py --strict   # warnings are errors (CI mode)
```

## Reading order

1. [`docs/design.md`](docs/design.md) — the claimed/observed split and what "meta-ize" produces
2. [`catalog/workflows/_example-nf-core-mag.yaml`](catalog/workflows/_example-nf-core-mag.yaml) — a
   filled record; the schema is much easier to read backwards from this
3. [`schema/ontology/step_roles.yaml`](schema/ontology/step_roles.yaml) — the vocabulary everything
   else is expressed in
4. [`docs/roadmap.md`](docs/roadmap.md) — what's next and what's undecided

## Ground rules

- **Nothing without a locator.** Every extracted step carries `evidence` pointing at the sentence or
  the line range it came from.
- **LLM judgments are re-derivable artifacts, not facts.** They are stored with the model ID and
  prompt version that produced them, and any `human_review` overrides them.
- **The catalog never picks a pipeline.** It returns ranked candidates with tiers and caveats. The
  agent, or the person, decides.
- **Not a benchmark.** We record what people did and whether it can be reproduced — not what is best.
  Accuracy claims are stored as claims, attributed to their source.
