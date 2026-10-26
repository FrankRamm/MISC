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
configured supported accelerometer. The default activation also requires a
verifiable systemd service and permission to restart it; the installer requests
your sudo password when needed. Run the command as the service user, without
prefixing the complete installation with `sudo`. No Git checkout is required.

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
homing configuration until explicitly resolved. After installation succeeds,
it rechecks the selected instance and **automatically restarts its verified
systemd service**. It does not start homing or calibration. Review the resulting
config and mounting before either.

Console `RESTART` and `FIRMWARE_RESTART` do not reload already imported Python
modules. A real host-process restart is required after installing/updating
code or changing module links. Later configuration-only threshold edits can
use console `RESTART`; normal calibration `SAVE_CONFIG` also remains unchanged.
See [the source evidence](COMPATIBILITY.md#loading-code-and-configuration).

## Automatic host-service restart

The installer restarts the selected instance's exact verified systemd unit;
it does not assume a service named `klipper.service`. It checks the loaded
unit and effective `ExecStart` against the selected checkout, main config,
API socket and any running process. For running instances, it also verifies
the main process identity. Environment-based units can use that live process
as evidence without executing or interpreting their environment files.
Automatic restart supports the system manager; user systemd managers require
`--no-restart` and manual activation.

An unknown service manager, unverifiable unit/process or unavailable restart
permission fails before installation writes. The installer obtains any needed
sudo privilege before the initial idle check and file changes, with at most
60 seconds for authorization. It then runs the service restart noninteractively
with a 45-second timeout. Read-only service and privilege probes have five-second
timeouts. The installation command itself does not need to run as root.

After a successful transaction, the installer repeats unit/process identity
and, for a running instance, read-only printer idle/pause checks immediately
before restart. Once the
service restart completes, it verifies a new active Klipper process matching
the selected instance. This is a full host-process restart, which loads changed
Python modules. It is separate from calibration's normal `SAVE_CONFIG` restart.
The process check does not establish that Klipper reached Ready, that a module
works correctly, or that hardware behavior is correct.
These checks cannot atomically prevent another client from starting work after
the last idle response; keep the printer idle throughout installation.

A stopped selected service can be started by the default restart when its
effective `ExecStart` literally identifies the same checkout, config and API
socket. Stopped environment-based or wrapped commands cannot supply live
process identity and need `--no-restart`.

To install without restarting or starting the service:

```bash
bash /path/to/Beacon_Accel_Homing/install.sh --no-restart
```

For a hosted installation, append `--no-restart` to the final `bash "$t"`
invocation in the download command. After reviewing the result, restart the
selected host process yourself while idle, using the printed service command
or that instance's actual process manager. An unsupported manager requires
this option. Dry runs, full uninstall and explicit rollback never restart or
start the service automatically.

Installing the same unchanged package/config creates no managed config/link
edits or new transaction backup, but still restarts the verified service by
default so code activation does not depend on whether files needed changing.
Its activation receipt is updated. `--no-restart` skips that activation too.
If activation fails after installation, the installed files and transaction
backup remain available and the command returns a failure. The separate
`activation.json` beside the backup (or `.accel_homing_activation.json` when
there is no backup) records the activation result without changing transaction
history hashes. A failed activation is not a successful deployment; review the
reported failure before retrying or rolling back.

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

## Update an existing installation

Each downloaded commit has its own immutable source directory. Updating an
installed version therefore requires reviewing the new release and explicitly
allowing its module links to replace the previous targets:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing --replace-existing-links'
```

`--replace-existing-links` backs up the existing link targets and repoints them
to the verified new source. Keep all source directories and transaction backups
for rollback. Without this option, a different existing link target is refused
before any installation writes; a completed download alone does not update
the installed plugin. A successful update automatically restarts the verified
selected service after the same fresh idle and identity checks as installation.
Append `--no-restart` to defer that activation to a manual host-process restart.

For a pinned update, use the same reviewed full commit SHA in the download URL
and `--ref`, retaining `--replace-existing-links`; see
[pinning a published commit](#pin-a-specific-published-commit).

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
`--klippy-socket /actual/klippy_uds` can supply a known selected-instance
socket for read-only idle checks. Automatic restart additionally requires that
socket to match the verified service/process arguments; the override cannot
replace that identity evidence. If a running process does not expose `-a`,
use `--no-restart` with the known socket and activate the host process manually.
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
causes an actionable error. The default activation can start a stopped verified
systemd service; `--no-restart` leaves it stopped.

Discovery cannot prove that no other instance uses a checkout. Custom stopped
instances outside the recognized paths, unreadable service definitions and
unresolved environment/drop-in configuration may not be discovered. Review
those host-specific instances when a checkout is shared.

When running, Klipper must report ready, `idle_timeout` Ready/Idle, no active
print and no pause. If the selected API is inaccessible, its identity is
unknown, or status is unsafe, installation aborts. For offline repair, stop the
selected instance yourself and use `--no-restart` to keep it stopped while
reviewing the result. The installer does not stop an active print or override
an unsafe status to complete installation.

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
- Configured `homing_speed` is preserved by default for every sensor. Generic
  plans must still satisfy `max_report_travel/max_sample_age` and the fixed
  3 mm startup request budget. The default 1 mm / 0.020 s settings give a
  50 mm/s reporting limit; there is no separate fixed 50 mm/s cap.
  An incompatible plan produces an
  actionable conflict before config/module writes or activation; the installer
  does not select a lower speed. It shares the runtime formula and uses printer
  `max_velocity`, `max_accel`, and nonzero plugin `homing_accel` limits.
  `--homing-speed N` is a deliberate optional speed edit and is refused if its
  effective speed violates either policy. Acceleration is never raised.
  No universal speed is known to guarantee a detectable impact on all mechanics.
  Installation does not change timing settings or acceleration to admit a
  speed. Updating preserves a speed previously written by an older installer;
  it does not automatically restore an earlier value from backup history.
  Runtime checks repeat before enable/backoff, including after later manual
  config edits or temporary live acceleration changes. CALIBRATE/TEST/CHARTS
  also check requested generic X/Y plans before any workflow effect, even
  when XY is unhomed. Refusals report effective speed/acceleration and a lower
  `homing_speed` ceiling; a plugin cap cannot raise a lower live acceleration.
  This does not promise a physical stopping distance; see
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
safe Z handler, whose configured hop may occur before a later X/Y motion-policy
refusal. The workflow preflight does not intercept public `G28` or change Z
settings. See [the source contract](COMPATIBILITY.md#safe_z_home-and-xy-only-workflows).

`[homing_override]` and `G28` macros still require manual review before
installation. After adapting their behavior you can explicitly acknowledge
that review with `--acknowledge-homing-override`; this does not validate a
custom path. Workflows still reject these paths and unknown wrapper chains,
including a custom handler beneath or above `safe_z_home`.

## Prepare calibration after the host-process restart

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
authorizes the first candidate home, starting at 0.5 g by default. It does not
measure the manual position. After an early trigger, XY is released and made
unhomed; center it manually again and reissue the same calibration command to
resume, keeping the same `AXIS` and optional `SAVE_CONFIG` value. The console
reports manual-recentering instructions; the pending calibration retains its
configured timeout. The default 0.5 g step makes the first retry 1.0 g.
One geometry-qualified discovery must be followed by ten additional consecutive
geometry-qualified homes at the same candidate. Their commanded-travel range
(`max - min`) must be **strictly less than 1.5 mm**; exactly 1.5 mm fails.
Discovery and manual centering are excluded, and each axis has its own window.
If that validation range reaches 1.5 mm, calibration aborts before another
automatic return/home, without increasing the candidate or staging, saving or
restarting. Existing early-hit retries still clear the count and distance
window; after manual recentering, the new candidate needs another qualifying
discovery and ten fresh validations. These commanded distances do not prove
contact with the correct stop or absence of skipped steps.
Calibration and saved models accept finite values from 0.5 g; existing models
at 1 g and 2 g remain valid. Manual threshold settings and temporary SET
commands retain their 3–15 g range. An explicit existing
`calibration_start_threshold` is preserved; set it to `0.5` or leave it absent
to use the new default. Updating does not replace a saved model's threshold;
run calibration to create a new one. Previous models remain loadable, but
updating does not qualify their historical distances under the new 1.5 mm rule.

TEST and CHARTS require the same existing manual XY center, both axes unhomed
and nozzle/bed clearance. Start them directly with
`ACCEL_HOMING_TEST CENTERED=1` or
`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`. They do not release XY before the
first home or request a separate confirmation. `CENTERED=1` is mandatory and
authorizes movement after stationary preparation succeeds.

Successful calibration uses `SAVE_CONFIG=1` by default: it writes the model to
the generated `SAVE_CONFIG` block of `printer.cfg` and restarts Klipper.
To defer both actions, start with
`ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1 SAVE_CONFIG=0`. That only stages the
model in memory and restores the previous active thresholds; you must issue
`SAVE_CONFIG` yourself to write it and restart. The calibrated threshold is
used after reload unless a manual threshold override takes priority. An
ordinary `RESTART` discards an unsaved staged model.
Once ready, repeat the preparation and use
`ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` for Y.
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
the restored config, then **restart the selected Klipper host process yourself
while idle**, using the service command printed by the uninstaller. This
applies restored modules and removes the previously imported plugin code.

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

A second install of the same package/config makes no further managed
config/link edits or transaction backups, but repeats default service
activation and updates its receipt. `--no-restart` records the opt-out without
restarting the service.
An installer never changes firmware, wiring or physical geometry. Full uninstall
and explicit rollback require a manual restart of the selected host process
while idle. Restoring old module links also requires a real process restart;
console `RESTART` does not reload the previous Python implementation.

## Pin a specific published commit

Replace both occurrences of `FULL_40_CHARACTER_COMMIT_SHA` below with the full
published commit SHA you have selected from the testing fork. Use the same SHA
in the download URL and `--ref`; a branch name is not an immutable version:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/FULL_40_CHARACTER_COMMIT_SHA/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref FULL_40_CHARACTER_COMMIT_SHA'
```

For an existing installation, append `--replace-existing-links` to that final
`bash "$t"` command after reviewing the selected release, as in the update
command above. This option preserves the old targets in the transaction backup.

No Git dependency, automatic update manager or motion is added. Installation
and updates use the automatic host-service restart described above unless
`--no-restart` is selected.

The offline download fixtures in `tests/test_installer.py` verify branch
resolution, complete downloads, integrity checks, retained credits and
download-failure handling without calling GitHub or a printer.
