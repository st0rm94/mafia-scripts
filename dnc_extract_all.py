#!/usr/bin/env python3
"""
dnc_extract_all.py — batch driver for `dnc_extract.py`.

Scans a directory for Mafia (2002) `.dnc` script-export files, pairs
each main container (`<NAME>.dnc`) with its optional init-scripts
sidecar (`<NAME>-InitScripts.dnc`), and runs the single-file extractor
once per pair.

Pairing rules
-------------
* `<NAME>.dnc`              => main container; sidecar pulled in if found
* `<NAME>-InitScripts.dnc`  => init-scripts sidecar
    - if `<NAME>.dnc` also exists, the sidecar is consumed by that pair
      and NOT processed standalone
    - otherwise it is processed standalone (init-only pair)
* Any other `*.dnc` file    => treated as a main container with no
                                 sidecar (no `-InitScripts.dnc` companion)

Output layout (per pair, in the same directory as the inputs by default)
-----------------------------------------------------------------------
    <NAME>/
        <script_name>.script   one file per extracted script
        ...
    <NAME>.summary             combined statistics file

Names containing spaces or other Windows-illegal filename characters in
the input are passed through unchanged at the directory level — the
underlying file already exists with that name, so any tool that opened
the .dnc can also open its output directory.

Target runtime: Python 3 on Windows, no third-party deps.

Usage
-----
    py dnc_extract_all.py                # scan ./dnc/ (cwd-relative)
    py dnc_extract_all.py --dir PATH     # scan PATH instead
    py dnc_extract_all.py --dry-run      # list planned pairs, do nothing

By default the scanner looks in `./dnc/` (relative to the current
working directory), which matches the on-disk layout in this repo
(`exe-re/scripts/dnc/*.dnc`). Extracted output still lands in the
current working directory by default — i.e. `<BASE>/` subdirs and
`<BASE>.summary` files appear next to (not inside) the `dnc/` folder.

Exit code is the number of pairs that failed to extract (0 on full success).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Reuse the single-file extractor in-process — much faster than spawning
# a subprocess per .dnc, and we get structured error handling for free.
import dnc_extract

# Required minimum API version of the sister module. Bump in lockstep
# with the value in `dnc_extract.MODULE_API_VERSION` whenever this
# script starts depending on a newer feature.
REQUIRED_DNC_EXTRACT_API = 3

_actual_api = getattr(dnc_extract, 'MODULE_API_VERSION', 0)
if _actual_api < REQUIRED_DNC_EXTRACT_API:
    sys.stderr.write(
        f"ERROR: dnc_extract.py is out of date "
        f"(found API v{_actual_api}, need >= v{REQUIRED_DNC_EXTRACT_API}).\n"
        f"  module loaded from: {getattr(dnc_extract, '__file__', '?')}\n"
        f"  Update dnc_extract.py to the matching version, then delete\n"
        f"  any __pycache__/ directory next to it before re-running.\n"
    )
    sys.exit(2)


INIT_SUFFIX = '-InitScripts.dnc'  # also defined in dnc_extract; kept local for clarity
DNC_GLOB    = '*.dnc'


def discover_pairs(scan_dir: Path):
    """
    Walk `scan_dir` (non-recursive) and group `.dnc` files into pairs.

    Returns a list of tuples sorted by base name:
        (base_name, main_path_or_None, init_path_or_None)

    Where exactly one of `main_path` / `init_path` may be None but never
    both. Files whose names don't end in `.dnc` are ignored. Files whose
    names ARE `.dnc` but follow neither convention (i.e. some weird
    `-InitScripts.dnc` orphan whose base differs from any `.dnc` file)
    still produce an init-only pair, since `dnc_extract` handles that.
    """
    if not scan_dir.is_dir():
        raise SystemExit(f"not a directory: {scan_dir}")

    # Bucket by base name
    mains: dict = {}   # base_name -> Path
    inits: dict = {}   # base_name -> Path

    for path in sorted(scan_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() != '.dnc':
            continue
        name = path.name
        if name.endswith(INIT_SUFFIX):
            base = name[: -len(INIT_SUFFIX)]
            if base in inits:
                sys.stderr.write(
                    f"warning: duplicate init-scripts base {base!r} "
                    f"({inits[base].name} vs {name}); keeping first\n"
                )
                continue
            inits[base] = path
        else:
            base = path.stem  # strips only the trailing `.dnc`
            if base in mains:
                sys.stderr.write(
                    f"warning: duplicate main base {base!r} "
                    f"({mains[base].name} vs {name}); keeping first\n"
                )
                continue
            mains[base] = path

    pairs = []
    all_bases = sorted(set(mains.keys()) | set(inits.keys()))
    for base in all_bases:
        pairs.append((base, mains.get(base), inits.get(base)))
    return pairs


def run_pair(
    base: str,
    main_path,
    init_path,
    out_root: Path,
) -> bool:
    """
    Invoke the single-file extractor for one pair. Returns True on
    success, False on failure (exception logged to stderr).
    """
    out_dir = out_root / base
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_root / f"{base}.summary"

    state = dnc_extract.ExtractState(out_dir=out_dir)
    try:
        if main_path is not None:
            dnc_extract.process_any(main_path, state)
        if init_path is not None:
            dnc_extract.process_any(init_path, state)
        dnc_extract.write_summary(state, summary_path)
    except SystemExit as exc:
        # process_any / process_main_dnc raise SystemExit on bad magic
        # etc; downgrade to a per-pair failure so the batch keeps going.
        sys.stderr.write(f"[{base}] FAILED: {exc}\n")
        return False
    except Exception as exc:  # noqa: BLE001 — defensive top-level catch
        sys.stderr.write(f"[{base}] FAILED: {type(exc).__name__}: {exc}\n")
        return False

    src_descr = []
    if main_path is not None:
        src_descr.append(f"main={main_path.name}")
    if init_path is not None:
        src_descr.append(f"init={init_path.name}")
    print(
        f"[{base}] extracted {state.scripts_written} scripts -> {out_dir}/  "
        f"({', '.join(src_descr)})"
    )
    return True


def main(argv) -> int:
    ap = argparse.ArgumentParser(
        description="Batch-extract every .dnc pair in a directory.",
    )
    ap.add_argument(
        '--dir',
        default='dnc',
        help="directory to scan (default: ./dnc, cwd-relative)",
    )
    ap.add_argument(
        '--out-root',
        help="root output directory (default: current working directory)",
    )
    ap.add_argument(
        '--dry-run',
        action='store_true',
        help="list discovered pairs without extracting",
    )
    args = ap.parse_args(argv)

    scan_dir = Path(args.dir).resolve()
    # Output defaults to the current working directory so that
    # `<BASE>/` subdirs and `<BASE>.summary` files appear next to the
    # `dnc/` folder, not inside it (matches the on-disk layout in this
    # repo). Explicit --out-root still wins.
    out_root = Path(args.out_root).resolve() if args.out_root else Path.cwd()

    pairs = discover_pairs(scan_dir)
    if not pairs:
        print(f"no .dnc files found in {scan_dir}")
        return 0

    # Pretty-print plan
    print(f"scanning: {scan_dir}")
    if out_root != scan_dir:
        print(f"output  : {out_root}")
    print(f"found   : {len(pairs)} base name(s)")
    for base, m, i in pairs:
        bits = []
        if m is not None:
            bits.append(f"main={m.name}")
        if i is not None:
            bits.append(f"init={i.name}")
        kind = 'pair' if (m is not None and i is not None) else (
            'main-only' if m is not None else 'init-only'
        )
        print(f"  - {base!r:<32}  [{kind}]  {', '.join(bits)}")

    if args.dry_run:
        return 0

    print()
    failures = 0
    for base, m, i in pairs:
        if not run_pair(base, m, i, out_root):
            failures += 1

    print()
    print(
        f"done: {len(pairs) - failures}/{len(pairs)} pair(s) extracted "
        f"({failures} failed)"
    )
    return failures


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
