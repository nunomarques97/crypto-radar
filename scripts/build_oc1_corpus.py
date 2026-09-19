"""Build the OC-1 section 6 corpus from recorded Kraken OHLCVT zips (T051a, D59/D60/D64).

Usage (both arguments required, no default)::

    python scripts/build_oc1_corpus.py --sextant-data <dir> --out <dir>

``--sextant-data`` is read only (the zips are verified against
``kraken-archive/manifest.json`` and read in memory, never extracted). ``--out`` must not
exist or be empty, and is refused at the repository root, in the synthetic fixture corpus
and inside the Sextant. Seed and selection rules are fixed in
``radar_v08/adapters/oc1_corpus_builder.py``. Exit 0 on success, 2 on a refused build (the
typed code is printed), nothing is written unless the whole corpus was built.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from radar_v08.adapters.oc1_corpus_builder import (  # noqa: E402
    BuildError,
    build_corpus,
    check_output_dir,
    write_corpus,
)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the OC-1 section 6 corpus (read-only source).")
    parser.add_argument("--sextant-data", required=True, help="Sextant data directory with the Kraken OHLCVT zips")
    parser.add_argument("--out", required=True, help="new or empty output directory for lock.json and the cases")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        check_output_dir(arguments.out, arguments.sextant_data)  # refuse before reading anything
        corpus = build_corpus(arguments.sextant_data)
        target = write_corpus(corpus, arguments.out, arguments.sextant_data)
    except BuildError as error:
        print(f"refused: {error.code.value}: {error}", file=sys.stderr)
        return 2
    print(f"corpus written to {target}")
    print(f"lock sha256 {corpus.lock_sha256}")
    for partition, cases in corpus.cases.items():
        counts: dict[str, int] = {}
        for case in cases:
            category = str(case["category"])
            counts[category] = counts.get(category, 0) + 1
        print(f"{partition.value}: {len(cases)} cases " + ", ".join(f"{name}={counts[name]}" for name in sorted(counts)))
    separation = corpus.lock["separation"]
    print(f"separation {separation}")
    if corpus.ignored_zips:
        print("ignored (not in the manifest): " + ", ".join(corpus.ignored_zips))
    if corpus.listed_zips_absent:
        print("in the manifest but absent: " + ", ".join(corpus.listed_zips_absent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
