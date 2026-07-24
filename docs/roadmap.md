# Roadmap

Phases are ordered by dependency, not by calendar. Each ends with something inspectable.

Status legend: **done** · **next** · *later*

---

## Phase 0 — Schema and ontology · **done**

The vocabulary has to exist before anything can be extracted into it.

- `schema/workflow.schema.json` — the record format, with `claimed`/`observed`/`divergence`
- `schema/ontology/step_roles.yaml` — ~50 roles, the slots that make chains comparable
- `schema/ontology/archetypes.yaml` — 14 composition shapes with red-flag rules
- `schema/ontology/tools.yaml` — ~150 tools + 15 reference DBs, aliases for text and code matching
- `scripts/validate.py` — JSON Schema plus cross-file, graph, and soundness checks
- `catalog/workflows/_example-nf-core-mag.yaml` — one filled record, validating

**Exit check:** `python scripts/validate.py` → 0 errors. ✓

---

## Phase 1 — Ingest lanes · **in progress** (Europe PMC lane working)

`ingest/europepmc.py` fetches real bioRxiv preprints end to end, and `scripts/try_biorxiv.py`
runs them straight into the `claimed` extractor. Measured on 20 amplicon preprints:

- **9/20 produced a chain.** All 11 losses were upstream full-text availability, not parsing.
- **0 failures extracting Methods** from XML we did get — the JATS section walk held on every paper.
- **88% of named tools matched `tools.yaml`**, up from 62% on the first run; the gap closed by
  adding the tools real papers actually cite rather than the ones we guessed.

Two API facts that cost real time, now recorded in the module docstring:

- The full-text endpoint is `/{id}/fullTextXML` with **no source prefix**. The plausible-looking
  `/PPR/{id}/fullTextXML` returns 404 with an empty body — indistinguishable from "no full text".
- **Nothing in the search metadata predicts whether full text exists.** `OPEN_ACCESS:Y AND
  IN_EPMC:Y` still yields only 28–40% retrievable (11/40 and 16/40 on two queries), and two records
  identical across `inEPMC`, `isOpenAccess`, `hasTextMinedTerms`, `hasPDF` and `fullTextIdList` can
  differ on it. The only test is the fetch, so over-request ~3× and treat 404 as an ordinary outcome.

That availability ceiling is the main open risk to catalog size, and it is worth resolving before
scaling ingest: bioRxiv's own TDM/S3 full-text corpus would sidestep Europe PMC's subset entirely.

---

## Phase 1 (original plan)

Get candidate papers and workflows into a staging area. No extraction yet — this phase only
answers "what is there".

- `ingest/arxiv.py` — Atom API over `q-bio.GN`, `q-bio.PE`, `q-bio.QM`
- `ingest/biorxiv.py` — bioRxiv details API, filtered to the microbiology/bioinformatics subjects
- `ingest/europepmc.py` — preferred over PubMed E-utilities because it serves OA **full text**,
  which is what `claimed` extraction needs. Fall back to PubMed for abstract-only records.
- `ingest/registries.py` — nf-core, WorkflowHub, Snakemake Workflow Catalog, Galaxy
- `ingest/dedupe.py` — merge on DOI → repo URL → normalized title

Design constraints worth fixing now:

- **Cache raw responses to disk, keyed by source ID.** Ingest must be re-runnable without hammering
  anyone's API, and extraction gets re-run far more often than ingest does.
- **Respect rate limits and licenses.** Store full text only where the article license allows it;
  otherwise store the locator and re-fetch on demand. `provenance.sources[].license` exists for this.
- Query strategy is itself a deliverable — write the search terms down in `ingest/queries.yaml`
  rather than burying them in code, since recall depends entirely on them.

**Exit check:** a staging table of N candidates with source IDs, titles, and detected repo URLs;
a report of how many have code attached, broken down by lane.

---

## Phase 2 — Repo harvesting · *later*

For every candidate with a repo: clone at a pinned commit, inventory it, discard the bulk.

- Detect engine from entrypoint files (`main.nf`, `Snakefile`, `*.cwl`, `*.wdl`, `*.sh`, `*.Rmd`)
- Extract dependency specs: `environment.yml`, `Dockerfile`, `renv.lock`, `requirements.txt`,
  nf-core `modules.json`
- Record commit SHA, release tag, license, CI config, test data, last commit date
- Keep only workflow/config/env files and READMEs — never the whole repo

Populates `provenance.code`, `runnability`, and most of `vetting.scorecard` with zero model calls.

**Exit check:** scorecard fields populated for every candidate with a resolving repo.

---

## Phase 3 — Dual extraction · **in progress** (text side)

The core of the project. Two independent extractors that must not see each other's output — the
whole value of `divergence` depends on them being blind to one another.

- `extract/from_text.py` → `claimed`. LLM over the Methods section, constrained to emit steps with
  ontology role IDs and an evidence quote per step. Falls back to `abstract_only` completeness when
  full text is unavailable.
- `extract/from_code.py` → `observed`. Hybrid: deterministic parsing where the engine allows it
  (Nextflow process/module names, Snakemake rule names, conda env package lists) and LLM reading
  for loose scripts and notebooks. Deterministic first — it is cheaper, reproducible, and its
  output constrains the model's.
- `extract/normalize.py` — `tool_raw` → canonical `tool` via alias matching, reporting misses so
  `tools.yaml` grows from data.
- `extract/diverge.py` — align the two chains by role and emit `divergence` entries.

Prompts live in `extract/prompts/` and are **versioned**, because `vetting.adjudication.prompt_version`
references them and stale judgments must be identifiable.

