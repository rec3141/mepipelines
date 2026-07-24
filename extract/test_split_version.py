"""Unit tests for the version-suffix splitter.

No model or network needed — run these before any extraction work.

The load-bearing cases are the digit-bearing tool NAMES: DADA2, MetaBAT2, CheckM2, SemiBin2,
MaxBin2, bwa-mem2, QIIME2. A splitter that strips their trailing digit silently destroys alias
matching for a large fraction of this field's tooling, and the failure is invisible downstream —
`tool_raw: "MetaBAT"` just quietly matches nothing.

    python extract/test_split_version.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from from_text import split_version  # noqa: E402

CASES: list[tuple[str | None, str | None, tuple[str | None, str | None]]] = [
    # (tool_raw, version, expected)
    # --- version genuinely glued to the name ---
    ("cutadapt v3.4", None, ("cutadapt", "3.4")),
    ("DADA2 v1.20", None, ("DADA2", "1.20")),
    ("SPAdes (v3.15.5)", None, ("SPAdes", "3.15.5")),
    ("QIIME2 2021.4", None, ("QIIME2", "2021.4")),
    ("GTDB-Tk version 2.1.1", None, ("GTDB-Tk", "2.1.1")),
    ("vsearch v2.21.1", None, ("vsearch", "2.21.1")),
    ("Trycycler v0.5.3", None, ("Trycycler", "0.5.3")),
    ("fastp 0.23.2", None, ("fastp", "0.23.2")),
    # --- digit-bearing names that must survive intact ---
    ("DADA2", None, ("DADA2", None)),
    ("MetaBAT2", None, ("MetaBAT2", None)),
    ("CheckM2", None, ("CheckM2", None)),
    ("SemiBin2", None, ("SemiBin2", None)),
    ("MaxBin2", None, ("MaxBin2", None)),
    ("bwa-mem2", None, ("bwa-mem2", None)),
    ("minimap2", None, ("minimap2", None)),
    ("Kraken 2", None, ("Kraken 2", None)),
    ("Bowtie 2", None, ("Bowtie 2", None)),
    # --- precedence and degenerate input ---
    ("cutadapt v3.4", "9.9", ("cutadapt", "9.9")),  # explicit model version wins
    ("cutadapt", "v3.4", ("cutadapt", "3.4")),      # leading v stripped from explicit version
    ("3.15.5", None, ("3.15.5", None)),             # refuses to consume the whole string
    (None, None, (None, None)),
    ("", None, (None, None)),
    ("  vsearch  ", None, ("vsearch", None)),
]


def main() -> int:
    failures = 0
    for tool, version, expected in CASES:
        got = split_version(tool, version)
        ok = got == expected
        failures += not ok
        status = "ok  " if ok else "FAIL"
        detail = "" if ok else f"   expected {expected}"
        print(f"{status} {str(tool):<24} {str(version):<6} -> {got}{detail}")

    total = len(CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
