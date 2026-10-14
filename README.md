# STIL/WGL Scan Pin/Timing/CHL Checker

Canonical repository/skill name: `stil-wgl-scan-pin-timing-chl-checker`
(formerly `stil-scan-pin-checker`). The display name uses slashes; the
filesystem and repository name uses hyphens. The CLI entry point remains
`scripts/check_scan_pins.py` for compatibility.

A reusable Copilot skill and standalone Python CLI for checking STIL/WGL scan-data pins against tester pin groups, with optional executable STIL timing and symbolic WGL timing checks.

## What it checks

- Reads `.stil[.gz]` and `.wgl[.gz]` without modifying or extracting inputs to disk.
- STIL: extracts `ScanIn` and `ScanOut` from every `ScanStructures / ScanChain` block. WGL: checks first/last external endpoints of each `scanchain`; internal scan cells are not tester pins.
- Requires **direction matching** in the base check for both body and setup: ScanIn must be in `stm_scan_in` or `stm_scan_nonclk_dpin`; ScanOut must be in `stm_scan_out` or `stm_scan_nonclk_dpin`. The dpin group is direction-free. Pins only in the opposite scan group fail with DIRECTION_MISMATCH; pins in none of these groups fail with PIN_NOT_FOUND.
- Converts names to uppercase and bus indices `[n]` to `_n`. Example: `"data_bus"[2]` becomes `DATA_BUS_2`.
- Reports chain names, decompressed line numbers, normalized/original pins and matching groups as JSON.
- Records clocks and absent ScanIn/ScanOut declarations as informational observations.
- DRD performs basic checks only. IOD/CCD additionally assess body and chain Channellink eligibility using strictly direction-matched scan-in/out membership; no copies are made by default.
- On an explicit IOD/CCD export request, copies eligible files into a versioned Channellink folder inside each source directory, inserting `_sc_` before the filename version and keeping originals unchanged.

**Timing is opt-in with `--timing`.** CCD WGL setup/body is streamed to EOF: the active timeplate must be 10 ns; setup TCK must toggle between `0` and `1` at least once (five `0`/five `1` run statistics are reported but informational); a body needs one return-format (pulse waveform) input scan clock at `1` for at least 2 consecutive executed cycles; a `chain` file is setup+body and must pass both rules. Clocks are identified by timeplate waveform, not pin name. These WGL checks are requested *symbolic criteria*, not proof of physical clock edges or MHz. STIL resolves the selected waveform table, Spec category, executed vectors/macros and parallel Shift to validate drive-edge cadence; Period alone is insufficient. Target JTAG clocks: DRD/IOD 25 MHz, CCD 10 MHz; target scan clock: 100 MHz for all. See executable STIL timing below for supported syntax and idle-hold policy. When requested, timing FAIL exits 1 and UNDETERMINED/ERROR exits 2; unproven timing prevents explicit Channellink export. WGL Channellink export requires `--timing`; without it, check-only retains basic-pin behavior. Missing declarations alone do not fail membership. Empty/unreadable/unsupported inputs cannot pass.

## Installation

Requires Python **3.10+**. No pip packages or external services are required.

Reuse an existing local interpreter and use `-B` to avoid bytecode caches. Do not create an environment or editor-setup folders in shared pattern directories; if an isolated environment is needed, keep it outside that workspace.

Clone this repository into your personal skills directory, naming the directory `stil-wgl-scan-pin-timing-chl-checker`:

```text
~/.copilot/skills/stil-wgl-scan-pin-timing-chl-checker/
```

On Windows this is `%USERPROFILE%\.copilot\skills\stil-wgl-scan-pin-timing-chl-checker`. Alternatively copy the repository into `.github/skills/stil-wgl-scan-pin-timing-chl-checker/` in a project. Reopen the editor/chat if the new skill is not immediately discovered.

Ask Copilot: "Check scan pins in this STIL directory against this pin file" or "Check the executed STIL clock timing." Use `--timing` to request timing validation separately from pin membership.

## Standalone usage

From the repository directory:

```shell
python -B scripts/check_scan_pins.py /path/to/patterns --pin-file /path/to/target.pin --die DRD
python -B scripts/check_scan_pins.py /path/to/sample.stil.gz --pin-file /path/to/target.pin --die IOD
python -B scripts/check_scan_pins.py /path/to/patterns --plt-dir /path/to/PLT --stage sort --die DRD
python -B scripts/check_scan_pins.py /path/to/patterns --pin-file /path/to/target.pin --die CCD --groups scan_in scan_out scan_nonclk_dpin
python -B scripts/check_scan_pins.py /path/to/CCD_G1_setup.wgl.gz --pin-file /path/to/target.pin --die CCD --timing
```

`--die` is required even with `--pin-file`; missing/unknown dies are rejected instead of guessing a CHL policy. Each invocation covers one die, so split mixed-die scopes. These rules apply to both SORT and CLASS:

| Die | Default behavior | Copy policy |
|---|---|---|
| DRD | Basic only; CHL NOT_CHECKED | Disabled; `--channel-link` is rejected, even with `--dry-run` |
| IOD / CCD | Basic plus read-only CHL eligibility | Only with explicit `--channel-link` |