**Exit check:** hand-verify ~20 records end to end. Report extractor precision/recall against that
hand-labeled set, and keep the set as a regression fixture. This is the phase where being wrong is
cheapest to discover and most expensive to miss.

### Where it stands

`extract/from_text.py` works. `extract/fixtures/claimed.yaml` holds 5 of the ~20 fixtures, run by
`extract/test_claimed.py --repeat N`. Fixtures are written for this repo rather than copied from
papers — license-clean, and it lets us plant specific traps.

Lessons worth keeping, all of them learned the expensive way:

- **The ontology is the prompt.** `roles_block()` renders `step_roles.yaml` straight into the
  system prompt, so a vague role description *is* an extractor bug. Sharpening
  `taxonomy_assign` vs `classify_genomes` fixed GTDB-Tk misassignment across both MAG fixtures;
  passing `typical_tools` through — which we had curated and were simply not sending — stopped
  Filtlong landing in `custom`.
- **JSON Schema `description` fields don't reach the model on LM Studio.** The schema constrains
  structure only. Guidance belongs in the prompt file.
- **Check the assertion before blaming the model.** Several early "failures" demanded behaviour the
  prompt correctly forbids — inferring a database name from a release the text states bare — and
  one passed spuriously by substring-matching a single digit.
- **Single runs prove nothing.** Failures here are probabilistic; `--repeat` is the unit of
  evidence, and a fix confirmed once is not confirmed.

**Resolved — the intermittent runaway was a configuration bug, not the model.** Gemma 4 through an
OpenAI-compatible endpoint lets the thinking channel consume the entire token budget and omits
content; raising the ceiling does not help. Disabling thinking in LM Studio's `model.yaml` removed
it outright and cut the suite from 774 s to ~130 s. Full diagnosis, the LM Studio and ollama fixes,
and the measured before/after are in [`docs/local-models.md`](local-models.md) — including the
delimiter detail that Gemma 4 uses `<|channel>thought … <channel|>` rather than `<think>`.

The residual tradeoff is `params` recall: thinking off loses one or two numeric parameters on
`amplicon_asv_clean` that thinking on captured reliably. Roles, tools, and ordering are unaffected.
Use thinking off for development; decide the production setting with the labeled set.

Still open: the `observed` extractor (`from_code.py`) is unwritten, the fixture set needs
roughly quadrupling, and model choice stays deferred until the labeled set can decide it.

---

## Phase 4 — Vetting · *later*

- `vet/scorecard.py` — pure function over Phase 2 output. No model. Recomputable at any time.
- `vet/adjudicate.py` — LLM judgment: does the chain match the claims, what is it valid for, what
  are the caveats, are there soundness flags. Emits `suggested_tier` plus evidence.
- `vet/tier.py` — combine scorecard, adjudication, and any `human_review` override into `tier`.

Two rules that keep this honest:

1. `human_review.tier_override` always wins.
2. Adjudications carry their model ID and prompt version and are treated as re-derivable, never as
   ground truth. When the model changes, re-run rather than trust.

Spot-check protocol: sample a fixed fraction of adjudications per batch for human review, and track
agreement over time. If agreement drops, the prompt or the model changed underneath us.

**Exit check:** every record has a tier; the sampled agreement rate is recorded.

---

## Phase 5 — Meta-analysis · *later*

What the catalog is actually for. Outputs land in `catalog/analysis/` as committed data.

- **Consensus chains** per archetype — modal role sequence and its variance
- **Substitution classes** — which tools actually swap at a role, empirically, versus the
  `substitutable` guesses currently hard-coded in `step_roles.yaml`
- **Step drift** — CheckM→CheckM2, OTU→ASV, SILVA→GTDB timelines
- **Orphan steps** — roles common in `observed`, rare in `claimed`. The reproducibility gap.
- **Coverage map** — `(platform × strategy × environment)` cells with no vetted workflow
- **Archetype re-derivation** — cluster real chains on role sequence and check the hand-written
  archetypes against them. Where the data disagrees, the data wins and `archetypes.yaml` changes.

**Exit check:** a coverage map and an orphan-step table that would be publishable on their own.

---

## Phase 6 — Consumption · *later*

- `catalog/build.py` — compile YAML records into a single `catalog.json` plus a SQLite file
  (generated, `.gitignore`d; the YAML is the source of truth)
- `scripts/build_site.py --catalog` — the explorer in `site/` currently builds from raw extraction
  output; pointing it at vetted catalog records instead is a one-function change, deliberately
  left erroring loudly rather than silently emitting an empty site
- `match/rank.py` — `match(dataset_metadata) -> ranked candidates`, the function `_auto_pipeline()
  in omc becomes
- A written handoff for omc — see `docs/omc-integration.md`

**Exit check:** given a real SRA accession, the ranked candidate list is defensible to a
microbial ecologist.

---

## Open questions

Worth deciding before the phase that depends on them, not now:

- **Recall vs. precision on ingest.** Broad queries mean more junk to adjudicate. A `benchmark`
  archetype already exists so comparison papers can be kept rather than discarded — but where the
  net is cast is unresolved.
- **Where the analysis boundary sits.** Many ecology papers run a pipeline *and* an analysis;
  `ecological_analysis_only` catches the pure cases, but split papers need a convention.
- **How to handle QIIME2-shaped opacity.** A chain that is one `qiime2` suite call hides its
  internals. Expand suites into their constituent roles, or record them as one node and flag them?
  Expansion is more comparable; the single node is more honest about what the paper said.
- **Update cadence.** Preprints get revised and repos move. Re-ingest on a schedule, or on demand?
