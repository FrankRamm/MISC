# Accelerometer Homing

Use an already configured accelerometer as an X/Y impact endstop in Klipper,
Kalico or a compatible fork. Optional commands calibrate, test and chart it.

**[CloakedWayne](https://github.com/CloakedWayne) created the original Beacon
Accelerometer Homing plugin and its Beacon impact-detection algorithm** in
[Monolith3D/MISC](https://github.com/Monolith3D/MISC/tree/main/Beacon_Accel_Homing).
This experimental [FrankRamm extension](https://github.com/FrankRamm/MISC/tree/codex/universal-accel-homing)
is intended for supervised testing and future upstream contribution after
physical validation. Thanks to **mc-red** for extensive testing.
Full credits: [NOTICE](NOTICE). License: [GPLv3](LICENSE).

Impact homing deliberately contacts a mechanical stop. Supervise initial homes
and calibration with an accessible emergency stop; software checks do not prove
correct physical homing.

## Install or update

With the printer **idle**, run in the Linux host's **shell** as the existing
Klipper service user. Install:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

Update an existing installation:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing --replace-existing-links'
```

The installer detects the checkout, configuration and sensor, backs up changes
and activates X/Y accelerometer endstops. It preserves configured homing speeds,
thresholds and run currents; no speed or travel cap is added by default.
It verifies the selected systemd service and idle state, then **automatically
restarts that service**, requesting sudo if needed. It does not start homing.
Keep the printed source directory: installed links depend on it. For selection,
options and recovery, see [installation details](docs/INSTALLATION.md).

## Setup

Use an already working, firmly mounted sensor that follows each tested axis,
with the correct `axes_map`. Check [sensor and workflow compatibility](docs/COMPATIBILITY.md).
Calibration, TEST, CHARTS and ACCURACY require Cartesian/CoreXY, independent Z and stock
`G28`, directly or beneath the built-in `[safe_z_home]`.

## Calibrate X, then Y

Before **CALIBRATE, TEST, CHARTS or ACCURACY**, keep the printer idle and unpaused.
**Physically center XY by hand and leave both axes unhomed in Klipper.** Use
your machine's XY motor controls while keeping Z supported. Ensure nozzle/bed
clearance for XY travel: these workflows do not perform a Z hop.

Run commands in the **printer console**. **`CENTERED=1` authorizes movement from
that manual center; there is no further confirmation.** Motion starts after
the stationary preparation check.

| Command | Purpose |
| --- | --- |
| `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1` | Calibrate or resume X. |
| `ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` | Calibrate or resume Y after repeating the preparation. |
| `ACCEL_HOMING_TEST CENTERED=1` | Test current X/Y thresholds with the same preparation. |
| `ACCEL_HOMING_CHARTS_GENERATE CENTERED=1` | Run that test and export acceleration charts. |
| `ACCEL_HOMING_ACCURACY AXIS=X SAMPLES=10 CENTERED=1` | Repeat X homes and export travel statistics and measured acceleration charts. |
| `ACCEL_HOMING_STATUS` | Show thresholds, progress, results and chart paths. |
| `ACCEL_HOMING_CANCEL` | Cancel pending calibration and restore temporary settings. |
| `SET_ACCEL_HOMING AXIS=X THRESHOLD=0.5` | Change X temporarily until restart; use `AXIS=Y` for Y. |
| `G28 X` / `G28 Y` | Normal homing after physical validation. |

Calibration starts at **0.5 g by default**. After one qualifying home, it needs
**ten additional consecutive accepted homes** at that candidate threshold.
Their commanded-travel range, **maximum minus minimum, must be strictly less
than 1.5 mm**, independently for each axis; exactly 1.5 mm fails.

If a home triggers too early, the candidate rises by 0.5 g by default and XY
is released and left unhomed. Manually center XY again, then repeat the **same
calibration command** to resume. There is no automatic return after a suspect home.

Success **automatically saves** that axis in `printer.cfg`'s **DO NOT EDIT**
block and restarts Klipper. Leave that block alone. When ready, repeat the
preparation and calibrate Y; each axis is saved independently.

TEST and CHARTS home X, return X alone to center, then home Y. Charts export
CSV, JSON, SVG and HTML; their paths appear in the console and STATUS.
ACCURACY uses the same preparation, excludes one qualifying home per axis,
then measures `SAMPLES` repeats (2–50; default 10). Omit `AXIS` to test X and Y.
It reports commanded-travel variation without tuning or saving thresholds.
Recalibrate after changes to mounting, belts, sensor orientation, speed or current.

## Set thresholds manually

Edit `x_threshold` or `y_threshold` in your existing `[accel_homing]` section
(`accel_homing.cfg` for new installations), then issue `RESTART` while idle.

```ini
x_threshold: 0.5
y_threshold: 0.5
```

Values are in **g** and must be finite and positive. Higher means a stronger
impact is required; sensor limits are explained below.
Explicit axis settings override a legacy shared `threshold`, then saved
calibration, then sensor defaults. Comment out manual settings to use calibration;
keep only one plugin section. See the [configuration example](examples/accel_homing.cfg).

## Uninstall

Run in the host's **shell** as the same Klipper user:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/uninstall.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

It restores pre-installation settings while preserving unrelated later edits.
Review the result, then **manually restart the selected Klipper host process
while idle**, using the printed service command. See [recovery details](docs/INSTALLATION.md#uninstall-and-restore-the-pre-installation-configuration).

## For the curious

<details>
<summary>Installation, activation and restoration details</summary>

The Linux host needs Python 3.8+, `bash` and `curl`. Configure sensor firmware
and wiring first. Multiple instances or sensors require explicit selection.
Installation disables conflicting sensorless settings and preserves existing
saved configuration. An incompatible generic speed/timing plan is reported
before writes rather than silently lowering the speed. Existing explicit
travel limits remain active; optional
[`--max-report-travel` and `--max-startup-travel`](docs/INSTALLATION.md#explicit-generic-travel-budgets)
select those limits during installation or an update.

`--replace-existing-links` backs up and repoints previous source links. Keep
source directories under `~/.local/share/accel-homing/` and transaction backups
for rollback. [Installation details](docs/INSTALLATION.md) cover manual
selection, dry runs, verified downloads, pinning and backups.

Installation and updates verify the selected systemd service and recheck that
its instance is idle before restarting it. Append `--no-restart` to the final
`bash "$t"` invocation to defer activation, then restart the selected host
process while idle using the printed command. Printer-console `RESTART` or
`FIRMWARE_RESTART` alone cannot reload already imported Python modules. See
[restart requirements](docs/INSTALLATION.md#automatic-host-service-restart)
for service detection, privilege checks and activation failures.

Uninstallation restores original X/Y endstops, sensorless settings and plugin
links from before the first recorded installation, including later upgrades.
It removes configuration and calibration models introduced by this extension,
preserving unrelated later edits and other saved models. Conflicting settings
or incomplete backups stop restoration before configuration or link changes;
the uninstaller identifies the file and backup to review. Downloads, charts
and backups are retained. Uninstallation neither restarts Klipper nor moves
the printer; review its output and restart the host process yourself while idle.

</details>

<details>
<summary>Sensor support and evidence</summary>

A toolhead sensor cannot measure moving-bed Y impacts; the mounted sensor
must move with each tested axis.

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

</details>

<details>
<summary>Thresholds and saved models</summary>

On restart, threshold priority for each axis is: explicit `x_threshold` or
`y_threshold`, explicit legacy shared `threshold`, saved model, then sensor-aware
default. Leave the shared setting absent to use calibrated values. Generic
16 g drivers normally default to 8 g; MPU9250 uses its verified 8 g range and a
4 g default, so **do not use the 8 g example for MPU9250**. Unknown compatible
objects need their actual configured `sensor_range`.

Manual threshold settings and temporary SET commands accept any finite positive
value in g. When the sensor range is known, values are bounded to 80% of that
configured or detected full-scale range; native Beacon retains its fallback
when clip range is unknown. There is no fixed 3 g minimum or 15 g maximum for
manual settings. The actual session range is checked again before each pass:
another Beacon client using a narrower scale can cause a previously accepted
value to be rejected. Calibration and saved models retain their 0.5–15 g range,
subject to the calibration ceiling and sensor-range limits.

The named model defaults to `[accel_homing_model default]`. It stores thresholds,
ten-success counts, the validation travel range and fixed 1.5 mm limit, and
sensor/configuration metadata. Existing saved models remain loadable; updating
does not certify their historical travel range under the new calibration rule.
Run a new calibration to obtain that qualification. Calibration defaults to
`SAVE_CONFIG=1`, which writes the file and restarts Klipper automatically.
`SAVE_CONFIG=0` stages results in memory until you issue `SAVE_CONFIG` yourself;
it neither writes the file nor activates the new threshold for ordinary
homing. The workflow restores the previous active thresholds when it ends.
The staged model supplies its threshold after saving and reloading, unless a
manual override takes priority. An ordinary `RESTART` discards unsaved staged
values. A clean calibration has one
qualifying discovery home and ten validation homes: eleven homes in total.
With `SAVE_CONFIG=0`, the completed axis remains homed. Before calibrating the
other axis, use your machine's appropriate controls to leave both XY axes
unhomed and physically center XY by hand again.

To defer saving, start with
`ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1 SAVE_CONFIG=0`.
When resuming after recentering, keep the same command, `AXIS` and optional
`SAVE_CONFIG` value. The console reports when recentering is needed; a pending
calibration expires after its configured timeout.

</details>

<details>
<summary>Workflow restrictions and calibration qualification</summary>

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

Calibration defaults: start at 0.5 g, increase by 0.5 g for early triggers, stop
at 12 g or 80% of sensor range, allow at most 40 homing attempts and wait up to
300 seconds for manual recentering. `calibration_start_threshold` is configurable
with a minimum of 0.5 g. Existing models at 1 g and 2 g remain valid. Starting
at 0.5 g applies when `calibration_start_threshold` is absent or set to 0.5;
an explicit existing value is preserved. Updating does not replace a saved
model's threshold; recalibrate to produce a new model. Starting or resuming
calibration requires both XY axes to be unhomed; it never marks manually
centered coordinates as homed.
Qualification requires over 10 mm of commanded travel and agreement within
20% of expected center-to-stop travel by default. After a separate qualifying
discovery, ten consecutive geometry-qualified validation homes at the same
candidate must also have `max(travel_mm) - min(travel_mm) < 1.5 mm`. This fixed
limit has no configuration override. Each axis has its own validation window.
The existing early-hit classification (pressed start, at most 10 mm, or travel
below the geometry tolerance) still raises the candidate and clears that window;
the new candidate must qualify again. A range failure among geometry-qualified
validation homes aborts without a higher candidate, another automatic move,
model staging, writing or restart. Status and console progress report the range
and limit. Commanded distances do not establish physical repeatability.
Missing/late data, invalid captures, MCU errors and excessive travel abort
instead of increasing the threshold. Stock Klipper homing fault cleanup still
applies and may release all motors.

</details>

<details>
<summary>Stationary preparation and diagnostics</summary>

`no fresh quiet accelerometer baseline before the move` means the pass could
not obtain or retain the required stationary reference before arming.
After the error, run `ACCEL_HOMING_STATUS`. It retains the baseline result and
phase, quiet-window sample count and duration, X/Y/Z peak-to-peak variation and
quiet limit, plus sample/batch ages and the allowed sample age. Fresh samples
can repeatedly reset the quiet window before it spans `baseline_time`.
Review duration, reset count, variation and sample ages together; the error
alone does not prove excessive sensor noise. Include the status output and
matching Klipper log when reporting a failure.

The same check protects ordinary homing and supervised workflows, including
already enabled motors and second touches. It enables the motors needed for
the axis (both drives for CoreXY), waits for queued setup/backoff/retract and
any settling delay after enabling motors, then collects a new sensor reference.
`enable_settle_time` defaults to 0.25 s and accepts finite values from 0 to 2 s;
setting it to zero keeps the quiet-data check.

The reference needs at least 50 fresh contiguous samples spanning
`baseline_time` (0.1 s by default). Native Beacon permits peak-to-peak
variation on every sensor axis up to
`max(0.20 * threshold, min(0.5 g, threshold))`. The 0.5 g floor allows a 0.5 g
calibration candidate to acquire its reference; lower manual thresholds also
cap that floor. It is not a configuration option and does not raise the
impact threshold.
Configured generic accelerometers, including Cartographer's separate ADXL/LIS
sensor, use `max(0.20 * threshold, baseline_noise_floor)`.
`baseline_noise_floor` defaults to **0.2 g** and accepts finite values from
**0 to 0.2 g**; zero restores the previous 20% rule. With a 0.5 g calibration
candidate, the default quiet limit is 0.2 g while the impact threshold remains
0.5 g. This setting does not affect native Beacon.

When a native Beacon reference exceeds the original 20% limit, movement
proof discounts the observed stationary-noise envelope. Already-pressed
detection uses the current upper velocity bound; ambiguous evidence fails
with `no_motion` by a calculated startup deadline. It does not count as a
successful pressed start. References within the old limit retain the legacy
path. Neither path changes raw impact detection or introduces a blind
interval. These policies have synthetic validation only; they do not prove
physical contact or stopping distance. See
[native Beacon motion checks](docs/COMPATIBILITY.md#native-beacon-with-an-expanded-reference).

Acquisition is limited to `baseline_time + 0.5 s` after setup completes.
Earlier buffered data cannot qualify. Data faults abort; persistent noise or
no data produces `no_baseline`
before the pass starts. The validated reference is frozen before arming;
there is no interval or minimum travel that ignores an early real impact.
See [the preparation contract](docs/COMPATIBILITY.md#pre-motion-stationary-preparation).

This is a software noise check, not proof of contact or a guarantee against
every false home. A driver's hold-to-run current change at the first step can
still cause movement after this check. See Klipper's
[`hold_current` guidance](https://www.klipper3d.org/TMC_Drivers.html#prefer-to-not-specify-a-hold_current).
The preparation check does not change driver settings.

</details>

<details>
<summary>Generic timing, speed and travel limits</summary>

Generic defaults are 5 ms reports, a 20 ms maximum sample age and at least
1000 Hz. The previous report interval is restored afterwards. Sample freshness,
stream faults and startup motion detection still apply at every speed.
There is no default 50 mm/s cap or startup-distance admission limit. Default
installation preserves any finite positive configured `homing_speed`, including
75 or 100 mm/s, without adding distance caps. Only an explicit installer
`--homing-speed N` requests a speed edit. Normal toolhead velocity and
acceleration limits still apply; acceleration is never raised automatically.
Native Beacon keeps configured speeds; its expanded-reference checks are
described above.

`max_report_travel` and `max_startup_travel` are independent optional limits,
disabled when absent. An existing explicit value stays active. The installer
flags with the same names opt into those limits, and are refused for native
Beacon. A configured reporting limit requires effective speed at most
`max_report_travel / max_sample_age`; a configured startup limit checks nominal
travel before a host stop request. Explicit values accept finite ranges
0.1..2 mm and 0.1..5 mm respectively. An incompatible explicit limit is refused
before installation writes, without lowering speed or clearing that limit.
An update also preserves a 50 mm/s value written by an older installer; it
does not infer or restore a historical speed from backups. Those backups
retain the original configuration for review.
Runtime checks use the current toolhead velocity and acceleration limits,
including temporary changes left by a macro or tuning routine. A nonzero
`homing_accel` is only an upper limit: it cannot raise a lower live acceleration.
CALIBRATE/TEST/CHARTS check their requested generic X/Y plans before changing
workflow state or moving; homing preparation checks them again. Ordinary
`G28 X/Y` still uses the configured public handler, so `safe_z_home` can perform
its Z hop before a later X/Y policy refusal. No Z settings are changed.

A generic home with no impact must establish motion before a calculated
deadline or fail with `no_motion`; this does not establish an origin. Ordinary
`G28` can finish on a fresh impact before motion proof; workflows also check
motion and travel.

When an accepted generic reference exceeds the original 20% quiet limit,
movement proof subtracts an allowance for its observed noise. Each sensor
axis uses the larger deviation from the accepted mean to either observed
extreme; status reports the applied `motion-proof noise bound XYZ` in g.
This assumes later stationary noise stays within those observed bounds.
Automatic impact direction uses the same bounds; uncertain drift leaves
detection bidirectional. Raw impact acceleration and threshold, manual direction
overrides, first-impact eligibility and the startup deadline stay unchanged.
Very noisy or slow cases can fail with
`no_motion`; that failure does not raise speed or thresholds automatically.
This adjustment has synthetic validation only and still needs supervised
physical testing.

The startup deadline remains calculated from effective speed, acceleration,
sample age and reporting margin. Higher speeds or lower acceleration can
increase the nominal travel before the host requests a stop for absent motion
proof. An optional `max_startup_travel` can reject such a plan; no distance cap
is inserted automatically. Installation does not change sample age, reporting
interval, acceleration or current to admit a requested speed.

The **1.5 mm calibration rule is a separate repeatability test**:
`max(travel_mm) - min(travel_mm) < 1.5 mm` across ten accepted validation homes.
It is not a per-home travel cap or a physical stopping-distance guarantee.
Neither the startup deadline nor optional nominal travel limits include
unbounded host stalls, transport/MCU stop delay or the machine's mechanical
response. A configured speed still needs supervised physical validation.
See [the exact calculation](docs/COMPATIBILITY.md#generic-startup-motion-policy).

Commanded/firmware step distances are not encoder measurements. Ten repeats
cannot prove contact with the correct stop or absence of skipped steps.
Starting calibration at 0.5 g does not establish that either problem is fixed.
A softer impact at lower speed may also be undetectable. Mounting and actual
physical behavior need operator validation.

</details>

<details>
<summary>Test results, accuracy, charts and temporary thresholds</summary>

TEST and CHARTS need the same existing manual XY center, both axes unhomed
and nozzle/bed clearance as calibration. `CENTERED=1` declares that preparation
complete; omitting it fails before movement. They start without a preliminary
XY release or a separate confirmation and never request a Z move. Both axes
must use suitable accelerometer endstops. A suspect home aborts without an
automatic return.

`ACCEL_HOMING_ACCURACY AXIS=X SAMPLES=10 CENTERED=1` uses the same preparation.
`AXIS=X` or `AXIS=Y` tests that installed axis; omitting `AXIS` tests X then Y.
It first qualifies a home, excludes it from statistics, returns that trusted
axis to center and records the requested number of additional homes. Each
accepted home is followed by a return of that axis to center only when another
home or axis is needed. Thresholds, homing speed and current remain unchanged;
the command neither stages a model nor saves configuration or restarts Klipper.
A suspect home aborts without another automatic move. Use the accessible
emergency stop if active movement must be interrupted; `ACCEL_HOMING_CANCEL`
is not an immediate emergency stop for a running console command.

The accuracy report includes each home's commanded travel, measured onset and
peak in g, actual configured trigger threshold, and min/max/mean/range, population
standard deviation (mm) and population variance (mm²). Range is maximum minus
minimum; variance divides the squared deviations from the mean by N. A partial
run is explicitly incomplete; fewer than two measured homes have no reported
standard deviation or variance. The qualifying home is always excluded.
Travel is the firmware's commanded step distance, recorded to **0.001 mm**,
not an encoder measurement of physical stop position. These statistics describe
variation across those repeated commands; they do not prove absolute accuracy,
absence of skipped steps, or calibration qualification for any sample count.
The HTML opens with a travel chart and statistics, then lets you select each
home's embedded X/Y/Z acceleration trace. CSV and JSON retain the individual
values and references to the complete per-home captures.

The console reports acceleration at impact detection, its measured peak in g
and commanded travel. Charts show X/Y/Z acceleration relative to the stationary
baseline, configured trigger threshold and detected onset marker when available.
That threshold is the value used by the detector, not a newly calibrated minimum
g requirement. The measured onset and peak can differ from it. Outputs include
full-resolution CSV, metadata JSON and ready-to-open SVG/HTML without plotting
tools to install. The default `accel_homing_charts/` directory is beside the
main `printer.cfg`; `charts_directory` changes it. See the
[configuration example](examples/accel_homing.cfg) for all settings.

`SET_ACCEL_HOMING AXIS=X THRESHOLD=0.5` changes X temporarily until restart;
use `AXIS=Y` for Y. It does not save a calibration model. For manual configuration,
keep your existing sensor name and plugin section; do not add a second one.
Example for an **ADXL345**:

```ini
[accel_homing]
accel_chip: adxl345 toolhead
#x_threshold: 8.0
#y_threshold: 8.0
```

Leave thresholds commented to use saved calibration. Uncomment an axis to
override it, choose a sensor-appropriate value and issue `RESTART` while idle.
That console command is sufficient for configuration-only changes.

</details>

<details>
<summary>Legacy names and provenance</summary>

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
