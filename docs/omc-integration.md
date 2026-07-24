# Integration with omc

`mepipelines` produces data. `omc` consumes it. No runtime dependency in either direction — omc
vendors or imports a released catalog file.

## The seam

`omc/portal/app/submissions.py:34`:

```python
def _auto_pipeline(breakdown: list) -> PipelineType:
    """Pick a pipeline from breakdown rows (best guess)."""
    ...
    if any("AMPLICON" in s for s in strategies):
        return PipelineType.MICROSCAPE
    if any("RNA" in s for s in strategies):
        return PipelineType.RNASEQ
    ...
    return PipelineType.NANOPORE_MAG
```

Five hardcoded pipelines, an if/else ladder over `platform`/`strategy`/`source`, and a default
fallthrough. It works, and for omc's own five pipelines it should keep working — but it can only
ever return one answer, with no alternatives, no provenance, and no statement of confidence.

The catalog's `applicability` block was designed against exactly these fields. That is not a
coincidence and it is the whole reason integration is cheap:

| omc breakdown key | catalog field |
|---|---|
| `platform` | `applicability.platform` |
| `strategy` | `applicability.library_strategy` |
| `source` | `applicability.library_source` |
| `layout` | `applicability.library_layout` |

`omc/portal/app/sra_metadata.py` already produces these for every submission. Everything the
catalog adds beyond them — marker gene, read length, environment, depth, required reference DBs —
refines ranking without needing new omc plumbing.

## What integration looks like

Deliberately additive. omc keeps executing only pipelines it can actually run; the catalog supplies
context, alternatives, and justification around that choice.

**Step 1 — map omc's own pipelines into the catalog.** Write five records for `microscape`,
`nanopore_mag`, `illumina_mag`, `rnaseq`, `isolate_genome`, each carrying an
`omc_pipeline_id` in `aliases`. This alone is useful before any survey data exists: it puts omc's
pipelines on the same axes as the literature, which is what makes "how does ours compare" answerable.

**Step 2 — replace the ladder with a ranker, same return type.**

```python
def _auto_pipeline(breakdown: list) -> PipelineType:
    candidates = catalog.match(breakdown, runnable_by="omc")
    if candidates:
        return candidates[0].omc_pipeline_id
    return PipelineType.NANOPORE_MAG   # keep the fallthrough
```

`runnable_by="omc"` filters to the five omc can execute. Behavior should be identical to today's
ladder on today's inputs — verify that against existing submissions before switching, and treat any
disagreement as a finding about one system or the other.

**Step 3 — surface the rest to the author and to the AI.** The non-runnable candidates are the
actual payoff. On the submission page: *"Three published workflows match this data type. omc runs
the closest equivalent. Two others use SemiBin2 instead of MetaBAT2 — here are the papers."*

Two downstream consumers in omc benefit directly:

- **The author interview** (`omc/ai/author_interview.py`) can ask better questions when it knows
  what the field typically does with this data shape — and can ask about the specific choices the
  catalog flags as contested (normalization method, binner, DA test).
- **Manuscript methods generation** (`omc/ai/manuscript_generator.py`) can cite the workflows the
  chain descends from, with real DOIs from `provenance.sources`, instead of describing steps
  generically.

**Step 4 — coverage feedback.** The Phase 5 coverage map says which `(platform × strategy ×
environment)` cells have vetted literature workflows but no omc pipeline. That is a build queue for
omc, derived from evidence rather than from guessing.

## What crosses the boundary

A single released artifact:

```
catalog/catalog.json     # compiled from catalog/workflows/*.yaml, one file, versioned
```

Options, in order of preference:

1. **Vendor it.** Copy `catalog.json` into omc at a tagged catalog version. Zero coupling, fully
   reproducible, updates are an explicit PR that shows exactly what changed in the diff.
2. **Git submodule.** Live-ish, still pinned to a commit.
3. **Package it.** Only worth it once `match()` is complex enough that omc shouldn't reimplement it.

Start with (1). The catalog will change far more slowly than omc does, and an update that silently
changes pipeline selection for a live submission is exactly the failure mode to avoid.

## What must not cross

- **No live API calls from omc to this repo.** Pipeline selection cannot depend on a service being up.
- **No LLM adjudication at omc request time.** Adjudications are precomputed and committed. omc
  reads judgments; it never generates them.
- **The catalog never picks.** It returns ranked candidates with tiers and caveats. omc — or its
  author, or its agent — decides, and that decision is recorded on omc's side.
