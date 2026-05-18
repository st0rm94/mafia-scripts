#!/usr/bin/env python3
"""
dnc_extract.py — extractor for Mafia (2002) `.dnc` script-export files.

Pulls every script out of one or more `.dnc` containers and writes each
script body to its own `.script` text file alongside a single
`<base>.summary` file with command-keyword statistics.

Two container formats are supported, both produced by the official
DNC tools shipped with Mafia (2002):

1. **Main mission/freeride container** (`<NAME>.dnc`, file tag `0xAE20`).
    A flat sequence of *actor* records (`0xAE21`); each actor has nested
    sub-records (name `0xAE23`, type `0xAE22`, data `0xAE24`). Two actor
    type codes are known to carry a script body:
      - type 5  — pure script actor (10-byte prelude, then `u32 len`
        + body)
      - type 27 — "Human" / scripted NPC (0x4f-byte prelude with the
        usual actor-property block, then `u32 len` + body; the body
        may be zero bytes for NPCs with no per-instance script)
    Other type codes carry binary payloads (props, lights, transforms,
    …) and are silently skipped. As a safety net, unknown actor types
    are sniffed for an embedded length-prefixed ASCII script blob and
    a stderr WARNING is emitted if one is found (no extraction — the
    prelude layout is unknown, so we won't guess).

2.  **Init-scripts sidecar** (`<NAME>-InitScripts.dnc`, file tag
    `0xAE50`). A flat sequence of *script* records (`0xAE51`); each
    record's payload directly contains a name + script body and is
    UNCONDITIONALLY a script (no type filter). This is where global
    init scripts like `GameInitStart`, `GameInitEnd`, and helper
    subroutines like `TraffSndSect` live.

By default the program reads the primary file AND auto-discovers the
sibling `-InitScripts.dnc` (if present) and merges everything into one
output directory + one summary, with no provenance distinction in the
output (per-script entries don't note which container they came from).

Target runtime: Python 3 on Windows, no third-party deps.

Usage
-----
    py dnc_extract.py FREERIDE.dnc [--out-dir FREERIDE]

If `FREERIDE-InitScripts.dnc` exists alongside `FREERIDE.dnc`, it is
read automatically. Pass `--no-init-scripts` to disable that.

Output layout
-------------
    <out_dir>/
        <script_name>.script    one file per extracted script
        ...
    <out_dir>.summary           combined statistics file

File formats (reverse-engineered 2026-05-14)
--------------------------------------------
Little-endian throughout. Both formats use a uniform record layout:

    +0  u16   tag
    +2  u32   record_size      ; HEADER-INCLUSIVE (next record at off+record_size)
    +6  ...   payload

Main `.dnc` (file tag 0xAE20)
    actor record (0xAE21) payload = nested sub-records:
        name record  (0xAE23) payload = NUL-terminated string + garbage padding
        type record  (0xAE22) payload = u32 type_code (+ 6 trailing bytes ignored)
        data record  (0xAE24) payload depends on type_code
    Type-5 (pure script) data payload:
        +0     10 bytes  unknown header (small ints / flags)
        +10    u32       inner_script_len
        +14    N bytes   script source (CRLF, comments preserved verbatim)
    Type-27 (Human / scripted NPC) data payload:
        +0     0x4f bytes  actor-property block (kind byte, flags,
                           position/rotation floats, health, model ids …)
        +0x4f  u32         inner_script_len (may be 0)
        +0x53  N bytes     script source (CRLF, same dialect as type 5)

Init-scripts `-InitScripts.dnc` (file tag 0xAE50)
    script record (0xAE51) payload layout:
        +0          u8    kind (always 1 in observed samples)
        +1          u32   name_len
        +5          name_len bytes  (raw name, NO NUL terminator)
        +5+name_len u32   body_len
        +9+name_len body_len bytes  (script source, CRLF, comments verbatim)
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Module API version. Bumped whenever the in-process API consumed by
# `dnc_extract_all.py` (ExtractState / process_any / write_summary)
# changes shape. Sister scripts should compare against their expected
# minimum and fail loudly on mismatch instead of dying with an opaque
# AttributeError half-way through a batch run.
#
# v3 (2026-05-18): added type-27 (Human) script extraction; per_script
# tuple grew a `type_code` field (None for init-script records);
# write_summary renders `type=N` / `type=init` per entry.
MODULE_API_VERSION = 3

# ---------------------------------------------------------------------------
# Tag constants (little-endian u16 values)
# ---------------------------------------------------------------------------
# Main `.dnc`
TAG_MAIN_FILE  = 0xAE20
TAG_MAIN_ACTOR = 0xAE21
TAG_TYPE       = 0xAE22
TAG_NAME       = 0xAE23
TAG_DATA       = 0xAE24

# `-InitScripts.dnc` sidecar
TAG_INIT_FILE   = 0xAE50
TAG_INIT_SCRIPT = 0xAE51

TYPE_SCRIPT = 5
TYPE_HUMAN  = 27

# Per-actor-type prelude size in bytes. The script field, when present,
# is always laid out as:
#       +prelude       u32   inner_script_len
#       +prelude + 4   N     script source (CRLF, may be empty)
# Only types listed here are extracted; everything else is treated as
# binary payload and skipped (with an auto-detect safety net below).
# Verified across all 21 shipped main `.dnc` containers on 2026-05-18:
# 100% of type-5 and type-27 actors conform; no other type does.
SCRIPT_PRELUDE_BY_TYPE = {
    TYPE_SCRIPT: 10,     # 10-byte unknown header
    TYPE_HUMAN:  0x4f,   # 0x4f-byte Human-actor property block
}

# Legacy aliases retained for backward-readability — point at the
# type-5 entry of SCRIPT_PRELUDE_BY_TYPE so there is one source of truth.
SCRIPT_HDR_BYTES   = SCRIPT_PRELUDE_BY_TYPE[TYPE_SCRIPT]   # = 10
SCRIPT_LEN_OFFSET  = SCRIPT_HDR_BYTES                       # u32 inner_len follows the prelude
SCRIPT_BODY_OFFSET = SCRIPT_HDR_BYTES + 4

# Auto-detect safety net: when an actor has a type not in
# SCRIPT_PRELUDE_BY_TYPE, scan its data payload for a length-prefixed
# ASCII-script-shaped blob. If we find one, log a warning so a human
# can investigate whether the type should be added to the table — but
# do NOT extract, because we cannot guess the prelude layout safely.
AUTODETECT_MIN_BODY_LEN     = 8     # ignore tiny matches (noise)
AUTODETECT_SAMPLE_BYTES     = 200   # only first N bytes are scored
AUTODETECT_PRINTABLE_THRESH = 0.95  # fraction of bytes in {TAB, CR, LF, 0x20..0x7e}

# Init-scripts inner layout offsets
INIT_KIND_OFFSET     = 0
INIT_NAMELEN_OFFSET  = 1
INIT_NAME_OFFSET     = 5

# Init-scripts marker byte at INIT_KIND_OFFSET. Only value 1 has been
# seen so far; the parser warns rather than aborts on other values, in
# case other Mafia builds use different markers.
INIT_KIND_EXPECTED = 1

# Characters that are illegal in Windows filenames
_FORBIDDEN = '<>:"/\\|?*'


# ---------------------------------------------------------------------------
# Aggregation state passed through the two parsers
# ---------------------------------------------------------------------------
@dataclass
class ExtractState:
    out_dir: Path
    cmd_counter: Counter = field(default_factory=Counter)
    casing_observed: dict = field(default_factory=dict)
    per_script: list = field(default_factory=list)        # (name, type_code_or_None, line_count, cmds)
    type_counts: Counter = field(default_factory=Counter) # only main-format actors
    actors_total: int = 0                                  # main-format actors walked
    init_records_total: int = 0                            # init-format records walked
    scripts_written: int = 0
    skipped_no_name: int = 0
    skipped_no_data: int = 0
    autodetect_warnings: int = 0                           # unknown-type actors that looked script-shaped
    sources: list = field(default_factory=list)           # (path, size_bytes, kind)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """Make a script name safe for use as a Windows filename."""
    out = []
    for ch in name:
        if ch in _FORBIDDEN or ord(ch) < 0x20:
            out.append('_')
        else:
            out.append(ch)
    cleaned = ''.join(out).strip().rstrip('.')
    return cleaned or 'unnamed'


def read_u16(buf: bytes, off: int) -> int:
    return struct.unpack_from('<H', buf, off)[0]


def read_u32(buf: bytes, off: int) -> int:
    return struct.unpack_from('<I', buf, off)[0]


def first_token(line: str):
    """First whitespace-/comma-/paren-delimited token of a script line."""
    s = line.lstrip()
    if not s:
        return None
    if s.startswith('//') or s.startswith(';') or s.startswith('#'):
        return None
    end = 0
    for end, ch in enumerate(s):
        if ch.isspace() or ch in ',(':
            break
    else:
        end = len(s)
    tok = s[:end]
    return tok or None


def collect_commands(script_text: str, state: ExtractState):
    """Update keyword counts (case-insensitive) and return sorted unique
    lowercased command list for this script."""
    local = set()
    for line in script_text.splitlines():
        tok = first_token(line)
        if tok is None:
            continue
        key = tok.lower()
        state.cmd_counter[key] += 1
        state.casing_observed.setdefault(key, set()).add(tok)
        local.add(key)
    return sorted(local)


def emit_script(state: ExtractState, name: str, body: bytes, type_code=None) -> None:
    """Decode `body` for keyword counting, write raw bytes to disk.

    `type_code` is the owning actor's type code for main-container scripts
    (5 = pure script, 27 = Human), or None for init-script-sidecar records
    which have no actor-type concept.
    """
    # Decode tolerantly for tokenisation only — disk write is byte-exact.
    text = body.decode('latin1', errors='replace')
    cmds = collect_commands(text, state)
    line_count = text.count('\n') + (0 if text.endswith('\n') else 1)
    state.per_script.append((name, type_code, line_count, cmds))

    base = sanitize_filename(name)
    out_path = state.out_dir / f"{base}.script"
    dup = 1
    while out_path.exists():
        out_path = state.out_dir / f"{base}_{dup}.script"
        dup += 1
    out_path.write_bytes(body)
    state.scripts_written += 1


# ---------------------------------------------------------------------------
# Main `.dnc` parser (file tag 0xAE20)
# ---------------------------------------------------------------------------
def parse_actor(buf: bytes, actor_off: int, actor_end: int):
    """Walk one main-format actor. Returns (name, type_code, data_off, data_len)."""
    name = type_code = data_off = data_len = None

    p = actor_off + 6
    while p < actor_end:
        if p + 6 > actor_end:
            raise ValueError(f"truncated sub-record header at 0x{p:x}")
        tag = read_u16(buf, p)
        size = read_u32(buf, p + 2)
        if size < 6 or p + size > actor_end:
            raise ValueError(
                f"bad sub-record size {size} at 0x{p:x} (actor 0x{actor_off:x})"
            )
        payload_off = p + 6
        payload_len = size - 6

        if tag == TAG_NAME:
            raw = buf[payload_off:payload_off + payload_len]
            name = raw.split(b'\x00', 1)[0].decode('latin1', errors='replace')
        elif tag == TAG_TYPE:
            if payload_len < 4:
                raise ValueError(f"type record too short at 0x{p:x}")
            type_code = read_u32(buf, payload_off)
        elif tag == TAG_DATA:
            data_off = payload_off
            data_len = payload_len
        # else: silently skip — none observed under uniform-length rule

        p += size

    return name, type_code, data_off, data_len


def extract_main_script_body(buf: bytes, data_off: int, data_len: int,
                             prelude_size: int = SCRIPT_HDR_BYTES) -> bytes:
    """Extract the script body from a main-container actor data payload.

    Layout (uniform across all known script-bearing actor types):
        +prelude_size       u32  inner_script_len
        +prelude_size + 4   N    script source bytes

    `prelude_size` is the per-actor-type fixed-size header in front of
    the length field — looked up via SCRIPT_PRELUDE_BY_TYPE by the
    caller. The body MAY be zero bytes (observed for ~4% of type-27
    actors in the shipped corpus).
    """
    body_offset = prelude_size + 4
    if data_len < body_offset:
        raise ValueError(
            f"script data payload too short for prelude={prelude_size}: "
            f"{data_len} bytes"
        )
    inner_len = read_u32(buf, data_off + prelude_size)
    avail = data_len - body_offset
    if inner_len > avail:
        sys.stderr.write(
            f"  warning: inner_len {inner_len} > available {avail}; truncating\n"
        )
        inner_len = avail
    return buf[data_off + body_offset : data_off + body_offset + inner_len]


def _looks_like_script_blob(blob: bytes) -> bool:
    """Heuristic: does this byte run look like Mafia script source?

    Used ONLY by the auto-detect safety net — never on payloads whose
    layout is already known. We require near-pure printable-ASCII +
    whitespace to keep the false-positive rate low.
    """
    sample = blob[:AUTODETECT_SAMPLE_BYTES]
    if len(sample) < AUTODETECT_MIN_BODY_LEN:
        return False
    printable = sum(
        1 for b in sample
        if b in (0x09, 0x0a, 0x0d) or 0x20 <= b <= 0x7e
    )
    return (printable / len(sample)) >= AUTODETECT_PRINTABLE_THRESH


def autodetect_script_in_unknown_type(
    buf: bytes, data_off: int, data_len: int,
    file_name: str, actor_name: str, type_code: int,
    state: ExtractState,
) -> None:
    """Scan an unknown-type actor's data payload for a length-prefixed
    script-shaped blob. If one is found, log a stderr warning and
    increment `state.autodetect_warnings`. Never extracts — the prelude
    layout is unknown, and silently emitting a guess could corrupt
    downstream tooling. The intent is to flag the case so a human can
    add the type to SCRIPT_PRELUDE_BY_TYPE after verification.
    """
    # Scan candidate prelude offsets. Bound at +0x200 to keep the
    # cost negligible — observed preludes are 0x0a and 0x4f.
    max_p = min(data_len - 4, 0x200)
    for p in range(4, max_p + 1):
        cand = read_u32(buf, data_off + p)
        # Length must exactly consume the rest of the payload AND be
        # large enough to be a real script (>= AUTODETECT_MIN_BODY_LEN).
        if cand != data_len - p - 4:
            continue
        if cand < AUTODETECT_MIN_BODY_LEN:
            continue
        body = buf[data_off + p + 4 : data_off + p + 4 + cand]
        if not _looks_like_script_blob(body):
            continue
        sys.stderr.write(
            f"  AUTODETECT: {file_name}: actor '{actor_name}' "
            f"(type {type_code}) has script-shaped payload at "
            f"prelude=0x{p:02x}, body_len={cand}. Add type {type_code} "
            f"to SCRIPT_PRELUDE_BY_TYPE to extract; not extracted now.\n"
        )
        state.autodetect_warnings += 1
        return  # one warning per actor is enough


def process_main_dnc(input_path: Path, state: ExtractState) -> None:
    buf = input_path.read_bytes()
    if len(buf) < 6:
        raise SystemExit(f"{input_path}: file too small")

    file_tag = read_u16(buf, 0)
    file_size = read_u32(buf, 2)
    if file_tag != TAG_MAIN_FILE:
        raise SystemExit(
            f"{input_path}: not a main .dnc file "
            f"(tag 0x{file_tag:04x} != 0x{TAG_MAIN_FILE:04x})"
        )
    if file_size != len(buf):
        sys.stderr.write(
            f"warning: {input_path.name}: file_size field {file_size} "
            f"!= actual size {len(buf)}\n"
        )

    state.sources.append((input_path, len(buf), 'main'))

    off = 6
    actor_index = 0
    while off < len(buf):
        if off + 6 > len(buf):
            sys.stderr.write(
                f"warning: {input_path.name}: trailing {len(buf) - off} bytes "
                f"after last actor\n"
            )
            break
        tag = read_u16(buf, off)
        size = read_u32(buf, off + 2)
        if tag != TAG_MAIN_ACTOR:
            sys.stderr.write(
                f"warning: {input_path.name}: unexpected top-level tag "
                f"0x{tag:04x} at 0x{off:x}\n"
            )
            break
        if size < 6 or off + size > len(buf):
            raise SystemExit(f"{input_path.name}: bad actor size {size} at 0x{off:x}")
        actor_end = off + size

        try:
            name, tcode, data_off, data_len = parse_actor(buf, off, actor_end)
        except ValueError as exc:
            sys.stderr.write(f"{input_path.name} actor #{actor_index} @0x{off:x}: {exc}\n")
            off = actor_end
            actor_index += 1
            continue

        state.type_counts[tcode if tcode is not None else -1] += 1

        prelude = SCRIPT_PRELUDE_BY_TYPE.get(tcode)
        if prelude is not None:
            if name is None:
                state.skipped_no_name += 1
            elif data_off is None:
                state.skipped_no_data += 1
            else:
                body = extract_main_script_body(buf, data_off, data_len, prelude)
                emit_script(state, name, body, type_code=tcode)
        elif tcode is not None and data_off is not None and data_len >= 8:
            # Unknown type — sniff for an embedded script blob and warn
            # only. See autodetect_script_in_unknown_type docstring.
            autodetect_script_in_unknown_type(
                buf, data_off, data_len,
                input_path.name, name or '<unnamed>', tcode, state,
            )

        off = actor_end
        actor_index += 1

    state.actors_total += actor_index


# ---------------------------------------------------------------------------
# Init-scripts `.dnc` parser (file tag 0xAE50)
# ---------------------------------------------------------------------------
def parse_init_script_record(buf: bytes, payload_off: int, payload_len: int):
    """
    Decode one 0xAE51 record's payload. Returns (name, body_bytes).
    Layout:
        +0   u8   kind (=1)
        +1   u32  name_len
        +5   name_len bytes  (raw name, NO NUL terminator)
        +N   u32  body_len
        +N+4 body_len bytes  (script source)
    """
    if payload_len < 9:
        raise ValueError(f"init-script payload too short: {payload_len} bytes")

    kind = buf[payload_off + INIT_KIND_OFFSET]
    if kind != INIT_KIND_EXPECTED:
        sys.stderr.write(
            f"  warning: init-script kind={kind} (expected {INIT_KIND_EXPECTED})\n"
        )

    name_len = read_u32(buf, payload_off + INIT_NAMELEN_OFFSET)
    name_off = payload_off + INIT_NAME_OFFSET
    if name_len > payload_len - 9:
        raise ValueError(
            f"init-script name_len {name_len} overflows payload ({payload_len} bytes)"
        )

    name = buf[name_off : name_off + name_len].decode('latin1', errors='replace')
    # Strip trailing NULs just in case some authoring tool padded; the
    # observed format does NOT NUL-terminate, but be defensive.
    name = name.rstrip('\x00')

    body_len_off = name_off + name_len
    body_len = read_u32(buf, body_len_off)
    body_off = body_len_off + 4
    if body_off + body_len > payload_off + payload_len:
        raise ValueError(
            f"init-script body_len {body_len} overflows payload "
            f"(name_len={name_len}, payload_len={payload_len})"
        )
    body = buf[body_off : body_off + body_len]

    # Sanity: trailing bytes inside payload?
    consumed = INIT_NAME_OFFSET + name_len + 4 + body_len
    if consumed != payload_len:
        sys.stderr.write(
            f"  warning: init-script payload has {payload_len - consumed} "
            f"unconsumed trailing bytes (name={name!r})\n"
        )

    return name, body


def process_init_dnc(input_path: Path, state: ExtractState) -> None:
    buf = input_path.read_bytes()
    if len(buf) < 6:
        raise SystemExit(f"{input_path}: file too small")

    file_tag = read_u16(buf, 0)
    file_size = read_u32(buf, 2)
    if file_tag != TAG_INIT_FILE:
        raise SystemExit(
            f"{input_path}: not an init-scripts .dnc file "
            f"(tag 0x{file_tag:04x} != 0x{TAG_INIT_FILE:04x})"
        )
    if file_size != len(buf):
        sys.stderr.write(
            f"warning: {input_path.name}: file_size field {file_size} "
            f"!= actual size {len(buf)}\n"
        )

    state.sources.append((input_path, len(buf), 'init'))

    off = 6
    rec_index = 0
    while off < len(buf):
        if off + 6 > len(buf):
            sys.stderr.write(
                f"warning: {input_path.name}: trailing {len(buf) - off} bytes "
                f"after last record\n"
            )
            break
        tag = read_u16(buf, off)
        size = read_u32(buf, off + 2)
        if tag != TAG_INIT_SCRIPT:
            sys.stderr.write(
                f"warning: {input_path.name}: unexpected top-level tag "
                f"0x{tag:04x} at 0x{off:x}\n"
            )
            break
        if size < 6 or off + size > len(buf):
            raise SystemExit(
                f"{input_path.name}: bad record size {size} at 0x{off:x}"
            )

        try:
            name, body = parse_init_script_record(buf, off + 6, size - 6)
        except ValueError as exc:
            sys.stderr.write(
                f"{input_path.name} init-record #{rec_index} @0x{off:x}: {exc}\n"
            )
            off += size
            rec_index += 1
            continue

        if not name:
            state.skipped_no_name += 1
        else:
            emit_script(state, name, body, type_code=None)

        off += size
        rec_index += 1

    state.init_records_total += rec_index


# ---------------------------------------------------------------------------
# Summary emission
# ---------------------------------------------------------------------------
def write_summary(state: ExtractState, summary_path: Path) -> None:
    with summary_path.open('w', encoding='utf-8', newline='\n') as fh:
        fh.write("# .dnc extraction summary\n")
        for path, size, kind in state.sources:
            fh.write(f"# source        : {path.name}  ({size} bytes, {kind})\n")
        fh.write(f"# actors_total  : {state.actors_total}\n")
        fh.write(f"# init_records  : {state.init_records_total}\n")
        fh.write(f"# scripts_found : {state.scripts_written}\n")
        if state.skipped_no_name or state.skipped_no_data:
            fh.write(
                f"# skipped       : no_name={state.skipped_no_name} "
                f"no_data={state.skipped_no_data}\n"
            )
        if state.autodetect_warnings:
            fh.write(
                f"# autodetect    : {state.autodetect_warnings} unknown-type "
                f"actor(s) had script-shaped payload (see stderr)\n"
            )
        fh.write("\n")

        if state.type_counts:
            fh.write("# main-container actor type-code distribution\n")
            for tcode, n in sorted(state.type_counts.items()):
                if tcode == TYPE_SCRIPT:
                    label = 'script'
                elif tcode == TYPE_HUMAN:
                    label = 'human (scripted NPC)'
                else:
                    label = ''
                fh.write(f"#   type {tcode!s:>4}  count={n}  {label}\n")
            fh.write("\n")

        fh.write("# ---- command keyword totals (across all scripts, case-insensitive) ----\n")
        fh.write(f"# unique commands  : {len(state.cmd_counter)}\n")
        fh.write(f"# total invocations: {sum(state.cmd_counter.values())}\n")
        fh.write("# (counts are case-insensitive; observed source casings shown in brackets)\n\n")
        for cmd, n in sorted(state.cmd_counter.items(), key=lambda kv: (-kv[1], kv[0])):
            variants = sorted(state.casing_observed.get(cmd, {cmd}))
            if len(variants) == 1 and variants[0] == cmd:
                fh.write(f"{n:6d}  {cmd}\n")
            else:
                fh.write(f"{n:6d}  {cmd}    [casings: {', '.join(variants)}]\n")

        fh.write("\n# ---- per-script command listing ----\n")
        for name, type_code, line_count, cmds in sorted(state.per_script):
            type_label = 'init' if type_code is None else str(type_code)
            fh.write(
                f"\n[{name}]  type={type_label}  lines={line_count}  "
                f"commands={len(cmds)}\n"
            )
            for c in cmds:
                fh.write(f"    {c}\n")


# ---------------------------------------------------------------------------
# Sniffing & dispatch
# ---------------------------------------------------------------------------
def detect_format(path: Path) -> str:
    """Peek at the first 2 bytes; return 'main', 'init', or 'unknown'."""
    with path.open('rb') as fh:
        head = fh.read(2)
    if len(head) < 2:
        return 'unknown'
    tag = struct.unpack('<H', head)[0]
    if tag == TAG_MAIN_FILE:
        return 'main'
    if tag == TAG_INIT_FILE:
        return 'init'
    return 'unknown'


def process_any(path: Path, state: ExtractState) -> None:
    """Dispatch to the correct parser based on file magic."""
    fmt = detect_format(path)
    if fmt == 'main':
        process_main_dnc(path, state)
    elif fmt == 'init':
        process_init_dnc(path, state)
    else:
        raise SystemExit(f"{path}: unrecognised file magic")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
INIT_SUFFIX = '-InitScripts.dnc'


def main(argv) -> int:
    ap = argparse.ArgumentParser(
        description="Extract scripts from Mafia (2002) .dnc containers."
    )
    ap.add_argument(
        'input',
        help="primary .dnc file — either the main container "
             "(e.g. FREERIDE.dnc) or an init-scripts sidecar "
             "(e.g. MISE01-InitScripts.dnc); the matching companion "
             "file is auto-discovered if present in the same directory",
    )
    ap.add_argument('--out-dir', help="output directory (default: <input-stem>/)")
    ap.add_argument(
        '--no-init-scripts',
        action='store_true',
        help=f"do NOT auto-load sibling *{INIT_SUFFIX} file",
    )
    ap.add_argument(
        '--init-scripts',
        help=f"explicit path to a *{INIT_SUFFIX} file "
             "(overrides auto-discovery)",
    )
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.is_file():
        ap.error(f"not a file: {in_path}")

    # Determine the canonical "base name" of the mission/freeride. If the
    # user passed the init-scripts sidecar as the primary input, strip the
    # `-InitScripts` suffix so output paths still derive from the mission
    # name (e.g. `MISE01-InitScripts.dnc` -> base `MISE01`).
    in_stem = in_path.stem
    if in_stem.endswith('-InitScripts'):
        base_stem = in_stem[: -len('-InitScripts')]
        primary_input_is_init = True
    else:
        base_stem = in_stem
        primary_input_is_init = False

    # Output dir + summary derive from base_stem unless --out-dir given.
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else in_path.with_name(base_stem)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir.parent / f"{out_dir.name}.summary"

    # Resolve the companion file (whichever side wasn't passed in).
    # `--init-scripts PATH` overrides auto-discovery on the main side;
    # `--no-init-scripts` disables auto-discovery in either direction.
    main_path = None
    init_path = None
    if primary_input_is_init:
        init_path = in_path
        if args.init_scripts:
            ap.error("--init-scripts cannot be combined with an init-scripts primary input")
        if not args.no_init_scripts:
            candidate = in_path.with_name(base_stem + '.dnc')
            if candidate.is_file():
                main_path = candidate
    else:
        main_path = in_path
        if args.init_scripts:
            init_path = Path(args.init_scripts)
            if not init_path.is_file():
                ap.error(f"not a file: {init_path}")
        elif not args.no_init_scripts:
            candidate = in_path.with_name(base_stem + INIT_SUFFIX)
            if candidate.is_file():
                init_path = candidate

    state = ExtractState(out_dir=out_dir)
    if main_path is not None:
        process_any(main_path, state)
    if init_path is not None:
        process_any(init_path, state)

    write_summary(state, summary_path)

    print(f"extracted {state.scripts_written} scripts -> {out_dir}")
    print(f"summary: {summary_path}")
    if main_path is not None:
        print(f"main source        : {main_path}")
    if init_path is not None:
        print(f"init-scripts source: {init_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
