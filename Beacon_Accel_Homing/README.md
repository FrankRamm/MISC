# Accelerometer Homing

Use an already configured accelerometer as an X/Y impact endstop in Klipper,
Kalico or a compatible fork. Optional commands help you calibrate separate
X/Y thresholds, test them and chart the measured acceleration.

**[CloakedWayne](https://github.com/CloakedWayne) created the original Beacon
Accelerometer Homing plugin and its Beacon impact-detection algorithm** in
[Monolith3D/MISC](https://github.com/Monolith3D/MISC/tree/main/Beacon_Accel_Homing).
This experimental extension is developed in the
[FrankRamm testing fork](https://github.com/FrankRamm/MISC/tree/codex/universal-accel-homing)
for supervised testing and future upstream contribution after physical
validation. Full credits and provenance: [NOTICE](NOTICE). License:
[GPLv3](LICENSE).

Impact homing deliberately contacts a mechanical stop. Supervise the first
homes and all calibration with an accessible emergency stop. This extension
still needs physical validation; software checks do not prove a correct home.

## Before you start

- A Linux printer host with Klipper, Kalico or a compatible checkout, plus
  Python 3.8+, `bash` and `curl`. Install as the existing Klipper service user.
- An already working Beacon, ADXL345, LIS2DW, LIS3DH, MPU9250 or ICM20948
  accelerometer. Cartographer needs its separately configured `adxl345` or
  `lis2dw` accelerometer object.
- A firmly mounted sensor that moves with each tested axis, with the correct
  `axes_map`. A toolhead sensor cannot measure moving-bed Y impacts.
- For calibration, test and charts: Cartesian/CoreXY with independent Z and
  stock `G28`, directly or beneath the recognized built-in `[safe_z_home]`.
  Other custom homing paths require changes before these workflows can run;
  see the [optional compatibility notes](#for-the-curious).

## 1. Install on the Linux printer host

Run this single line in the host's **shell**, not the printer console:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

The installer downloads verified files, detects the Klipper checkout, config
and configured sensor, then installs the plugin and activates its X/Y
endstops. It backs up changed files, disables conflicting sensorless settings
and keeps existing thresholds, driver run currents and saved configuration.
Generic accelerometers may require lower homing speeds, which it adjusts.

Multiple printer instances or sensors need explicit selection. Configure
sensor firmware and wiring beforehand. Installation does not start movement
or restart Klipper.

Review the resulting configuration and mounting. Then, **while idle**, run the
host service restart command printed by the installer in the host's **shell**.
Use its selected service name; restarting the Klipper host process loads the
installed Python modules. Printer-console `RESTART` or `FIRMWARE_RESTART`
alone does not reload already imported code.

Keep the printed source directory under `~/.local/share/accel-homing/`; the
installed links use it. See [installation details](docs/INSTALLATION.md) for
manual selection, dry run, pinned downloads, backups and rollback.
For an existing installation, use the
[update command](docs/INSTALLATION.md#update-an-existing-installation) with
`--replace-existing-links` to back up and repoint the previous source links.

## 2. Set thresholds

You can calibrate each axis below, or edit thresholds in your existing
`[accel_homing]` or legacy `[beacon_accel_homing]` section. New installations
create the section in `accel_homing.cfg`. Example for an ADXL345:

```ini
[accel_homing]
accel_chip: adxl345 toolhead
#x_threshold: 8.0
#y_threshold: 8.0
```

Keep your existing section and sensor name; do not add a second section.
Leave thresholds commented to use saved calibration values. To override one
axis, uncomment its setting, change the value and issue `RESTART` while idle.
That console command is sufficient for these configuration-only edits.
Thresholds are in **g**: higher values
require a stronger impact; lower values can trigger on motion vibration.
Manual settings take priority over saved calibration values.

## 3. Use normal homing

After physical validation, use **`G28 X`** or **`G28 Y`** in the printer console.
The configured axis uses its accelerometer endstop. Calibration, tests and
charts are optional workflows that run only when you request them.

Energizing a motor can jolt the sensor and resemble contact. Before every
homing pass, the plugin waits for setup to finish and checks fresh, quiet
accelerometer data. Newly enabled motors get an additional 0.25 s settling
delay by default (`enable_settle_time`). If the data does not settle, that
homing pass aborts before it starts. This check still needs physical validation.

All commands below go in the **printer console**:

| Command | What it does |
| --- | --- |
| `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1` | Starts or resumes X calibration after manually centering XY, with both axes unhomed. Use `AXIS=Y` for Y. |
| `ACCEL_HOMING_TEST CENTERED=1` | Tests X then Y with current thresholds after manually centering XY, with both axes unhomed. |
| `ACCEL_HOMING_CHARTS_GENERATE CENTERED=1` | Runs the same X-then-Y test and exports measured acceleration charts. |
| `ACCEL_HOMING_STATUS` | Shows thresholds, recent results, calibration progress, model and chart paths. |
| `ACCEL_HOMING_CANCEL` | Cancels a pending calibration/manual-recentering wait and restores temporary settings. |
| `SET_ACCEL_HOMING AXIS=X THRESHOLD=4.0` | Optional temporary X threshold change until restart; use `AXIS=Y` for Y. |

## Calibrate X, then Y

Before CALIBRATE, TEST or CHARTS, ensure the nozzle has clearance above the bed
for XY travel. These workflows do not perform `safe_z_home`'s Z hop.

1. **Before the command, physically center the toolhead by hand in both X and
   Y.** Use the XY motor controls appropriate for your machine so Z stays
   supported. Both X and Y must be **unhomed in Klipper**. Do not home them in
   advance or assign coordinates to mark them homed.
2. Run `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`. `CENTERED=1` states that you
   have already centered XY and authorizes movement; the sensor cannot detect
   that position. **There is no further confirmation: X starts after the
   stationary preparation check.** The first home tests the actual candidate
   threshold, starting at **2 g by default**, from that manual center.
3. After one qualifying home, the plugin performs **ten additional consecutive
   accepted homes** at the same threshold. Between accepted trials, it returns
   only the tested axis to center.
4. If it triggers too early, the plugin raises the candidate threshold, resets
   the validation count and releases XY, leaving both axes unhomed. Center XY
   manually again, then repeat **`ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`** to
   resume the pending candidate and attempt count. It does not return
   automatically after a suspect home. Sensor faults or excessive travel abort
   calibration.
5. On success, the default `SAVE_CONFIG=1` automatically saves that axis in
   the **DO NOT EDIT** block of `printer.cfg` and restarts Klipper. Do not edit
   that generated block. Once ready, repeat the manual preparation and run
   **`ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1`**. X and Y are saved independently.

Keep the same calibration command, `AXIS` and optional `SAVE_CONFIG` value
when resuming after manual recentering. The console reports when recentering
is needed; the pending calibration expires after its configured timeout.

Existing manual threshold overrides remain active even after calibration.
Comment them out if you want the saved model to supply the thresholds.
Recalibrate after changes to mounting, belts, sensor orientation, homing speed
or motor current.

## Test and view charts

Physically center XY by hand and leave both axes unhomed before either command,
using the same manual preparation and nozzle/bed clearance as calibration.
Then run **`ACCEL_HOMING_TEST CENTERED=1`** or
**`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`**. `CENTERED=1` declares that this
preparation is complete and authorizes movement. Omitting it fails before
movement. X starts after stationary preparation, with no separate confirmation
or preliminary XY release.

They home **X, return X alone to center, then home Y**. These workflows never
request a Z move. A suspect home aborts without an automatic return. Both axes
must use suitable accelerometer endstops.

The console reports acceleration at impact detection and its measured peak in
**g**, plus commanded travel. Charts show X/Y/Z acceleration relative to the
stationary baseline, the threshold and the impact marker when available. They
export full-resolution CSV, metadata JSON and ready-to-open SVG/HTML, with no
plotting tools to install.

Paths appear in the console and `ACCEL_HOMING_STATUS`. The default output is
`accel_homing_charts/` beside the main `printer.cfg`; `charts_directory` changes
it. See the [configuration example](examples/accel_homing.cfg) for all settings.

## Uninstall and restore the previous configuration

Run this single line in the Linux host's **shell**, as the same Klipper user:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/uninstall.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

It restores the original X/Y endstops, sensorless settings and plugin links
from before the first recorded installation, including installations followed
by upgrades. It removes configuration and calibration models introduced by
this extension, preserving unrelated later edits and other saved models.

If a changed setting conflicts with restoration or a backup is incomplete,
it stops before changing configuration or plugin links and identifies the
file and backup to review. Source downloads, charts and backups are retained.
See [uninstall and recovery details](docs/INSTALLATION.md#uninstall-and-restore-the-pre-installation-configuration).
Review the result, then **restart the selected Klipper host process yourself
while idle**, using the service command printed by the uninstaller.
Uninstallation does not restart Klipper or move the printer.

## For the curious

<details>
<summary>Compatibility, limits, saved models and implementation details</summary>

### Sensor support and evidence

| Sensor | Data used | Evidence for this extension |
| --- | --- | --- |
| Beacon | Existing raw accelerometer stream | Original detector retained; offline fixtures |
| ADXL345, LIS2DW, LIS3DH, MPU9250, ICM20948 | Configured Klipper `batch_bulk` or verified older `api_dump` contract | Driver/API inspection and synthetic tests |
| Cartographer | Separately configured ADXL345/LIS2DW object | Standard accelerometer path; probe distance is not acceleration |
| Other compatible object | Explicit `accel_chip` and actual `sensor_range` | Runtime capability checks; no general hardware claim |

`accel_chip: auto` requires exactly one recognized configured sensor. Explicit
selection uses its full section name, preserving suffix case, such as
`adxl345 ToolHead`. There is no dual-sensor routing. One-axis installation is
available with `--axes x` or `--axes y`; TEST/CHARTS still require both axes.

Compatibility depends on APIs, including MCU trigger dispatch and timestamped,
machine-mapped accelerometer data. An older `api_dump` driver alone does not
establish support for every older Klipper release. See the pinned
[source/API evidence](docs/COMPATIBILITY.md) and [offline verification](TESTING.md).
Historical Beacon printer tests do not validate this extension's generic
backends or workflows. Synthetic charts are marked
**SIMULATED / NOT PRINTER MEASUREMENT**.

### Thresholds and saved models

On restart, threshold priority for each axis is: explicit `x_threshold` or
`y_threshold`, explicit legacy shared `threshold`, saved model, then sensor-aware
default. Leave the shared setting absent to use calibrated values. Generic
16 g drivers normally default to 8 g; MPU9250 uses its verified 8 g range and a
4 g default, so **do not use the 8 g example for MPU9250**. Unknown compatible
objects need their actual configured `sensor_range`.

Manual threshold settings and temporary SET commands retain their 3–15 g
range. Calibration and its saved model accept values starting at 2 g; the
calibration ceiling and sensor-range limits still apply.

The named model defaults to `[accel_homing_model default]`. It stores thresholds,
ten-success counts and sensor/configuration metadata. Calibration with
`SAVE_CONFIG=0` stages results until you issue `SAVE_CONFIG` yourself; that
command writes the file and restarts Klipper. A clean calibration has one
qualifying discovery home and ten validation homes: eleven homes in total.
With `SAVE_CONFIG=0`, the completed axis remains homed. Before calibrating the
other axis, use your machine's appropriate controls to leave both XY axes
unhomed and physically center XY by hand again.

### Workflow restrictions and guards

Workflows require idle, unpaused Klipper, Cartesian/CoreXY, independent Z,
controllable XY enables and single-touch X/Y settings (`homing_retract_dist: 0`,
`min_home_dist: 0`). Standard Klipper/Kalico `[safe_z_home]` is supported when
its active handler and underlying stock `G28` are verified. Workflow homes
recheck this path before each trial and call that stock handler for X or Y
only, bypassing `safe_z_home`'s `z_hop`.
Normal `G28`, Z homing and Cartographer probe configuration retain their
existing behavior. Dual carriages, shared XY/Z enables, `homing_override`,
G28 macros and unknown wrapper chains remain rejected. See
[the source contract and limits](docs/COMPATIBILITY.md#safe_z_home-and-xy-only-workflows).

Calibration defaults: start at 2 g, increase by 0.5 g for early triggers, stop
at 12 g or 80% of sensor range, allow at most 40 homing attempts and wait up to
300 seconds for manual recentering. `calibration_start_threshold` is configurable
with a minimum of 2 g. Starting or resuming calibration requires both XY axes
to be unhomed; it never marks manually centered coordinates as homed.
Qualification requires over 10 mm of commanded travel and agreement within
20% of expected center-to-stop travel.
Missing/late data, invalid captures, MCU errors and excessive travel abort
instead of increasing the threshold. Stock Klipper homing fault cleanup still
applies and may release all motors.

### Stationary preparation before each pass

The same check protects ordinary homing and supervised workflows, including
already enabled motors and second touches. It enables the motors needed for
the axis (both drives for CoreXY), waits for queued setup/backoff/retract and
any settling delay after enabling motors, then collects a new sensor reference.
`enable_settle_time` defaults to 0.25 s and accepts finite values from 0 to 2 s;
setting it to zero keeps the quiet-data check.

The reference needs at least 50 fresh contiguous samples spanning
`baseline_time` (0.1 s by default), with peak-to-peak variation on every sensor
axis at most 20% of that pass's impact threshold. Acquisition is limited to
`baseline_time + 0.5 s` after setup completes. Earlier buffered data cannot
qualify. Data faults abort; persistent noise or no data produces `no_baseline`
before the pass starts. The validated reference is frozen before arming;
there is no interval or minimum travel that ignores an early real impact.
See [the preparation contract](docs/COMPATIBILITY.md#pre-motion-stationary-preparation).

This is a software noise check, not proof of contact or a guarantee against
every false home. A driver's hold-to-run current change at the first step can
still cause movement after this check. See Klipper's
[`hold_current` guidance](https://www.klipper3d.org/TMC_Drivers.html#prefer-to-not-specify-a-hold_current).
The preparation check does not change driver settings.

### Generic timing and speed limits

Generic defaults are 5 ms reports, a 20 ms maximum sample age and at least
1000 Hz. The previous report interval is restored afterwards. Speed is
capped at `min(50, max_report_travel / max_sample_age)` mm/s, normally 50 mm/s.
The installer may lower it further for the startup guard; it never raises
acceleration. Beacon retains its existing speed and already-pressed behavior.

A generic home with no impact must establish motion before a calculated
deadline or fail with `no_motion`; this does not establish an origin. Ordinary
`G28` can finish on a fresh impact before motion proof; workflows also check
motion and travel. Plans exceeding a **3 mm nominal commanded-travel budget**
before the host stop request are rejected. This excludes host stalls,
transport and MCU stop delay, so it is **not a physical stopping guarantee**.
See [the exact calculation](docs/COMPATIBILITY.md#generic-startup-motion-policy).

Ten repeats and firmware step distances cannot prove contact with the correct
stop or absence of skipped steps. A softer impact at lower speed may also be
undetectable. Mounting and actual physical behavior need operator validation.

### Legacy names and provenance

Historical `[beacon_accel_homing]`, `beacon: beacon`, `beacon_accel:x/y`,
`BEACON_ACCEL_HOMING_STATUS` and `SET_BEACON_ACCEL_HOMING` remain accepted. Load
only one plugin section; existing Beacon thresholds are retained.

The original base is upstream commit
[`f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4`](https://github.com/Monolith3D/MISC/commit/f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4),
including its `Co-Authored-By: Claude Opus 5.5` provenance. The original project
credited FrankRamm and Ingstronmentalist for printer testing. The FrankRamm
fork adds generic backends, supervised workflows, saved models, charts and
transactional installation for eventual upstream contribution after physical
validation. Full attribution remains in [NOTICE](NOTICE) and [LICENSE](LICENSE).

</details>
