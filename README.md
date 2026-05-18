# Mafia (2002) `.dnc` script extractor

Pure-stdlib Python 3 tooling that pulls every embedded script out of
the `.dnc` script-export containers used in the reverse-engineering of
Mafia (2002, Illusion Softworks). Two scripts, one format, no
dependencies.

## TL;DR

```bash
cd exe-re/scripts
python3 dnc_extract_all.py        # batch: extract every .dnc in ./dnc/
```

The repository already ships with the extracted corpus committed —
every script from every shipped mission/freeride/menu container lives
in a sibling directory (`MISE01/`, `FREERIDE/`, `00MENU/`, …) with a
matching `<NAME>.summary` next to it. Re-running the extractor will
reproduce those files byte-for-byte from the originals in `dnc/`.

## What `.dnc` actually is (and why we use it)

`.dnc` is **not** the game's native on-disk format. The shipping
game stores every scripted actor, prop, light, and embedded script
inside `scene2.bin` — a much more complex proprietary container that
also holds geometry references, lighting data, collision, and so on.

The `.dnc` files in `dnc/` were produced by **BozSceneTreeEditor**,
a fan-made scene editor that can open `scene2.bin` and re-serialise
its actor list into the simpler, mostly-plaintext `.dnc` script-export
format. We work from `.dnc` rather than `scene2.bin` directly because:

- script bodies appear as readable CRLF text right next to their
  length-prefix (trivially extractable);
- the actor-record wire format is uniform and small (5 known record
  tags vs the dozens used by `scene2.bin`);
- everything we care about for script reverse-engineering survives the
  round-trip — actor name, actor type, and the script source itself.

The trade-off is that `.dnc` does **not** preserve full `scene2.bin`
fidelity — the non-script binary payloads of most actor types are
opaque blobs as far as this extractor is concerned. If you need the
full scene data (geometry, lights, collision, etc.), open `scene2.bin`
in BozSceneTreeEditor directly; if you just need the script source
and a per-actor type tag, `.dnc` + this extractor is the right tool.

## Layout

```
exe-re/scripts/
├── README.md                this file
├── dnc_extract.py           single-file extractor (also importable as a module)
├── dnc_extract_all.py       batch driver — wraps dnc_extract.py
├── dnc/                     147 source `.dnc` files (mission + init-script pairs)
│   ├── FREERIDE.dnc
│   ├── FREERIDE-InitScripts.dnc
│   ├── MISE01.dnc
│   ├── MISE01-InitScripts.dnc
│   └── ...
├── <BASE>/                  extracted scripts, one directory per source container pair
│   ├── <script_name>.script   one file per script, raw bytes (CRLF preserved)
│   └── ...
└── <BASE>.summary           per-pair stats: type distribution, command frequency, per-script command listing
```

76 mission/menu/freeride base names → 76 `<BASE>/` directories + 76
`.summary` files already committed.

## What gets extracted

The `.dnc` format wraps a flat sequence of "actor" records. Most actor
types carry binary payload (props, lights, doors, static meshes) which
the extractor silently skips. Two actor types carry an embedded text
script and are extracted:

| actor type | role                       | extracted file |
|-----------:|----------------------------|----------------|
| **5**      | pure script actor          | `<actor_name>.script` |
| **27**     | "Human" — scripted NPC     | `<actor_name>.script` |

Plus a separate sidecar file format:

| file pattern                  | content                                      |
|-------------------------------|----------------------------------------------|
| `<NAME>-InitScripts.dnc`      | global init scripts (`GameInitStart`, `GameInitEnd`, helpers like `TraffSndSect`) |

The sidecar is auto-discovered: passing `MISE01.dnc` will pull in
`MISE01-InitScripts.dnc` if it exists in the same directory. The
combined output goes into a single `MISE01/` directory with no
provenance distinction between main-container scripts and
init-sidecar scripts — but each script's origin is recorded in the
summary file as `type=5`, `type=27`, or `type=init`.

