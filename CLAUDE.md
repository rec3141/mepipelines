# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this repo is

A survey of how microbial ecology papers **compose** bioinformatics tools into workflows, turned
into a vetted, queryable catalog. Companion to `../omc`, which runs pipelines; this repo decides
which pipeline the evidence supports. Read `docs/design.md` before making structural changes.

The unit of record is a **composition**, not a tool. Every composition is captured twice — `claimed`
(from the paper's Methods) and `observed` (from the attached repo) — with `divergence` between them
as a first-class field. If a change would blur that distinction, it is the wrong change.

## Layout

```
schema/workflow.schema.json    # record format (JSON Schema 2020-12)
schema/ontology/*.yaml         # step_roles, archetypes, tools — the controlled vocabularies
catalog/workflows/*.yaml       # source of truth, one record per workflow
catalog/analysis/              # Phase 5 derived data
ingest/                        # Phase 1 — arXiv, bioRxiv, Europe PMC, registries
extract/                       # Phase 3 — text->claimed, code->observed, normalize, diverge
scripts/validate.py            # schema + ontology + graph + soundness validation
docs/                          # design, roadmap, omc-integration
```

## Commands

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/validate.py            # validate catalog
./.venv/bin/python scripts/validate.py --strict   # warnings are errors (CI)
./.venv/bin/python scripts/validate.py catalog/workflows/foo.yaml   # one record

./.venv/bin/python extract/test_split_version.py  # unit tests, no model needed
./.venv/bin/python extract/llm.py --check         # probe the LLM endpoint
./.venv/bin/python extract/from_text.py --demo    # claimed-extraction fixture + checks
```

Always run `validate.py` after touching the schema, any ontology, or any catalog record. The
validator enforces cross-file constraints JSON Schema cannot express, and it has already caught real
errors in hand-written records.

## Conventions

**YAML dates stay strings.** `scripts/validate.py` installs a loader with the timestamp resolver
removed, because YAML 1.1 would otherwise turn `2024-01-01` into a `datetime.date` and break
JSON-Schema `format: date` validation and JSON round-tripping. Any new YAML reader in this repo must
use `load_yaml()` from that module, not `yaml.safe_load`.

**Underscore-prefixed catalog files are fixtures**, exempt from the id-matches-filename rule.
`_example-nf-core-mag.yaml` is hand-written from prior knowledge, not extraction — it carries
`needs_review: true` and placeholder values. Do not cite it as data.

**Unknown tools warn; unknown roles error.** `tools.yaml` is meant to grow from real extraction, so
a missing tool is a prompt to add it. The role vocabulary is what makes chains comparable, so an
unknown role is a hard failure.

**Evidence discipline.** No extracted claim without an `evidence` locator — a section name for text,
a repo-relative path with line range for code. The validator warns on observed steps that lack one.

**LLM output is a re-derivable artifact, never ground truth.** Anything a model produces is stored
with the model ID and prompt version that produced it (`vetting.adjudication`). `human_review`
always wins. Prompts live versioned in `extract/prompts/`.

**Provenance rides on every completion.** `extract/llm.py` returns a `Completion` carrying
provider, exact model ID, and prompt version, with `.provenance()` producing the dict that goes
straight into `vetting.adjudication`. A call path that drops it is a bug — an LLM judgment whose
model is unknown cannot be compared or re-derived, which is the whole point of storing it.

**Provider is env-selected, not hardcoded.** `MEP_PROVIDER=local` (LM Studio) or `openrouter`;
both speak the OpenAI-compatible protocol, so one client covers both. With `local` and no
`MEP_MODEL`, the client uses whatever LM Studio has loaded. Copy `.env.example` to `.env`.

**Fix model non-compliance deterministically where the format allows it.** When a model reliably
ignores a formatting instruction on a tightly-patterned field, add a post-processing backstop
rather than escalating prompt pressure — see `split_version()` in `extract/from_text.py`, which
pulls version suffixes out of `tool_raw`. Keep the prompt instruction too; it is the first line of
defence, the code is the guarantee. **Any such backstop needs a unit test covering the names it
could corrupt** — `DADA2`, `MetaBAT2`, `CheckM2`, `SemiBin2`, `bwa-mem2` and friends carry
meaningful trailing digits, and mangling one fails alias matching silently.

**Extraction runs at `temperature=0`.** Reproducibility matters more than variety here; there is no
task in this repo where output variation is desirable.

**Put instructions in the system prompt, not in JSON Schema `description` fields.** Measured on
LM Studio: the schema is used to constrain *structure* (a grammar), and its `description` strings
do not reliably reach the model. Moving the `params` guidance from the schema description into
`claimed_v1.md` took that assertion from failing 3/3 runs to failing 1/3. Use schema descriptions
as documentation for humans reading the code; put anything the model must act on in the prompt.

**Local-model setup is documented in [`docs/local-models.md`](docs/local-models.md).** Read it
before debugging an extraction failure that looks like model incapability — empty responses,
budget exhaustion, or reasoning text leaking into JSON are usually backend configuration, and the
fixes live outside this repo where nothing will remind you of them. Reasoning delimiters differ by
model family (`<think>` vs Gemma 4's `<|channel>thought … <channel|>`); `strip_think()` handles
both and anything added later must too.

**`finish_reason == "length"` does not mean the output is unusable.** A model can emit a complete,
valid JSON object and then keep generating until it hits the cap. `llm.py` therefore parses first
and raises `TruncatedResponse` only when nothing parseable came back — rejecting on the finish
reason alone throws away good extractions. Note also that the token budget on a reasoning model
covers thinking *and* output: gemma-4 spends ~75-85% of its tokens reasoning, so a ceiling sized
for the expected JSON will return zero content.

**Judge extractor changes on repeats, not a single run.** `test_claimed.py --repeat N` exists
because these failures are probabilistic. A fix verified on one run is not verified. Equally, when
a fixture fails, check whether the *assertion* is wrong before changing the prompt — several early
"failures" were the fixture demanding behaviour the prompt correctly forbids (inferring a database
name the text never states), and one assertion passed spuriously by matching a bare digit.

**`claimed` and `observed` extractors must stay blind to each other.** The entire value of
`divergence` depends on the two chains being derived independently. Do not add a code path that
feeds one extractor's output into the other, even as a hint.

## Things that are deliberate, not oversights

- `normalize` and `differential_abundance` are marked `substitutable: false` in `step_roles.yaml`.
  These choices are live methodological disputes and the tools genuinely disagree on real data.
- `remove_primers` is a separate role from `trim`. Running it after denoising is a material error,
  and folding it into `trim` would make that invisible.
- `observed: null` means "no code attached" and is different from an empty chain, which would mean
  "code exists and does nothing". The validator rejects `divergence` when `observed` is null.
- The catalog does not rank tools by quality. This is not a benchmark — see the non-goals in
  `docs/design.md`.

## Working style for this repo

- The ontologies are hypotheses about the field, seeded from prior knowledge. Phase 5 re-derives
  archetypes from real chains. When the data disagrees with the hand-written vocabulary, the data
  wins and the YAML changes.
- Prefer deterministic extraction over model calls where the format allows it (Nextflow process
  names, Snakemake rules, conda env lists). It is cheaper, reproducible, and constrains the model.
- Cache raw API responses and cloned repos under `.cache/` dirs (gitignored). Ingest must be
  re-runnable without re-hitting anyone's API; extraction runs far more often than ingest.
- Respect article licenses when storing full text. `provenance.sources[].license` exists for this —
  where the license does not permit caching, store the locator and re-fetch.
