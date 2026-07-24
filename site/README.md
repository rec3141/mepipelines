# site/ — the chain explorer

A static explorer for surveyed workflow chains. One HTML file plus generated data; no build
toolchain, no dependencies, nothing to install.

    python scripts/build_site.py --from extractions.json   # writes site/data.js
    python -m http.server -d site 8000                     # preview at localhost:8000

`index.html` is the app and is edited by hand. `data.js` is **generated — do not edit it**; it is
committed because GitHub Pages has to serve it.

## Why data.js and not data.json

The page reads `window.MEP_DATA` from a script tag rather than `fetch()`-ing a JSON file. A relative
`fetch()` is blocked by CORS under `file://`, which would mean the explorer only worked when served.
This way it opens correctly from disk *and* from Pages.

## Publishing

**Settings → Pages → Source → "GitHub Actions".** That's the only setup step;
`.github/workflows/pages.yml` then publishes `site/` on every push that touches it, and can be
re-run by hand from the Actions tab.

Branch-based deploys are *not* an option here: GitHub only offers `/ (root)` or `/docs` as source
folders, never an arbitrary directory. The alternatives were moving the explorer into `docs/`
(which holds developer prose — a different audience and a different thing) or into the repo root.
Uploading `site/` as a Pages artifact keeps the separation.

Everything is relative, so the same directory also works on any static host, or from `file://`.

## Design constraints worth preserving

- **Colour is validated, not chosen by eye.** The eight stage colours are the categorical palette
  from the dataviz skill and pass its CVD, lightness, chroma and contrast checks in both light and
  dark mode. Re-run the validator before changing any of them.
- **Nine ontology stages, eight palette slots.** `acquire` and `preprocess` merge into `reads`
  (both handle raw reads pre-inference) rather than cycling a ninth hue, which the palette
  rules forbid. That merge lives in `scripts/build_site.py`.
- **Colour never carries meaning alone.** Every block shows a two-letter stage code, and both
  sections have a full table view. The codes are two letters because `amplicon`/`assembly`/
  `annotate` all start with A and `reads`/`reference` with R.
- **Block text ink is per-slot.** White fails contrast on the lighter steps, so each slot pairs
  with its own ink (`--ink-1` … `--ink-8`).
