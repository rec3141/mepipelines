# Design

## What this repo is

A survey of **how microbial ecology papers compose bioinformatics tools into workflows**, turned
into a vetted, queryable catalog. The consumer is an agent that has just been handed a new dataset
(an SRA accession, a set of FASTQs) and needs to answer: *what has the field actually done with data
that looks like this, and which of those compositions is worth running?*

## The unit of record is a composition, not a tool

There are already good registries of individual bioinformatics tools (Bioconda, bio.tools, Galaxy
ToolShed). There is no good registry of **chains**. The interesting, hard-won, poorly-documented
knowledge in this field is not "DADA2 exists" — it is:

- DADA2 is run *after* primer removal with cutadapt, not before, and the `trunc_len` choice is
  coupled to the amplicon region
- MetaBAT2 output is nearly always passed through DAS_Tool or CheckM2 before anyone believes it
- half of long-read MAG papers polish before binning and half after, and they disagree about why

So a catalog entry is an ordered graph of steps, each step being `(role, tool, version, notable
params)`. The tool ontology exists only to normalize the nodes so chains become comparable.

## Every entry is captured twice

The core methodological commitment of this repo:

| | source | what it tells you |
|---|---|---|
| **claimed** | the paper's Methods section | what the authors believe/report they did |
| **observed** | the attached repo — Nextflow/Snakemake/WDL/CWL, shell scripts, notebooks, env files, lockfiles | what the code will actually execute |

We extract both independently and then diff them. The diff is a field, not an error:

- `undisclosed_step` — the code does something the paper never mentions (host filtering, a hidden
  rarefaction, an arbitrary abundance cutoff)
- `missing_step` — the paper describes a step with no counterpart in the repo
- `version_mismatch` — paper says QIIME2 2021.4, environment pins 2023.5
- `param_mismatch` — paper reports defaults, code overrides them
- `tool_substitution` — paper says MEGAHIT, code calls metaSPAdes
- `db_unspecified` — a reference-database-dependent step (taxonomy, annotation) with no version
  recorded anywhere

Entries with no repo are still catalogued, with `observed: null` and a vetting penalty. That
absence is itself survey data — the fraction of the field that publishes an unreproducible chain is
a headline result.

## Where entries come from

Four ingest lanes, each producing candidate records that merge on DOI/repo URL:

1. **arXiv** — `q-bio.GN`, `q-bio.PE`, `q-bio.QM`. Thin for this field but clean and free.
2. **bioRxiv** — where microbial ecology actually preprints. The bulk of the recent catalog.
3. **PubMed / Europe PMC** — peer-reviewed and canonical. Europe PMC is preferred over PubMed
   proper because it serves full text (and therefore Methods sections) for OA articles, which is
   what the `claimed` extraction needs.
4. **Code registries** — nf-core, WorkflowHub, Galaxy, Snakemake Workflow Catalog. These are
   `observed`-only entries with no paper: already-composed, already-runnable workflows. They are
   the high-water mark the literature gets scored against.

Lanes 1–3 find papers; lane 4 finds workflows. A record can come from either side and is enriched
by the other (paper → find its repo; registry workflow → find its citation).

## Vetting

Two-stage, per the decision in `docs/roadmap.md` Phase 4.

**Stage 1 — scorecard.** Objective, recomputable, no model in the loop. Signals: public repo
resolves; license present; dependency environment declared (conda/container/lockfile); workflow
engine vs. loose scripts; pinned versions; tagged release; CI configured; test data shipped; commit
recency; reference DB versions recorded; citation count. Each is a boolean or number, stored
verbatim so the weighting can change without re-ingesting.

**Stage 2 — LLM adjudication.** A model reads the Methods text plus the repo's workflow files and
produces judgment the scorecard can't: does the chain actually do what the abstract claims; what
inputs is it valid for; what are the failure modes and caveats; is a step doing something
scientifically dubious (rarefying before differential abundance, no negative controls in a
low-biomass study). Output is structured, carries a confidence, and cites the evidence span it
came from.

Adjudications are **stored with the model ID and prompt version that produced them** and are
treated as re-derivable artifacts, not ground truth. Any adjudication can be overridden by a
`human_review` block, which always wins.

Tiers: `reference` (runnable, pinned, tested, judged sound) → `usable` (runnable with work) →
`documented` (chain is legible, code isn't runnable) → `paper_only` (no code) → `rejected`.

## What "meta-ize" produces

Once N compositions are normalized against the same step ontology, the catalog answers questions no
single paper does:

- **Consensus chains** — for each archetype, the modal composition and its variance. "The 2024
  short-read MAG consensus is fastp → MEGAHIT → MetaBAT2+MaxBin2 → DAS_Tool → CheckM2 → GTDB-Tk."
- **Substitution classes** — tools that occupy the same role slot and are swapped freely
  (MetaBAT2 ↔ SemiBin2 ↔ VAMB), versus ones that aren't.
- **Step drift** — when the field switched CheckM → CheckM2, OTU → ASV, SILVA → GTDB, and how long
  the tail lasted.
- **Orphan steps** — steps that appear in code but almost never in Methods sections. The
  reproducibility gap, quantified.
- **Coverage holes** — `(platform × strategy × environment)` cells with no vetted workflow. Tells
  omc where it can't help yet, and tells us what to go build.

## How an agent uses it

Given dataset metadata, `applicability` blocks are matched to produce ranked candidates. The
matching keys deliberately mirror SRA's own vocabulary — `platform`, `library_strategy`,
`library_source`, `library_layout` — so the join is direct (see `docs/omc-integration.md`). Beyond
those, matching also considers marker gene / amplicon region, read length, environment, sequencing
depth, and whether required reference databases are available locally.

The agent gets ranked candidates with tier, caveats, and provenance — and decides. This repo never
decides for it. A candidate list that says "three papers did X, all three omitted the host-removal
step they actually ran, here are the repos" is more useful than a single confident answer.

## Non-goals

- Not benchmarking. We record what people did and whether it's reproducible, not which is *best*.
  Accuracy claims are recorded as claims, with their source.
- Not re-implementing pipelines. We point at other people's workflows.
- Not a tool registry. Individual tools are ontology nodes, deliberately thin.
