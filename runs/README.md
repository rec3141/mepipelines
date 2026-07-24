# runs/ — raw extraction output

Input to `scripts/build_site.py`. One JSON file per query batch, each the `--json` output of
`scripts/try_biorxiv.py`.

These are committed so the published explorer is reproducible: `site/data.js` is compiled from
them, and without them the corpus could only be regrown by re-running every extraction (which is
non-deterministic, so it would not reproduce the same site).

**None of this is catalog data.** Every chain here is single-pass model output with no second
reading, no human check, and no `observed` side to diff against. Records earn their way into
`catalog/workflows/` only after Phase 3 verification and Phase 4 adjudication.

| file | query | yield |
|---|---|---|
| `01-amplicon.json` | `microbiome AND amplicon` | 9/20 |
| `02-mag.json` | `metagenome AND (MAG OR "metagenome-assembled genome" OR binning)` | 9/25 |
| `03-longread-env.json` | `(nanopore OR "long read" OR soil OR marine) AND (metagenome OR microbiome)` | 12/25 |

Yield is bounded by full-text availability, not by extraction: roughly two thirds of Europe PMC
hits have no retrievable XML, and nothing in the search metadata predicts which. See
`ingest/europepmc.py`.

Batches overlap — `build_site.py` dedupes by paper ID, so 30 records collapse to 22 papers.

## Adding a batch

    python scripts/try_biorxiv.py --query '<query>' --limit 25 --json runs/NN-name.json
    python scripts/build_site.py --from runs/*.json      # merges and dedupes
    # commit runs/NN-name.json and the regenerated site/data.js
