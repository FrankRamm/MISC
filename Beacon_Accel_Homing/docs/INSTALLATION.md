# Installation and recovery

This document describes the experimental
[FrankRamm/MISC testing branch](https://github.com/FrankRamm/MISC/tree/codex/universal-accel-homing).
It extends the original Beacon Accelerometer Homing project by
[CloakedWayne](https://github.com/CloakedWayne), with GPLv3 and
[original credits/provenance](../NOTICE) retained. Physical validation is still
required before contribution upstream. This is not an endorsed upstream release.

## Download and install with one command

Prerequisites: the existing Linux Klipper service user, Python 3.8+, `bash`, `curl`,
an installed Klipper/Kalico/compatible checkout, and an already
configured supported accelerometer. No Git checkout or sudo is required.

Run this single line as that service user on the Linux printer host:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

The shell downloads the complete bootstrap to a temporary file before
executing it. A failed curl cannot start installation. The bootstrap resolves
the testing branch once to a full commit SHA, then downloads every required
plugin/installer source file, license, NOTICE, examples, documentation and
offline validation report at that commit. It verifies file sizes and Git blob
hashes against immutable GitHub metadata, checks Python syntax and records
cache hashes before invoking the installer. Any failed or incomplete download
aborts before plugin/config installation.

The installer activates the plugin section and X/Y endstops, not just plugin
links. It still refuses ambiguous hosts, accelerometers and unsafe/custom
homing configuration until explicitly resolved. Review the resulting config
and mounting, then issue **`RESTART` yourself while idle**. The installer
does not restart Klipper or start homing/calibration.

Sources remain under
`~/.local/share/accel-homing/FrankRamm--MISC-<full-commit-SHA>/`, with the exact
path and commit printed. Keep that directory: installed module links point
into it. Changed cached sources are rejected. `--install-dir` chooses another
persistent source directory. Both bootstrap entry points default to this
fork/branch; they do not fall back to upstream `main`.

The testing branch can change. Use a full commit SHA for a repeatable install;
see [pinning a published commit](#pin-a-specific-published-commit).

For an already extracted checkout or complete package, no download is needed:

```bash
bash /path/to/Beacon_Accel_Homing/install.sh
```

Use the actual source path reported by bootstrap, or your extracted folder,
in the local commands below.

## Inspect without writes

```bash
bash /path/to/Beacon_Accel_Homing/install.sh --dry-run
```

Dry run validates the package, target, includes, sensors, conflicts and planned
paths. It creates no backup, links or config files and queries no printer API.
It does not prove firmware API compatibility or physical suitability. A real
installation repeats preflight and checks live idle state before writes.
For a hosted dry run, append `--dry-run` to the final `bash "$t"` invocation
in the download command. This still downloads the verified source cache;
printer configuration and plugin paths are left unchanged.

## Explicit selection when needed

For a multi-instance host or multiple configured sensors:

```bash
bash /path/to/Beacon_Accel_Homing/install.sh --klipper-path "$HOME/kalico" --config "$HOME/printer_2_data/config/printer.cfg" --service klipper-2 --accel-chip 'adxl345 ToolHead'
```

Only the full existing sensor section is selected. Suffix case is preserved.
If a live process does not expose `-a`, add `--klippy-socket /actual/klippy_uds`.
The socket's `info.config_file` and available checkout identity must match the
selected target before its idle evidence is accepted. Another idle printer's
socket cannot authorize changes to the selected instance.

Accepted environment overrides are `KLIPPER_PATH`, `PRINTER_CONFIG`,
`KLIPPER_SERVICE`, and `KLIPPY_SOCKET`; command-line options take precedence.
Use `--axes x`/`--axes y` for one-axis endstop activation. Normal calibration is
per axis, but TEST/CHARTS use both X and Y and require suitable sensors/mounting.

Detection prefers actual running klippy arguments, then unambiguous systemd
unit files, then common stopped-host paths. Units requiring unresolved
environment variables, systemd specifiers or drop-ins are not guessed from the
base file: use a readable running command, or stop and resolve the instance
explicitly. More than one matching checkout/config/service or accelerometer
causes an actionable error. No service is started by the installer.

When running, Klipper must report ready, `idle_timeout` Ready/Idle, no active
print and no pause. If the selected API is inaccessible, its identity is
unknown, or status is unsafe, installation aborts. Stop the selected instance
yourself and retry if offline repair is necessary. The installer never stops
or restarts it for you.

## Changes and conflicts

- Existing include files are edited at their real paths. A new
  `accel_homing.cfg` is included before the main `SAVE_CONFIG` block, or through
  an existing matching include wildcard; duplicate includes are avoided.
- `[stepper_x]`/`[stepper_y]` gain the selected `accel_homing:x/y` endstop and
  `homing_retract_dist: 0`. Legacy sections retain `beacon_accel:x/y` aliases.
- Associated X/Y TMC `diag_pin`, `diag0_pin`, `diag1_pin`, `home_current`, and
  rail `homing_accel`/`min_home_dist` are commented with their previous values.
  Run currents, stall thresholds, unrelated macros, other axes, explicit X/Y
  thresholds and saved data remain intact.
- Effective generic speeds above `min(50, max_report_travel/max_sample_age)`
  are capped and can be lowered further for the fixed 3 mm startup request
  budget. The installer shares the runtime formula and uses printer
  `max_velocity`, `max_accel`, and nonzero plugin `homing_accel` limits.
  Slower speeds and Beacon speeds are kept; acceleration is never raised.
  `--homing-speed N` is an explicit optional override and is refused if its
  effective speed violates either policy. No universal speed is known
  to guarantee a detectable impact on all mechanics.
  Runtime checks repeat before enable/backoff, including after later manual
  config edits. This does not promise a physical stopping distance; see
  [the calculation and default 2.65 mm example](COMPATIBILITY.md).
- All six required Python modules are linked into `klippy/extras`. Existing
  regular files at those paths are never overwritten. Links to a different
  source require review and `--replace-existing-links`; the old link targets
  are saved. Existing source files are not deleted.
- Duplicate required sections/options, recursive/duplicate includes, missing
  sensor drivers and an unincluded pre-existing `accel_homing.cfg` fail before
  config writes. Unsupported config structure requires manual resolution.

`[homing_override]`, `[safe_z_home]` or a `G28` macro require manual review.
After adapting their behavior you can explicitly acknowledge that review with
`--acknowledge-homing-override`; this is not automatic validation. The supervised
calibration/test/chart commands still reject a wrapped `G28` because even an
X-only invocation can cause Z movement. Resolve those wrappers to use workflows.

## Prepare calibration after restarting

Before the first calibration command, use the XY motor controls appropriate
for your machine and physically center the toolhead by hand in both X and Y,
keeping Z supported. Both XY axes must be unhomed in Klipper. Do not run a
preparatory home or assign coordinates to mark either axis homed.

Then run in the printer console:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
```

`CENTERED=1` states that the toolhead is already physically centered and
authorizes the first candidate home, starting at 3 g by default. It does not
measure the manual position. After an early trigger, XY is released and made
unhomed; center it manually again and reissue the same calibration command to
resume, keeping the same `AXIS` and optional `SAVE_CONFIG` value.
`ACCEL_HOMING_CONFIRM` is reserved for TEST/CHARTS.

Successful calibration saves X and restarts Klipper by default. Once ready,
repeat the preparation and use `ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` for Y.
See the [full calibration steps](../README.md#calibrate-x-then-y) for validation,
manual threshold overrides and optional saving behavior.

## Backups, rollback and repeated installation

Each changed installation saves a private transaction directory beside the
main config under `.accel_homing_backups/<UTC-timestamp>-<id>/`. Its
`manifest.json` contains exact previous file bytes, permissions and link
targets. All backups are written before the first edit; ordinary write
failures restore already-applied changes automatically. Power-loss or process
termination recovery may need manual inspection of a `prepared` manifest.
Concurrent changes after planning are rejected.

The installer prints the actual backup and a complete rollback command. Use
that path, for example:

```bash
bash /path/to/Beacon_Accel_Homing/install.sh --rollback "$HOME/printer_data/config/.accel_homing_backups/ACTUAL-BACKUP-DIRECTORY"
```

Rollback restores the selected transaction, including original endstops and
sensorless settings. It refuses if any affected file/link was edited after
installation, preserving later changes for manual reconciliation.
`--uninstall`/`-u` selects the latest unchanged installed transaction for the
detected config; after multiple upgrades this rolls back one upgrade, not the
entire installation history. `--dry-run` can be combined with rollback.

A second install of the same package/config makes no further edits or backups.
An installer never changes firmware, wiring or physical geometry. After install
or rollback, review the result and issue `RESTART` yourself only while idle.

## Pin a specific published commit

Replace both occurrences of `FULL_40_CHARACTER_COMMIT_SHA` below with the full
published commit SHA you have selected from the testing fork. Use the same SHA
in the download URL and `--ref`; a branch name is not an immutable version:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/FULL_40_CHARACTER_COMMIT_SHA/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref FULL_40_CHARACTER_COMMIT_SHA'
```

No Git dependency, automatic update manager, service restart or motion is added.

The offline download fixtures in `tests/test_installer.py` verify branch
resolution, complete downloads, integrity checks, retained credits and
download-failure handling without calling GitHub or a printer.
