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

Discovery cannot prove that no other instance uses a checkout. Custom stopped
instances outside the recognized paths, unreadable service definitions and
unresolved environment/drop-in configuration may not be discovered. Review
those host-specific instances when a checkout is shared.

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

Standard Klipper/Kalico `[safe_z_home]` is accepted without an acknowledgement
flag. Its section and existing probe/Z settings are preserved. This includes
Cartographer configurations that use the built-in safe Z homing path.
Calibration/test/chart workflows verify the active built-in handler and its
underlying stock `G28`, then call that stock handler for X or Y only so the
wrapper's `z_hop` is not invoked. Normal `G28` continues through the existing
safe Z handler. See [the source contract](COMPATIBILITY.md#safe_z_home-and-xy-only-workflows).

`[homing_override]` and `G28` macros still require manual review before
installation. After adapting their behavior you can explicitly acknowledge
that review with `--acknowledge-homing-override`; this does not validate a
custom path. Workflows still reject these paths and unknown wrapper chains,
including a custom handler beneath or above `safe_z_home`.

## Prepare calibration after restarting

Before the first calibration command, use the XY motor controls appropriate
for your machine and physically center the toolhead by hand in both X and Y,
keeping Z supported. Ensure nozzle/bed clearance for XY travel before
CALIBRATE, TEST or CHARTS; those workflows do not perform the safe Z hop.
Both XY axes must be unhomed in Klipper. Do not run a preparatory home or
assign coordinates to mark either axis homed.

Then run in the printer console:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
```

`CENTERED=1` states that the toolhead is already physically centered and
authorizes the first candidate home, starting at 2 g by default. It does not
measure the manual position. After an early trigger, XY is released and made
unhomed; center it manually again and reissue the same calibration command to
resume, keeping the same `AXIS` and optional `SAVE_CONFIG` value.
`ACCEL_HOMING_CONFIRM` is reserved for TEST/CHARTS.

Successful calibration saves X and restarts Klipper by default. Once ready,
repeat the preparation and use `ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` for Y.
See the [full calibration steps](../README.md#calibrate-x-then-y) for validation,
manual threshold overrides and optional saving behavior.

## Uninstall and restore the pre-installation configuration

Run this single line as the existing Klipper service user in the Linux host
shell:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/uninstall.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

This downloads the actual `uninstall.sh` before executing it. The standalone
script uses the same complete, verified, commit-pinned package download as
installation. It always selects full uninstallation. No extracted ZIP or
source-cache path is needed. For a preview, append `--dry-run` to the final
`bash "$t"` invocation. Instance selection options such as `--config`,
`--klipper-path`, `--service` and `--klippy-socket` work there too.

From a verified source cache or complete Git checkout, the offline equivalent
is:

```bash
bash /path/to/Beacon_Accel_Homing/uninstall.sh
```

An ordinary extracted folder without verified cache metadata or tracked Git
files uses the hosted verified download instead.

Full uninstallation follows all active installation and upgrade transactions
for the selected config back to the first recorded baseline. It restores the
previous X/Y endstops, retract/speed settings, sensorless driver options and
plugin links. A plugin section, file or link that already existed before the
first recorded install is restored to that baseline rather than deleted.

It removes installer-created includes/configuration and saved
`accel_homing_model` sections introduced by this installation. Pre-existing
plugin models are restored. Other `SAVE_CONFIG` data, including Beacon and
bed-mesh models, and unrelated later configuration edits are preserved.
Source caches, chart output and all backup history remain on disk.

Module links belong to the checkout and can serve several printer instances.
If another discovered config uses that checkout and restoration would change
those links, uninstall refuses before writes. Resolve that shared-checkout
dependency manually, accounting for any custom instances discovery cannot see.

Restoration requires an intact, unambiguous backup history and recognizable
installer edits. A changed managed setting/link, unrelated sections added to
an installer-created config file, a missing original link target, unrecorded
plugin references, broken restored includes or incomplete history causes a
conflict before configuration/plugin writes. The error identifies the affected
path and backup for review. There is no force option that overwrites later edits.

Older version-1 backups recorded only changed files. If they lack the main
config baseline needed to distinguish pre-existing saved models from later
calibration, full uninstall refuses rather than guessing. New backups retain
all baseline config files, including an unchanged main config, so routine
calibration can be removed without losing other saved data.

Running Klipper must pass the same selected-instance idle checks as install.
Uninstallation never stops/restarts the service or starts movement. Review
the restored config and issue **`RESTART` yourself only while idle**.

## Backups, single-transaction rollback and recovery

Each changed installation saves a private transaction directory beside the
main config under `.accel_homing_backups/<UTC-timestamp>-<id>/`. Its
`manifest.json` contains exact previous file bytes, permissions and link
targets. All backups are written before the first edit; ordinary write
failures restore already-applied changes automatically. Power-loss or process
termination recovery may need manual inspection of a `prepared` manifest.
Concurrent changes after planning are rejected.

Before restoring files, full uninstall writes another transaction backup of
the current configuration and links, including plugin models it will remove.
It prints `Uninstalled. Current-state backup: ...` and a complete
`Undo this uninstall` command. That command uses `install.sh --rollback` with
the new backup, restoring the state immediately before uninstall if the
restored files/links have not changed since. Keep this backup as well as the
original installation history.

The installer prints the actual backup and a complete rollback command. Keep
that first-installation backup and every later transaction directory. Full
uninstall needs their history to identify the original configuration.

`--rollback` restores exactly one selected transaction. Use its printed path,
for example:

```bash
bash /path/to/Beacon_Accel_Homing/install.sh --rollback "$HOME/printer_data/config/.accel_homing_backups/ACTUAL-BACKUP-DIRECTORY"
```

Rollback refuses if any affected file/link was edited after that transaction,
preserving later changes for manual reconciliation. After an upgrade, it
returns to the previous installed version; it does not remove the complete
installation. `--dry-run` can be combined with rollback. The installer's
`--uninstall`/`-u` is equivalent to the full `uninstall.sh` operation above;
`uninstall.sh` itself rejects `--rollback` to keep those actions distinct.

When restoration reports a conflict, keep the printer idle and preserve the
current config plus the entire `.accel_homing_backups` directory. Inspect the
reported original backup's `manifest.json`: each changed path has its original
`before` state, with base64 file contents, permissions, or the original link
target. Compare those values with the live file and the recorded `after`
state. Reconcile only the installer-managed settings, retaining unrelated
edits and saved data. Restore original endstop/sensorless options before
removing plugin includes or module links. Do not delete the backups or copy
an old whole `printer.cfg` over current calibration data.

If the original link target has been removed, recover that original source at
the recorded path before retrying. Restoring a link to a missing Python module
would leave Klipper unable to load the previous plugin.

For an older incomplete backup, use a genuine pre-installation config copy
to identify the original endstops, sensorless settings and plugin models.
If no such baseline exists, the recorded changed-file snapshots can recover
their original settings, but unrecorded saved-model ownership needs manual
review. The uninstaller cannot reconstruct information that was never backed
up.

A second install of the same package/config makes no further edits or backups.
An installer never changes firmware, wiring or physical geometry. After install,
uninstall or rollback, review the result and issue `RESTART` yourself only while
idle.

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