Input directories are recursive. `.stil[.gz]` and `.wgl[.gz]` files are processed. Generated `_Channellink` directories, `_sc_v<number>` names and legacy `_CHL` files are excluded to avoid processing outputs on repeat runs. Use your selected interpreter instead of `python` if needed. For sampling, pass selected files instead of a whole directory; include an actual IOD/CCD body to test positive eligibility. Rule development or sample checks do not authorize real copies. CCD G1/G2/G3 are the WGL families in the current HVM source workflow; die selection must still be explicit.

### Body/chain classification and Channellink copies

```shell
# Preview only (no file creation)
python -B scripts/check_scan_pins.py /path/to/patterns --pin-file /path/to/target.pin --die IOD --channel-link --dry-run
# Explicitly requested IOD/CCD generation only; preserve originals
python -B scripts/check_scan_pins.py /path/to/patterns --pin-file /path/to/target.pin --die CCD --channel-link
```

All files first undergo the direction-aware base check with its dpin exemption. DRD stops there and is not assessed for CHL. For IOD/CCD, PASS files with an underscore-delimited `body` **or `chain`** filename token and **every declared ScanIn in scan_in, every declared ScanOut in scan_out** qualify, for both STIL and WGL (plain or compressed). CHL has no dpin exemption: a pin requiring dpin to pass prevents eligibility, although the file can still PASS the base check. Overlap with dpin is allowed when the correct directional scan group also contains the pin. Setup filenames (including ambiguous body/setup or chain/setup names), failed checks and files without data pins are never copied. With custom `--groups`, the order is scan-in, scan-out, nonclk dpin; three distinct names are required. Chain uses exactly the body naming and destination rules below, without changing its `chain` filename token. Existing timing gates and explicit-copy requirements remain unchanged.

The version is extracted from a single `v<number>` filename token. Insert `sc_` immediately before that existing token, preserving all other tokens, version case and the full STIL/WGL extension. Do not append a second version or the legacy `_CHL` suffix. For example:

```text
sort_IOD/batch/v1/example_body_p1_v1.stil.gz
	-> sort_IOD/batch/v1/v1_Channellink/example_body_p1_sc_v1.stil.gz
v1_TC100_G134/example_body_v1_TC100.stil.gz
	-> v1_TC100_G134/v1_Channellink/example_body_sc_v1_TC100.stil.gz
sort_CCD/batch/v1/example_chain_v1.wgl.gz
	-> sort_CCD/batch/v1/v1_Channellink/example_chain_sc_v1.wgl.gz
```

For recursive inputs, each file's own parent receives its output folder. A source under `batch/v1/` therefore gets `batch/v1/v1_Channellink/`, **not** `batch/v1_Channellink/`. The folder uses the lowercased filename version, not the full source directory name. Missing/ambiguous versions are reported instead of guessed; DRD skips this CHL naming check. `.stil[.gz]` and `.wgl[.gz]` retain their format, bytes and contents. Only the copied filename changes; SHA256 is verified. Existing identical files are skipped as ALREADY_EXISTS; differing files are errors and never overwritten. No rename, move or deletion of originals occurs.

Check-only IOD/CCD invocations report ELIGIBLE/SKIPPED (or a classification ERROR) with planned destinations and no writes. DRD reports NOT_CHECKED with `eligible: null` and no destination; this does not mean the pattern failed CHL requirements, since they were not evaluated. IOD/CCD export reports COPIED/ALREADY_EXISTS/SKIPPED/ERROR; preview uses WOULD_COPY and does not validate existing destination contents. Both check-only and preview create no output folders or files. Membership and CHL results are separate in JSON. Export/preview errors cause exit 2 even when membership passes; successful copies in other files are retained in export mode. Missing ScanIn/ScanOut declarations remain informational, as in basic mode.

### Optional pin-family selection

| Stage | Die | PLT filename family token |
|---|---|---|
| sort | DRD | ddr |
| sort | CCD | sort |
| sort | IOD | pcie |
| class | all three | class |

The token must be an underscore-separated part of the filename stem, e.g. `example_ddr_rev0.pin`. If several revisions match, select one explicitly with `--pin-file`. No vendor pin files are bundled. These family conventions are optional; explicit input paths and group overrides work with other installations.

## Results

JSON includes top-level `die`, `channel_link_checks` (false for DRD), `summary`, `channel_link_summary`, `channel_link_requested`, `clock_checks`, optional `timing_summary` and `dry_run`, plus per-file `pins`, `absent_fields`, `clocks_info_only`, optional `timing`, `error` and `channel_link` entries.

Each pin has `passed`, `failure`, `expected_group`, `allowed_groups`, `direction_matches` and actual `groups`. `passed=false` identifies a failure; a nonempty `groups` list does not imply success when direction is wrong. These direction-aware rules supersede the older union-only behavior; rerun previously passing inputs under the current rules.

