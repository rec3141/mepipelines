# runs/ — raw extraction output

Input to `scripts/build_site.py`. One JSON file per query batch, each the `--json` output of
`scripts/try_biorxiv.py`.

These are committed so the published explorer is reproducible: `site/data.js` is compiled from
them, and without them the corpus could only be regrown by re-running every extraction (which is
non-deterministic, so it would not reproduce the same site).

**None of this is catalog data.** Every chain here is single-pass model output with no second
reading, no human check, and no `observed` side to diff against. Records earn their way into
`catalog/workflows/` only after Phase 3 verification and Phase 4 adjudication.

| file | records | usable chains |
|---|---|---|
| `01-amplicon.json` | 9 | 8 |
| `02-mag.json` | 9 | 8 |
| `03-longread-env.json` | 12 | 10 |
| `04-community.json` | 7 | 7 |
| `05-marker.json` | 11 | 11 |
| `06-ecology.json` | 11 | 11 |
| `07-function.json` | 8 | 8 |
| `08-biogeochem.json` | 8 | 8 |
| `09-host.json` | 8 | 8 |
| `10-pub-2016-2018.json` | 18 | 18 |
| `11-pub-2019-2021.json` | 12 | 12 |
| `12-pub-2022-2023.json` | 20 | 20 |
| `13-pub-2024-2025.json` | 20 | 20 |

Queries are recorded in each batch's commit message; the ecology-focused set (04-09) was
written to avoid the eukaryotic-genome papers that earlier broad queries pulled in.

Batches 01-09 are **preprints** (`SRC:"PPR"`), where only 28-40% of hits have retrievable full
text. Batches 10-13 are the **published open-access literature** (`SRC:"MED"`, via `--published`),
where availability measured **100%** on a 25-record sample and the pool is ~100x larger
(97k vs 826 hits on a representative query). Batches 10-13 are also stratified by publication
period, which is what makes the drift analysis possible.

One caveat inherent to mixing the two: a preprint and its published version are separate Europe PMC
records, so the same study can in principle appear twice. Dedupe is by record ID, not by work.

Batches overlap — `build_site.py` dedupes by paper ID, so 30 records collapse to 22 papers.

## Adding a batch

    python scripts/try_biorxiv.py --query '<query>' --limit 25 --json runs/NN-name.json
    python scripts/build_site.py --from runs/*.json      # merges and dedupes
    # commit runs/NN-name.json and the regenerated site/data.js