As a safety net the extractor also scans unknown actor types for
script-shaped payloads and emits a stderr warning if any are found
(none currently exist in the shipped corpus). If a new mod/build
introduces a third script-carrying type, the warning tells you so.

## `dnc_extract.py` — single-file mode

Extract one container (and its auto-discovered init sidecar):

```bash
python3 dnc_extract.py dnc/MISE01.dnc
# writes:
#   ./MISE01/<name>.script        (one per script)
#   ./MISE01.summary              (stats)
```

Common flags:

| flag                         | effect |
|------------------------------|--------|
| `--out-dir PATH`             | write `<name>.script` into PATH instead of `./<stem>/` |
| `--no-init-scripts`          | skip the sibling `*-InitScripts.dnc` even if it exists |
| `--init-scripts PATH`        | use an explicit init-script file instead of auto-discovery |

You can also pass an init-script sidecar as the primary input
(`python3 dnc_extract.py dnc/MISE01-InitScripts.dnc`) — the main
`MISE01.dnc` is auto-discovered the same way.

The script source is written byte-for-byte from the container — CRLF
line endings, comments, and any embedded non-ASCII bytes are
preserved. Empty scripts (NPCs with no per-instance behaviour) are
emitted as zero-byte `.script` files so they still appear in the
summary listing.

## `dnc_extract_all.py` — batch mode

Walk a directory of `.dnc` files, pair main containers with their
init-script sidecars, and run the single-file extractor in-process
once per pair:

```bash
python3 dnc_extract_all.py          # scans ./dnc/, output goes to ./
python3 dnc_extract_all.py --dry-run   # preview the pairing plan, write nothing
```

Common flags:

| flag                | effect |
|---------------------|--------|
| `--dir PATH`        | scan PATH instead of `./dnc/` |
| `--out-root PATH`   | write `<BASE>/` + `<BASE>.summary` into PATH instead of cwd |
| `--dry-run`         | print the planned pairs and exit without extracting |

Pairing rules:
- `<NAME>.dnc` + `<NAME>-InitScripts.dnc` → one pair, merged output
- `<NAME>.dnc` with no sidecar → main-only pair
- `<NAME>-InitScripts.dnc` with no `<NAME>.dnc` → init-only pair (rare; happens for some intro / cutscene containers)

Exit code is the number of failed pairs (`0` on full success).

## Regenerating the committed corpus

The committed `<BASE>/` directories and `<BASE>.summary` files are the
output of `dnc_extract_all.py` against the current `dnc/`. They are
checked in for convenience — most users want to read scripts, not
re-run a parser. If you change the extractor or want to verify the
corpus is in sync:

```bash
cd exe-re/scripts

# Remove the old extracted output (everything that isn't dnc/ or this readme)
ls -d */ | grep -v '^dnc/$' | xargs rm -rf
rm -f *.summary

# Regenerate
python3 dnc_extract_all.py
```

The extractor uses `_1`, `_2` suffix collision-handling on filename
clashes, so re-running without cleaning the old output first would
silently produce duplicate files — always clean first.

## Output format reference

### `<name>.script`
Raw script source. Line endings are CRLF (Windows). Comments
(`//`, `;`, `#`) are preserved. The Mafia script language is
case-insensitive at runtime — the same command can appear in different
casings across different shipped scripts (`findframe`, `FindFrame`,
`FINDFRAME`). The summary file deduplicates against a lowercased key
and lists the observed casings in brackets.

### `<BASE>.summary`
Plain text, three sections:

1. **Header** — source files, actor-type-code distribution, autodetect warnings (if any), skipped-actor stats.
2. **Command keyword totals** — every distinct command across all scripts in this pair, sorted by frequency, with observed source casings.
3. **Per-script listing** — one block per extracted script, annotated with `type=5` / `type=27` / `type=init`, line count, command count, and the alphabetised set of commands the script uses.

## Requirements

- Python 3.6+ (no third-party packages)
- Works on macOS, Linux, and Windows