| Status | Meaning |
|---|---|
| PASS | At least one data pin was found and every declared data pin matches its direction or belongs to direction-free dpin |
| FAIL | At least one declared data pin is missing or only belongs to the opposite directional group without dpin exemption |
| UNDETERMINED | No declared data pins were found |
| ERROR | Configuration, parsing, decompression or file-read failure |

Check-only exit codes cover basic checks: **0** = all PASS, **1** = membership failure, **2** = configuration/basic ERROR or UNDETERMINED. A check-only CHL filename error is reported separately in JSON; inspect it even if basic passes. Export/preview CHL errors also cause exit 2. If several statuses occur, inspect every result. `line` refers to decompressed STIL, not a byte offset in gzip.

### Supported syntax and limits

- Quoted or simple unquoted pin references, scalar pins and single numeric bus indices (also `"bus[2]"`).
- ScanStructures starts a line; opening brace can be on a later line. Optional structure names are accepted. Multiple blocks supported; flat ScanChain bodies required.
- WGL recognizes quoted scan chain endpoints, equation-based timeplate periods/waveforms, scanstates, pattern-order `vector`/`scan` statements, `repeat` and nested `loop`; computed cycles are cross-checked against `TesterCycle` annotations. Unsupported statements, nonmatching vector widths, cycle mismatches, or a missing pattern/final `end` raise ERROR rather than pretending to validate. Only the setup/body symbolic timing criteria are eligible for timing PASS. Full-file checks of large WGL take minutes (~5 min per 500M lines) but only a summary is reported.
- STIL `//`, `/* ... */` and `{* ... *}` annotations are ignored while preserving line positions. Quoted names containing comment delimiters remain intact.
- Pin files: `Resource` pin declarations; comma-separated `Group` members, including nested group references. Unknown members, cycles, duplicate/missing/empty requested groups cause an error. `#` comments are supported in pin files.
- UTF-8/ASCII inputs. Arbitrary pin expressions, ranges, escaped pin names and full IEEE STIL semantics are not implemented. Source files referenced by `Include` are not auto-resolved; declarations outside the inspected files are not covered.
- This is a targeted checker, not a complete language parser. Always review ERROR/UNDETERMINED and informational absent-field entries.

## Workflow integration boundary

Check-only invocation is the integration interface: JSON contains separate pin,
optional timing, and body/chain CHL eligibility results. The legacy explicit
`--channel-link` exporter still uses source-parent `vN_Channellink` directories.
No ready staging or chunk renaming workflow is implemented in this repository.
An external workflow can consume checker results to implement mutually exclusive
non-chunk ready classification and in-place CHL naming of its own staged chunk
copies; it must not invoke the legacy exporter for that policy. Required checks
must apply to the actual input bytes; original PASS does not certify derivatives.

## Clock knowledge

See [references/clock-reading.md](references/clock-reading.md) for the evidence path from ScanMasterClock through SignalGroups, active waveform table, Spec parameters, vectors and Macro/Shift expansion. Example names and values are synthetic/illustrative, not product timing requirements.

### Executable STIL timing

`scripts/stil_checks.py` uses the streaming grammar in `scripts/stil_timing.py`.
With `--timing`, the entire file is consumed, including gzip CRC validation.
The selected PatternExec/Category/Timing/PatternBurst, ordered groups, `\rN`
data, active `W`, inherited `V {}`, loops, and executed parallel Macro/Shift
vectors establish actual drive edges. Unused macro definitions prove nothing.
Setup requires observed TCK rise/fall/rise cadence (CCD 10 MHz; IOD/DRD 25 MHz).
Body requires consecutive completed 100 MHz pulses on each ScanMasterClock;
chain requires both. Measured high/low widths and idle holds are reported.
A trailing static hold is not a frequency failure. For NRZ TCK, consecutive
non-target intervals (equal or varying) fail; an isolated extended hold may
qualify as idle. Scan clocks may be gated between shift/capture operations:
these inactive-vector gaps are not frequency failures, but adjacent pulsing
vectors must remain 10 ns apart. Unknown clock drives cannot PASS.

Supported execution is deliberately restricted to one selected pattern and
single-vector Shift with equal-length scalar channel arguments. Unsupported
syntax, contexts, unresolved timing, truncation and decompression errors fail
closed; this is not a general STIL simulator. Pin PASS and timing PASS are
separate results. WGL retains its requested relaxed symbolic policy.

## Development and tests

```shell
python -B -m unittest discover -s tests -v
```

Tests generate small synthetic STIL/WGL/pin fixtures in temporary directories, including isolated copy tests. They cover basic direction rules, die policy, `_sc_v<number>` insertion, source-parent placement, timing symbols, repeat-run exclusion and no-write check/preview behavior. No proprietary pattern data is included.

## Sharing and privacy

The tool runs locally and makes no network requests. Do not commit actual STIL/pin files, generated reports, workstation paths, credentials or device data. Default ignore rules cover these common artifacts. Any test run can expose pin/chain names in its JSON; handle output under your organization's data policy.

This project is provided without a license grant at present. For redistribution or use beyond what the repository owner's permissions allow, obtain an appropriate license from the owner.