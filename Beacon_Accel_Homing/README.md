# Accelerometer Homing

Experimental X/Y impact homing for Klipper, Kalico and forks that expose the
required Klipper APIs. Uses an already configured Beacon or a supported
accelerometer object. It adds supervised threshold calibration, independent
X/Y overrides, measured charts, and a transactional installer.

The original Beacon Accelerometer Homing plugin and impact-detection algorithm
are by **[CloakedWayne](https://github.com/CloakedWayne)** in
[Monolith3D/MISC](https://github.com/Monolith3D/MISC/tree/main/Beacon_Accel_Homing).
This experimental extension is maintained in the
**[FrankRamm/MISC testing fork](https://github.com/FrankRamm/MISC/tree/codex/universal-accel-homing)**,
on `codex/universal-accel-homing`, for supervised testing and eventual
contribution upstream after physical validation. It is not an endorsed
upstream release. Historical Beacon tests do not validate the new generic
backends or workflows. See [credits and provenance](NOTICE).

Impact homing deliberately contacts a mechanical stop. Software checks and ten
consistent trials do not prove a correct physical home, absence of skipped
steps, safe mounting, or a guaranteed stopping distance. The operator performs
the first homes and all calibration with an accessible emergency stop.

## Install with one command

On the Linux printer host, run this as the existing Klipper service user.
It requires Python 3.8+, `bash`, `curl`, an installed Klipper/Kalico/compatible
checkout, and an already configured supported accelerometer:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

The command downloads the complete bootstrap before executing it. The
bootstrap resolves the testing branch once to a full commit SHA, downloads and
verifies all required source, license, credit and documentation files at that
revision, then runs the configuration installer. A failed or incomplete
download aborts before installation. Sources stay in a versioned directory
under `~/.local/share/accel-homing/`; the exact path and commit are printed.
For a reproducible release, use a full commit SHA in both the download URL and
`--ref`, as described in [installation details](docs/INSTALLATION.md).

The installer detects the Klipper checkout, executable, main config, configured
accelerometer and, when available, systemd service from the running `/proc`
command, systemd unit files, or common stopped-host directories. On an
unambiguous host it installs all plugin files, creates the plugin section,
activates actual X/Y endstops in their existing files, disables the associated
sensorless-only settings, and backs up every changed file/link. It preserves
unrelated macros, driver currents, existing independent thresholds and the
entire `SAVE_CONFIG` block. Repeating the same installation is idempotent.

It refuses ambiguous instances/sensors and custom homing paths until explicitly
resolved. It never guesses wiring, flashes firmware, uses sudo, restarts
Klipper, or starts movement. If Klipper is running, file writes require a
read-only API response proving the selected config is ready and idle. If its
status cannot be verified, stop that instance yourself and retry.

Review the resulting config and sensor mounting, then issue **`RESTART`
yourself while idle** to load the plugin. Keep the downloaded source directory:
the installed links point into it.

[Installation details, dry run, multi-instance selection, pinned downloads
and rollback](docs/INSTALLATION.md).

## What can be detected

| Configured sensor | Data path | Compatibility evidence |
| --- | --- | --- |
| Beacon (`[beacon]` or named Beacon section) | Existing Beacon raw accelerometer stream | Original Beacon detector retained; new behavior tested with offline fixtures |
| ADXL345, LIS2DW, LIS3DH, MPU9250, ICM20948 | Configured Klipper `batch_bulk`, or the verified older `api_dump` contract | Driver/API source inspection and synthetic batch/error/timing tests |
| Cartographer built-in accelerometer | Its separately configured `[adxl345 ...]` or `[lis2dw ...]` object | Standard Klipper accelerometer path; not the Cartographer distance/probe stream |
| Other compatible fork/sensor object | Explicit `accel_chip` and declared `sensor_range` if unknown | Runtime capability checks; no general hardware compatibility claim |

The board's brand is not the selection criterion. Its existing Klipper
accelerometer object must provide supported machine-mapped acceleration in
mm/s², valid timestamps, sufficient sample rate and error counters. Configure
the normal driver and MCU firmware first. The installer does not add firmware
features or establish an electrical interface. A missing or incompatible API
fails explicitly.

The sensor must physically follow motion on the tested axis, and its existing
`axes_map` must match the machine. A toolhead-only accelerometer on a bed-slinger
cannot detect a Y stop caused by moving the bed. Auto-detection cannot establish
mounting or orientation. Use `--axes x` for an installation intended only for X;
the supervised test/chart sequence requires usable accelerometer endstops on
both X and Y. No dual-sensor routing is implemented.

Generic drivers normally batch data for resonance measurements. This plugin
temporarily requests a 5 ms reporting interval and restores it afterwards. The
default accepted sample age is 20 ms, minimum configured/observed rate is
1000 Hz, and homing speed is capped at `min(50, max_report_travel /
max_sample_age)` mm/s, which is 50 mm/s at the default 1 mm travel budget.
The installer lowers higher effective generic homing speeds to that limit and,
when acceleration requires it, lowers them further to satisfy the startup
budget below. It preserves slower speeds and never raises acceleration. An
explicit incompatible `--homing-speed` is refused before writes. Beacon keeps
its existing homing speed. A soft impact at a lower
speed can still be undetectable. These limits are software acceptance bounds;
Python scheduling, USB/CAN delivery and the MCU stop are not hard real-time
guarantees.

While no impact has completed a generic home, its startup guard requires
measured motion by a calculated deadline. Missing proof then causes a
`no_motion` failure; the generic path never reports successful already-pressed
detection. Ordinary `G28` can still finish on a fresh threshold impact before
motion is proved; calibration/test/chart workflows apply stricter motion and
travel checks. Before motor enabling/backoff, the plugin rejects an
effective speed/acceleration plan whose nominal commanded travel before the
host stop request would exceed a fixed **3 mm** policy. At 50 mm/s,
5000 mm/s², 20 ms accepted sample age and a 5 ms report interval, the deadline
is 55 ms and the nominal request budget, including one 3 ms watchdog interval,
is 2.65 mm. Host scheduling stalls, transport and the physical MCU stop are
excluded: this is not a 3 mm physical stopping guarantee. Beacon retains its
legacy already-pressed detection. See the exact calculation in the
[compatibility notes](docs/COMPATIBILITY.md).

See [source/API evidence and limitations](docs/COMPATIBILITY.md) and
[offline verification](TESTING.md).

## Configuration

The installer creates and includes an editable `accel_homing.cfg` like this:

```ini
[accel_homing]
accel_chip: adxl345 toolhead
#x_threshold: 8.0
#y_threshold: 8.0
#model: default
```

Use the full **existing** sensor section name. `accel_chip: auto` is accepted
only when exactly one recognized accelerometer is configured. A named sensor
such as `[adxl345 ToolHead]` retains its case-sensitive suffix.

`x_threshold` and `y_threshold` are independently editable in g. Higher values
need a stronger impact and may miss a stop; lower values can trigger on motion
vibration. Leave them commented to use calibrated model values. Explicit
per-axis settings override the model on restart; the legacy shared `threshold`
also overrides both model defaults. MPU9250 uses its driver's verified 8 g
range and a sensor-aware initial default; an 8 g threshold is too high for it.

The existing stepper sections use:

```ini
[stepper_x]
endstop_pin: accel_homing:x
homing_retract_dist: 0

[stepper_y]
endstop_pin: accel_homing:y
homing_retract_dist: 0
```

These are edits to existing sections, not duplicate sections to paste beside
them. The installer makes these edits and records the original settings in
comments and its rollback backup. See [all examples](examples/accel_homing.cfg).

Historical `[beacon_accel_homing]`, `beacon: beacon`, `beacon_accel:x/y`,
`BEACON_ACCEL_HOMING_STATUS` and `SET_BEACON_ACCEL_HOMING` remain accepted. Do not
load both old and new plugin sections. Existing Beacon thresholds are kept.

## Supervised calibration

The following commands move the printer. They are operator instructions, not
commands executed by installation or by offline tests. Workflows require idle,
unpaused Klipper, independent Z, Cartesian/CoreXY kinematics, stock `G28`, no
`homing_override`/`safe_z_home`/G28 macro, and single-touch X/Y settings. They
reject wrappers that could silently introduce Z movement.

1. Run `ACCEL_HOMING_CALIBRATE AXIS=X`. It sets a low candidate threshold and
   performs one initial seed X home. The seed is not a validated home.
   A generic startup `no_motion` fault aborts; it does not establish an origin
   or count as a success. Resolve the mounting, motion or configuration issue
   before retrying.
2. The plugin releases XY and displays a centering prompt. Physically position
   the carriage at the machine's real X/Y center. A console message contains
   `ACCEL_HOMING_CONFIRM TOKEN=<current nonce> CENTERED=1`; send that exact
   command, or use the prompt's Centered button. A stale token is rejected.
3. A centered discovery home must qualify before automatic validation starts.
   The plugin then performs **ten additional consecutive qualified homes** at
   the same threshold, returning only the tested axis to its center. A clean
   run therefore has one seed, one discovery, and ten validation homes.
4. An early/short trigger or already-pressed result does not cause an automatic
   return. The plugin raises a candidate only for early triggers, resets the
   count when needed and asks for manual centering again. Missing/late data,
   long travel, MCU errors and invalid capture data abort instead of increasing
   the threshold blindly. Attempts and thresholds are bounded.
5. After ten successes the default `SAVE_CONFIG=1` writes a named model and
   restarts Klipper. Repeat with `ACCEL_HOMING_CALIBRATE AXIS=Y` to calibrate Y
   independently. `SAVE_CONFIG=0` stages results for a later operator-issued
   `SAVE_CONFIG` instead.

The saved model appears under Klipper's **DO NOT EDIT** block in `printer.cfg`:

```ini
#*# [accel_homing_model default]
#*# version = 1
#*# x_threshold = <measured accepted candidate>
#*# x_successes = 10
#*# x_metadata = <sensor/config/measurement metadata>
```

This is an illustration, not a block to paste manually. The model remains
separate from editable manual X/Y overrides. Changes to mounting, belts,
sensor orientation, homing speeds or current settings can invalidate a model.

`ACCEL_HOMING_STATUS` reports progress and model state.
`ACCEL_HOMING_CANCEL` cancels a waiting/active supervised workflow. Homing faults
retain stock Klipper error behavior; the deliberate centering prompt releases
XY only. Firmware step distances and travel checks cannot prove that the
mechanism actually contacted the correct stop.

## Test and measured charts

`ACCEL_HOMING_TEST` releases XY and asks for explicit manual centering first.
After confirmation it homes X, validates the X travel, returns X alone to
center, then homes Y. A suspect home aborts before an automatic return. The
workflow itself never requests a Z move.

`ACCEL_HOMING_CHARTS_GENERATE` uses the same supervised X-then-Y sequence and
exports the captured samples for each axis as full-resolution CSV, metadata
JSON, SVG and HTML. No separate plotting installation is needed. Output paths
are reported in the console and by `ACCEL_HOMING_STATUS`; the default directory
is `accel_homing_charts/` beside the selected main `printer.cfg`. `charts_directory`
can select another writable directory.

The chart plots baseline-subtracted measured X/Y/Z acceleration, the selected
threshold and the detected onset when available. Inspect noise versus impact
on the relevant axis. Motion-correlated data is evidence for tuning, not
physical position verification. Synthetic test captures are explicitly marked
**SIMULATED / NOT PRINTER MEASUREMENT**; no printer measurements were collected
for this rewrite.

## Credits, license and history

**CloakedWayne is the original author of Beacon Accelerometer Homing and its
Beacon impact-detection algorithm.** The base is upstream commit
[`f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4`](https://github.com/Monolith3D/MISC/commit/f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4),
which retains its original `Co-Authored-By: Claude Opus 5.5` provenance. The
original project credited FrankRamm and Ingstronmentalist for printer testing.
Those historical results do not validate this extension or generic hardware.

The FrankRamm testing fork adds the experimental generic accelerometer
backends, supervised workflows, saved models, charts and transactional
installation. It is intended for contribution upstream after physical
validation. GPLv3 is retained; see [LICENSE](LICENSE) and the distributed
[NOTICE](NOTICE) for full credits and provenance.
