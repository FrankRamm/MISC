# Detailed reference

[Back to the quick start](../README.md).

This reference covers advanced settings, workflow behavior and source evidence.
Installation and Moonraker registration are automatic; the configuration below
documents the entry written by the installer.

## Installation, activation and restoration details

The Linux host needs Python 3.8+, `bash`, `curl` and Git for native updates. Configure sensor firmware
and wiring first. Multiple instances or sensors require explicit selection.
Installation disables conflicting sensorless settings and preserves existing
saved configuration. An incompatible generic speed/timing plan is reported
before writes rather than silently lowering the speed. Existing explicit
travel limits remain active; optional
[`--max-report-travel` and `--max-startup-travel`](INSTALLATION.md#explicit-generic-travel-budgets)
select those limits during installation or an update.

The short command buffers a successful complete response before Bash starts.
Its final-marker guard rejects incomplete executable payloads; the bootstrap
then verifies every package file against one immutable Git commit. Defaults
already select this fork/branch; no `--repo`/`--ref` arguments are needed.
Append expert options after `bash -c "$installer" --`, for example
`--dry-run`, `--no-restart` or `--no-update-manager`.

Installed modules link directly into a dedicated clean Git checkout under
`~/.local/share/accel-homing/managed/`, so Moonraker's normal **Update** changes
the code actually loaded by Klipper. It tracks `codex/universal-accel-homing`
with `channel: dev`; current Mainsail/Fluidd support this without repository
tags. Their usual update confirmation may still appear. Moonraker pulls code
and restarts its associated Klipper service; it does not rerun `install.sh`.
The private PNG renderer dependencies survive normal updates.

### Moonraker configuration

For new registrations, the installer writes the entry below directly in the
matched `moonraker.conf`. The ellipses abbreviate the automatically selected
absolute path; the actual configuration contains the complete value printed as
**Managed update source**. The directory identifier is computed for the selected
installation. Use the [README block](../README.md#moonraker-updates) for manual
setup, with that complete path rather than the abbreviated example.

```ini
[update_manager accel_homing]
type: git_repo
channel: dev
path: …/.local/share/accel-homing/managed/…/repo
origin: https://github.com/FrankRamm/MISC.git
primary_branch: codex/universal-accel-homing
is_system_service: False
managed_services: klipper
```

It ensures the base `[update_manager]` section exists and avoids duplicate
registration. Existing installations using `accel_homing_update.cfg` keep that
file and include without migration. A matching entry added manually to the main
configuration is reused and preserved on uninstall; conflicting or duplicate
entries stop installation. Native updates use the stable checkout linked by the
installed modules, not the immutable download cache.
See [updater details](INSTALLATION.md#native-moonraker-updater).

Known installer-owned snapshot links migrate using transaction history.
Foreign links still require explicit review and `--replace-existing-links`;
custom `gcode_macro G28` entries keep their separate acknowledgement requirement.
Keep source directories and backups for rollback. Moonraker **Hard Recovery**
may replace the checkout and discard ignored plotting dependencies: rerun the
short installer afterward. Use it again when a release requires installer,
configuration or dependency changes. [Installation details](INSTALLATION.md)
cover detection, verified downloads, pinning and recovery.

Installation verifies the selected Klipper/Moonraker relationship and systemd
services, rechecks that the printer is idle and activates the updater without
manual configuration copying. `--no-restart` defers both required restarts;
use the printed commands while idle. `--no-update-manager` selects legacy
installation for new/unmanaged hosts without a supported Moonraker setup;
an already registered managed updater refuses that opt-out.
Printer-console `RESTART` or
`FIRMWARE_RESTART` alone cannot reload already imported Python modules. See
[restart requirements](INSTALLATION.md#automatic-host-service-restart)
for service detection, privilege checks and activation failures.

Uninstallation restores original X/Y endstops, sensorless settings and plugin
links from before the first recorded installation, including later upgrades.
It removes configuration and calibration models introduced by this extension,
preserving unrelated later edits and other saved models. Conflicting settings
or incomplete backups stop restoration before configuration or link changes;
the uninstaller identifies the file and backup to review. Downloads, charts
and backups are retained. Uninstall and rollback leave activation to the
operator. Restoring/removing owned updater configuration also requires the
matching Moonraker restart, in addition to Klipper, so its loaded update entry
matches disk. Review the output and restart the listed services while idle.


## Sensor support and evidence

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
available with `--axes x` or `--axes y`. TEST requires both axes; CHARTS can
select the installed axis with `AXIS=X` or `AXIS=Y`.

Compatibility depends on APIs, including MCU trigger dispatch and timestamped,
machine-mapped accelerometer data. An older `api_dump` driver alone does not
establish support for every older Klipper release. See the pinned
[source/API evidence](COMPATIBILITY.md) and [offline verification](../TESTING.md).
Historical Beacon printer tests do not validate this extension's generic
backends or workflows. Synthetic charts are marked
**SIMULATED / NOT PRINTER MEASUREMENT**.


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
keep only one plugin section. See the [configuration example](../examples/accel_homing.cfg).

## Thresholds and saved models

On restart, threshold priority for each axis is: explicit `x_threshold` or
`y_threshold`, explicit legacy shared `threshold`, saved model, then sensor-aware
default. Leave the shared setting absent to use calibrated values. Generic
16 g drivers normally default to 8 g; MPU9250 uses its verified 8 g range and a
4 g default, so **do not use the 8 g example for MPU9250**. Unknown compatible
objects need their actual configured `sensor_range`.

Manual threshold settings and temporary SET commands accept any finite positive
value in g. When the sensor range is known, values are bounded to 80% of that
configured or detected full-scale range. This is a conservative software
headroom policy, separate from physical saturation and sensor-fault checks;
native Beacon retains its fallback when clip range is unknown. There is no
fixed 3 g minimum or 15 g maximum for
manual settings. The actual session range is checked again before each pass:
another Beacon client using a narrower scale can cause a previously accepted
value to be rejected. Calibration and saved models require finite thresholds
of at least 0.5 g, with the same known-range headroom policy and no fixed 15 g
maximum. There is no configurable calibration g ceiling. The legacy
`calibration_max_threshold` setting is accepted but ignored with a deprecation
warning; it no longer affects noise selection, retries or model loading.
Before arming, the derived raw trigger limit must be finite and positive, as
must its squared value for XYZ-magnitude detection. This is numeric
representation validation: finite inputs whose derived square overflows to
infinity or underflows to zero are rejected. It is separate from the
known-range headroom policy.

The named model defaults to `[accel_homing_model default]`. It stores thresholds,
ten-success counts, the validation travel range, the fixed 2.0 mm calibration
limit, the XYZ-magnitude detector metric, and
sensor/configuration metadata. Existing saved models remain loadable; updating
does not certify their historical travel range under the new calibration rule.
Run a new calibration to obtain that qualification. Calibration defaults to
`SAVE_CONFIG=1`, which writes the file and restarts Klipper automatically.
`SAVE_CONFIG=0` stages results in memory until you issue `SAVE_CONFIG` yourself;
it neither writes the file nor activates the new threshold for ordinary
homing. The workflow restores the previous active thresholds when it ends.
The staged model supplies its threshold after saving and reloading, unless a
manual override takes priority. An ordinary `RESTART` discards unsaved staged
values. A clean calibration has one plausible discovery impact beyond 20 mm
and ten validation homes: eleven monitored moves in total.
With `SAVE_CONFIG=0`, the completed axis remains homed. Before calibrating the
other axis, use your machine's appropriate controls to leave both XY axes
unhomed and physically center XY by hand again.

To defer saving, start with
`ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1 SAVE_CONFIG=0`.
Early-impact retries run automatically within the same calibration command.
See the [commanded return and retry sequence](SIMPLE_CALIBRATION.md#automatic-early-impact-returns).


## Stationary vibration measurement and final retraction

`ACCEL_HOMING_CALIBRATE_NOISE` measures stationary accelerometer vibration in
g, not sound. It requires an idle, unpaused printer with an empty motion queue
and no other homing, workflow or accelerometer client. It leaves the current
motor/fan state as it is; neither manual centering nor `CENTERED=1` is required.
It does not home, move, enable/release motors, save a model or restart Klipper.
`SAVE_CONFIG` is not accepted by this command.

Each new per-axis `ACCEL_HOMING_CALIBRATE` invocation also acquires these three
windows before its first home. It uses a fresh measurement rather than a prior
standalone result and does not repeat it for candidate retries. Acquisition,
context or summary-export failure aborts before calibration movement.

Each of three separate one-second windows subtracts its own XYZ mean. One PNG
contains all three signed XYZ plots on the same vertical scale, with absolute
peaks, RMS and the separate starting candidates. Exact metrics, sensor context,
duration and count remain in STATUS; intermediate windows create no files.
`ACCEL_HOMING_STATUS` shows results, paths, `noise_recommendation` and the
remaining `noise_seed_axes`.

Configured batch sensors discard samples at or before the active noise
window's cutoff before checking that window's freshness and continuity.
Delivery between windows is drained without carrying pending samples into the
next window. Samples belonging to an active window must still pass the full
age, future-timestamp, continuity and sensor-error checks; the requested
reporting interval is not a delivery-time guarantee.

If timing fails, STATUS reports `sample_timing_fault` independently of the
last homing baseline. It retains the first failure's phase, window number,
`too_old` or `future` reason, cutoff and sample timestamps, signed sample/batch
ages and allowed limits. An aborted noise result also retains this object as
`timing_fault`, including after stream cleanup. A new stream clears the live
fault summary. An older homing baseline's ages do not describe a later noise
measurement failure.

For each X/Y component in the standalone diagnostic, the largest absolute peak
from all three windows receives the greater of a **20% or 0.1 g** margin,
then rounds upward on the configured
`calibration_start_threshold + n * calibration_step` grid. For example, a
2 g X peak gives a 2.5 g initial candidate with the default 1 g start and
0.25 g step.
This is an **initial candidate, NOT a calibrated impact threshold**. A candidate
above the known sensor range's 80% headroom limit is unavailable; it is never
reduced to fit. An unknown sensor range also leaves candidates unavailable. Complete
measurements and charts remain available in either case. An incomplete or
faulted run has no recommendation.

These component hints remain diagnostic. Automatic calibration instead uses
its own fresh **XYZ-magnitude** peak, matching its detector, to choose the
initial candidate on the configured start/step grid. An explicit `STEP` changes
the grid spacing and still allows this selection. `START_THRESHOLD` overrides
the initial value but does not skip measurement. Without that override, an
unknown range or an over-ceiling candidate aborts before movement rather than
falling back or clamping. Existing manual thresholds, saved models and ordinary
homing thresholds are unchanged by measurement. See
[the current calibration algorithm](SIMPLE_CALIBRATION.md) and
[numerical limits](COMPATIBILITY.md#stationary-accelerometer-noise-measurement).
Stationary vibration does not measure motor-start or travel transients and
does not establish a certified minimum impact threshold.

Calibration status/result `stationary_noise` identifies the fresh run, three
iterations, `impact_metric: xyz_magnitude`, `noise_magnitude_peak_g`, selected
`start_threshold_g`, `summary_paths` and `initial_threshold_source`
(`stationary_noise` or `explicit_start_threshold`). Saved model metadata keeps
`stationary_noise_measurement`; `stationary_noise_seed` is present only when
noise chose the initial value. An aborted noise-stage result identifies
`purpose: calibration_preflight` and `calibration_axis`.

`homing_retract_dist: 0` keeps one touch per home. The separate plugin setting
`back_off_dist` defaults to 5 mm (0 disables; finite 0–20 mm) and controls the
existing cross-axis release plus the final release after a successful ordinary
home. Final release runs after firmware homing-coordinate corrections, only
on axes completed by that invocation. It moves away from the configured stop
at the rail's retract speed (homing speed if absent), clips to the rail range
and does not repeat a release the firmware already made. The existing
`homing_accel` cap also applies to this move, preserving any lower live
acceleration and restoring the normal acceleration afterward. Z is not included.
Supervised workflows suppress this ordinary release on individual trials:
accepted intermediate homes still return only the tested axis to center;
after the whole workflow passes its checks and required per-home captures, its
final tested axis releases before model staging/saving or successful completion.
ACCURACY writes its aggregate summary after this trusted release. A later
summary-export failure reports an aborted run and retains any published final summary;
it does not reverse the preceding release.
A failed, cancelled or suspect workflow does not gain a final release move.
Homing speeds, driver currents and the meaning of commanded-travel statistics
are unchanged; a completed release is not proof of physical stop accuracy.


## Workflow restrictions and calibration qualification

Workflows require idle, unpaused Klipper, Cartesian/CoreXY, independent Z,
controllable XY enables and single-touch X/Y settings (`homing_retract_dist: 0`,
`min_home_dist: 0`). Standard Klipper/Kalico `[safe_z_home]` or `[homing_override]`
is supported when its exact built-in handler, stock predecessor and matching
G28 registrations are verified; an override script must not be active.
The two built-in sections cannot coexist. Workflow homes recheck this path
before each trial and call stock `G28` for X or Y only. They skip safe Z hops,
all override G-code and forced `set_position_*` setup, including current/enable
hooks, `SYNC_MOTORS` and Z/Beacon actions. Prepare suitable XY test
currents/enables and nozzle clearance independently.
Normal `G28` retains its configured wrapper and complete macro sequence.
Each successful underlying
XY home releases as described above; later safe Z/wrapper positioning may still
move XY. Z homing and Cartographer probe configuration
retain their existing behavior. Dual carriages, shared XY/Z enables,
G28 macros and nested/unknown wrapper chains remain rejected. See
[the source contract and limits](COMPATIBILITY.md#safe_z_home-and-xy-only-workflows).

Calibration uses the [impact-after-20 mm discovery and ten-check cycle](SIMPLE_CALIBRATION.md).
The default command measures noise before selecting its initial candidate:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
```

The default grid starts at 1 g with 0.25 g steps. Existing configured start/step
values remain in use. To force a 1 g initial candidate and 0.25 g increments,
use `START_THRESHOLD=1 STEP=0.25`; the noise measurement still runs first.

X or Y selects movement; all three sensor channels contribute to the
baseline-subtracted magnitude. Discovery continues until an impact; reaching
20 mm alone never selects a candidate. To qualify, an impact must be strictly
beyond 20 mm and within the inclusive configured midpoint-to-endstop band set
by `calibration_travel_tolerance` (default 20%). This applies to discovery and
every validation impact. A plausible discovery selects the candidate, then
only that axis returns to its configured midpoint. Only the following ten
centered impacts count toward repeatability. The running max-minus-min spread
is checked after every accepted impact: >2 mm immediately increases by STEP,
clears the window, recenters that axis and restarts discovery. Passing still
requires ten fresh checks with spread <=2 mm.

An impact at or before 20 mm, or strictly below the lower expected-distance
limit, is premature. Reverse the selected axis's recorded signed start-to-halt
displacement, including post-trigger travel. After the queued return and
settling complete, advance one STEP and restart discovery;
START_THRESHOLD/STEP cannot change mid-run. A 2 mm commanded displacement to the
halt produces a 2 mm reverse command. There is no accumulated-distance budget,
overshoot/nominal-clearance gate or additional physical-arrival check. Basic
current-record and finite-number checks remain, and ordinary firmware movement
errors propagate. See the [return sequence and completed-return telemetry](SIMPLE_CALIBRATION.md#automatic-early-impact-returns).

The 40-attempt default budget still includes discovery and validation across
all retries. Before another candidate, at least 11 attempts must remain for a
fresh discovery and ten checks; this is checked before any recovery return,
candidate increase or recentering. Threshold increases keep the selected STEP
and the known-range 80% headroom policy. Sensor
faults, a move with no impact, a missing current return record, a failed return
and attempt-budget exhaustion abort without a threshold increase. An impact
beyond 20 mm and above the upper expected-distance limit also aborts without a return,
threshold increase or model write. Only the suspect axis's homed state is
invalidated; the operator must physically center XY and prepare a new command.
These checks guard commanded-travel plausibility, not physical contact.

The initial physical center is confirmed by the operator while XY is unhomed.
Discovery and validation both use verified stock axis homing. After a qualifying
impact, the selected axis is returned to `(position_min + position_max) / 2`.
CALIBRATE and ACCURACY use the same helper for midpoint returns with ordinary
acceleration restored after homing and an explicit `calibration_settle_time`
wait (default 0.25 s) after the return. Early reversals keep their existing
homing acceleration cap and settling wait. Homing speed and its cap are unchanged.
Other axes are not commanded to move. Midpoint centering assumes the
accepted impact was the endstop; premature returns estimate the previous
starting point from commanded steps. Neither provides encoder feedback or
automatically diagnoses binding. Stock G28 fatal
errors may disable all motors, so keep Z supported. New saved models preserve
the same vector detector for ordinary homing; old models/manual overrides
retain the legacy selected-axis path.

## Stationary preparation and diagnostics

Simple calibration and axes using a saved XYZ-magnitude model acquire at least
50 fresh contiguous offset samples over `baseline_time` (100 ms by default),
but do not enforce quietness or use the noise envelope to prove movement.
Calibration's three initial noise windows are separate from this per-move offset.
Missing, stale, future, irregular, invalid or saturated reference data still
aborts. The acceleration threshold is active immediately after arming.
`ACCEL_HOMING_STATUS` labels this policy explicitly.

The quiet-reference and motion-bound descriptions below apply only to the
**legacy selected-axis detector** (old models/manual overrides). The new vector
path does not produce a startup `no_motion` failure. Both paths keep fresh
offset and sensor-delivery checks.

`no fresh quiet accelerometer baseline before the move` means an ordinary pass
could not obtain or retain its required stationary reference before arming.
After the error, run `ACCEL_HOMING_STATUS`. It retains the baseline result and
phase, quiet-window sample count and duration, X/Y/Z peak-to-peak variation and
quiet limit, plus sample/batch ages and the allowed sample age. Fresh samples
can repeatedly shorten the quiet window before it spans `baseline_time`.
The retained `quiet_resets` counter counts range violations that require
discarding an old prefix; it no longer means the whole candidate was cleared.
The retained `impact_threshold_g` identifies the threshold used for that
reference. A failed calibration restores the ordinary homing thresholds, so
the current X/Y thresholds can differ from the trial recorded in the baseline
summary. A calibration summary retains the configured quiet limit as context
but marks it as unenforced; cleanup can subsequently restore an ordinary
threshold of 8 g without changing that diagnostic.
Review duration, reset count, variation and sample ages together; the error
alone does not prove excessive sensor noise. Include the status output and
matching Klipper log when reporting a failure.

The same data preparation precedes ordinary homing and supervised workflows,
including already enabled motors and second touches. It enables the motors needed for
the axis (both drives for CoreXY), waits for queued setup/backoff/retract and
any settling delay after enabling motors, then collects a new sensor reference.
`enable_settle_time` defaults to 0.25 s and accepts finite values from 0 to 2 s;
setting it to zero keeps the quiet-data check.

The reference needs at least 50 fresh contiguous samples spanning
`baseline_time` (0.1 s by default). When quietness is required, its rolling
collector retains useful recent samples when an older extreme makes the
accumulated range too wide. It removes
only the oldest prefix needed to satisfy the range limit, then keeps enough
history to span the full duration. Every retained sample participates in the
XYZ extrema, mean and motion-proof envelope; no in-window outlier is filtered.
Readiness is checked again immediately before arming. The hard 65,536-sample
capacity fails with `baseline_overflow` if exceeded.

Native Beacon permits peak-to-peak
variation on every sensor axis up to
`max(0.20 * threshold, min(0.5 g, threshold))` outside calibration. The 0.5 g
floor permits that much variation at a 0.5 g homing threshold; lower manual
thresholds also cap that floor. It is not a configuration option and does not raise the
impact threshold.
Configured generic accelerometers, including Cartographer's separate ADXL/LIS
sensor, use `max(0.20 * threshold, baseline_noise_floor)`.
`baseline_noise_floor` defaults to **0.7 g** and accepts finite values from
**0 to 0.7 g**; zero restores the previous 20% rule. With a 0.5 g or 1 g
homing threshold, the default quiet limit is 0.7 g while the actual
impact threshold remains selected. Calibration does not enforce this limit.
Existing explicit floor settings remain active; set `baseline_noise_floor: 0.7` to use the wider
allowance when an older value is configured. This setting does not affect
native Beacon.

For generic calibration references and other references admitted above the
original 20% limit, movement proof still discounts each axis's maximum observed deviation from the accepted
mean. A wider baseline can therefore qualify for preparation while later
movement remains unproven and aborts with `no_motion`. Raw impact detection
still compares against the actual trial threshold from the first eligible
sample; the wider quiet allowance does not suppress those triggers.

For native Beacon calibration, or when another reference exceeds the original
20% limit, movement proof discounts the observed stationary-noise envelope. An additional bounded
sample interval around the first commanded step in recorded history excludes
earlier queued stationary samples from that interval. It must still prove
enough motion before the original startup deadline; raw impacts remain
eligible throughout. Already-pressed
detection uses the current upper velocity bound; ambiguous evidence fails
with `no_motion` by a calculated startup deadline. It does not count as a
successful pressed start. Non-calibration references within the old limit
retain the legacy path. Neither path changes raw impact detection or introduces a blind
interval. These policies have synthetic validation only; they do not prove
physical contact or stopping distance. See
[native Beacon motion checks](COMPATIBILITY.md#native-beacon-with-an-expanded-reference).

After a failure, STATUS retains a short motion-proof summary under the
affected axis's last result. Include it with the baseline summary when
reporting a problem; neither summary measures physical motor travel.

Acquisition is limited to `baseline_time + 0.5 s` after setup completes.
Earlier buffered data cannot qualify. Data faults abort; no data produces
`no_baseline`. Persistent noise can also prevent a reference outside calibration
before the pass starts. The validated reference is frozen before arming;
there is no interval or minimum travel that ignores an early real impact.
See [the preparation contract](COMPATIBILITY.md#pre-motion-stationary-preparation).

Generic warmup may discard expired backlog before the acquisition cutoff is
established. Obsolete pending samples and timestamp continuity are cleared
with that backlog, preventing the deliberate discard from looking like a
missing-sample gap. The gap checks on retained samples and all measured or
armed intervals remain active.

This is a software noise check, not proof of contact or a guarantee against
every false home. A driver's hold-to-run current change at the first step can
still cause movement after this check. See Klipper's
[`hold_current` guidance](https://www.klipper3d.org/TMC_Drivers.html#prefer-to-not-specify-a-hold_current).
The preparation check does not change driver settings.


## Generic timing, speed and travel limits

The following startup-motion estimator applies only to the legacy selected-axis
path. Simple calibration and saved vector models skip it, retaining sensor data
checks and the full-move travel/time watchdog. Explicit optional admission
limits are still honored; they are not physical stopping guarantees.

Generic defaults are 5 ms reports, a 50 ms maximum sample age and at least
1000 Hz. The previous report interval is restored afterwards. Sample freshness,
stream faults and startup motion detection still apply at every speed.
There is no default 50 mm/s cap or startup-distance admission limit. Default
installation preserves any finite positive configured `homing_speed`, including
75 or 100 mm/s, without adding distance caps. Only an explicit installer
`--homing-speed N` requests a speed edit. Normal toolhead velocity and
acceleration limits still apply; acceleration is never raised automatically.
Native Beacon keeps configured speeds; its expanded-reference checks are
described above.

The 50 ms sample-age default applies to stationary preparation and armed homing;
samples older than the configured limit are rejected. Explicit `max_sample_age`
values are preserved. The generic no-data watchdog uses the lesser of
`stream_timeout` (50 ms by default) and `max_sample_age`, so an explicit shorter
`stream_timeout` still stops missing delivery earlier. The travel-deadline grace
uses that same effective timeout. Native Beacon already uses a 50 ms default and
is unchanged.

`max_report_travel` and `max_startup_travel` are independent optional limits,
disabled when absent. An existing explicit value stays active. The installer
flags with the same names opt into those limits, and are refused for native
Beacon. A configured reporting limit requires effective speed at most
`max_report_travel / max_sample_age`; a configured startup limit checks nominal
travel before a host stop request. Explicit values accept finite ranges
0.1..2 mm and 0.1..5 mm respectively. An incompatible explicit limit is refused
before installation writes, without lowering speed or clearing that limit.
With sample age omitted, raising its default from 20 to 50 ms lowers a configured
1 mm report limit's speed ceiling from 50 to 20 mm/s. The unchanged legacy
startup calculation also gains 30 ms and `speed * 0.030` mm of nominal request
travel. Existing explicit distance limits can therefore reject a previously
admitted plan; they are not raised or removed automatically.
An update also preserves a 50 mm/s value written by an older installer; it
does not infer or restore a historical speed from backups. Those backups
retain the original configuration for review.
Runtime checks use the current toolhead velocity and acceleration limits,
including temporary changes left by a macro or tuning routine. A nonzero
`homing_accel` is only an upper limit: it cannot raise a lower live acceleration.
CALIBRATE/TEST/CHARTS check their requested generic X/Y plans before changing
workflow state or moving; homing preparation checks them again. Ordinary
`G28 X/Y` still uses the configured public handler, so `safe_z_home` can perform
its Z hop or `homing_override` its script before a later X/Y policy refusal.
No Z settings are changed.

A generic home with no impact must establish motion before a calculated
deadline or fail with `no_motion`; this does not establish an origin. Ordinary
`G28` can finish on a fresh impact before motion proof; workflows also check
motion and travel.

In the legacy selected-axis detector, when a generic reference exceeds the original 20% quiet limit,
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

The generic path retains a conservative speed peak from an eligible report
endpoint before the existing startup deadline. Once the ordinary lagged
step-history comparison is eligible, that peak can establish motion only if
it reached at least **half the full effective homing speed**. This preserves
valid early evidence when its later instantaneous lower bound has fallen due
to accumulated noise uncertainty. The peak is not compared against half of a
later, smaller commanded speed. The current-bound test remains available;
raw impact thresholds, data faults and the startup deadline are unchanged.

Generic `no_motion` errors and STATUS now retain the actual motion-proof
diagnostics: raw integrated speed, conservative lower/upper bounds, retained
peak, commanded-speed eligibility, configured effective speed/acceleration,
noise envelope, sample timing and startup deadline. A large raw speed with a
small lower bound indicates uncertainty in that estimate, not verified motion.
The log's impact threshold and this motion proof are separate checks.

The startup deadline remains calculated from effective speed, acceleration,
sample age and reporting margin. Higher speeds or lower acceleration can
increase the nominal travel before the host requests a stop for absent motion
proof. An optional `max_startup_travel` can reject such a plan; no distance cap
is inserted automatically. Installation does not change sample age, reporting
interval, acceleration or current to admit a requested speed.

The **2 mm simple-calibration rule is a separate repeatability test**:
`max(distance) - min(distance) <= 2.0 mm` across ten validation impacts,
excluding discovery. The running spread is checked after each impact; >2 mm
rejects immediately and requires a new discovery before ten fresh checks.
ACCURACY reports the measured spread without applying calibration's 2 mm
acceptance rule; its existing per-home qualification remains in use.
It is not a per-home travel cap or a physical stopping-distance guarantee.
Neither the startup deadline nor optional nominal travel limits include
unbounded host stalls, transport/MCU stop delay or the machine's mechanical
response. A configured speed still needs supervised physical validation.
See [the exact calculation](COMPATIBILITY.md#generic-startup-motion-policy).

Commanded/firmware step distances are not encoder measurements. Ten repeats
cannot prove contact with the correct stop or absence of skipped steps.
Changing the starting threshold does not establish that either problem is fixed.
A softer impact at lower speed may also be undetectable. Mounting and actual
physical behavior need operator validation.


## Test results, accuracy, charts and temporary thresholds

TEST and CHARTS need the same existing manual XY center, both axes unhomed
and nozzle/bed clearance as calibration. `CENTERED=1` declares that preparation
complete; omitting it fails before movement. They start without a preliminary
XY release or a separate confirmation and never request a Z move. Each tested
axis must use a suitable accelerometer endstop. A suspect home aborts without an
automatic return.

`ACCEL_HOMING_CHARTS_GENERATE AXIS=X CENTERED=1` homes and charts X only;
use `AXIS=Y` for Y or omit `AXIS` to home X then Y. Other values are rejected
before renderer preflight or workflow changes. Single-axis charts still require
manual XY centering and both axes unhomed. A successful final home gets the
configured final back-off; no return to center follows a single-axis capture.

`ACCEL_HOMING_ACCURACY AXIS=X SAMPLES=10 CENTERED=1` uses the same preparation.
`AXIS=X` or `AXIS=Y` tests that installed axis; omitting `AXIS` tests X then Y.
It first qualifies a home, excludes it from statistics, returns that trusted
axis to center and records the requested number of additional homes. Each
accepted home is followed by a return of that axis to center only when another
home or axis is needed. The accuracy measurements use fixed thresholds, homing
speed and current.
Midpoint returns use the same ordinary acceleration and explicit settling wait
as calibration, representing the usual return conditions between homes.

An early trigger can start one automatic standard calibration of the failing
axis. Recovery requires the current complete detector capture, the signed
trigger/halt step positions, an intact confirmed center reference and unchanged
operation state. It reverses the recorded halt displacement, waits for completion
and settling, then invalidates XY's rejected coordinate assignment without
disabling the motors or assigning a new center coordinate. This assumes no
skipped steps or slip; it does not measure physical position. Zero-step early
hits require matching zero trigger/halt evidence and need no return move.

The partial accuracy PNG and temporary-capture cleanup finish before calibration
begins. Its explicit initial threshold is the effective threshold used by the
failed accuracy attempt **plus 0.25 g**. The configured `calibration_step`, fresh
three-window noise measurement, sensor limits, discovery impact and full ten-home
repeatability qualification remain active. Recovery does not resume or rerun
accuracy automatically. It saves only after calibration passes, using the normal
`SAVE_CONFIG` command and restart. If a manual shared or axis threshold takes
priority, the selected axis override is staged with the new value as well; the
other axis and shared setting remain unchanged. Native save/include conflicts
are reported as `save_failed`, with no claim that persistence or restart succeeded.
After a successful save and restart, release XY, manually center it with nozzle
clearance, and run the exact ACCURACY command printed in the console.

Missing/stale halt evidence, a lost center reference, sensor faults, cancellation,
failed PNG/temporary cleanup or thresholds outside calibration limits prevent
automatic calibration. Use the accessible emergency stop if active movement must
be interrupted; `ACCEL_HOMING_CANCEL` is not an immediate emergency stop for a
running console command.

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
ACCURACY creates per-home PNGs in a private run-specific OS temporary directory
outside the chart/config roots. Their immutable receipts and PNG bytes are
validated before final publication. Only one final overview PNG per requested
axis is kept in the chart directory, with commanded travel/statistics, accepted
XYZ traces and the actual trigger threshold. Temporary PNGs are removed after
the summaries are saved, and on abort/cancel or export failure; any final partial
summaries remain available. Temporary paths are omitted from public status/results.
Overview dimensions and stored traces stay bounded for up to 100 measured homes.

An accepted impact capture must identify exactly one retained sample from the
original trigger packet and sample index. Its copied report uses that row's
exact onset time and retains the original trigger time separately. Missing or
ambiguous provenance aborts capture validation; no nearby row or synthetic
sample is substituted. This correction was reviewed without executing tests.

The console reports acceleration at impact detection, its measured peak in g
and commanded travel. Charts show X/Y/Z acceleration relative to the stationary
baseline, configured trigger threshold and detected onset marker when available.
That threshold is the value used by the detector, not a newly calibrated minimum
g requirement. The measured onset and peak can differ from it. All new chart
outputs, including incomplete reports, are **PNG only**: no CSV, JSON, SVG or
HTML sidecars. Each image uses bounded samples that retain signed extrema;
there is no full-resolution raw-data export. Previously generated files are
retained. Titles show the host's local time as `YYYY-MM-DD HH:MM:SS`.
Filenames also use `HH:MM:SS`, for example
`Accel Homing Accuracy X - 2026-10-27 23:59:42.png`; PNG metadata retains its
timezone offset and UTC provenance. Windows downloads need a colon-free name.
The default `accel_homing_charts/` directory is beside the
main `printer.cfg`; `charts_directory` changes it. See the
[configuration example](../examples/accel_homing.cfg) for all settings.

The installer prepares plotting dependencies for the selected Klipper Python
in a private, ignored `.plot-deps/` cache. Global package pins are unchanged;
the cache is retained on uninstall. Matplotlib/Agg renders in a separate
process while Klippy keeps servicing its reactor. A small real render is
checked before CHARTS/ACCURACY movement or stationary noise acquisition. A
missing renderer fails before those operations begin.

Completed PNGs can appear inline when a verified Moonraker file route and the
frontend's formatted console are available. Mainsail's raw-output mode does
not render inline images. The PNG path remains available to open directly;
direct preview needs a same-origin Moonraker route accessible to an ordinary
browser GET, such as a trusted LAN or session-cookie route. Remote frontends
or header-token-only authentication may require opening the PNG through the
frontend file browser. No tokens are embedded in chart links.

PNG-root discovery accepts a same-user Moonraker launched with path arguments
or `MOONRAKER_DATA_PATH`/`MOONRAKER_CONFIG_PATH`, using its own home/defaults.
It requires exactly one process matching this Klippy socket. Unresolved
includes/templates, relative socket paths or ambiguous matches keep the saved
PNG path available without an inline URL. That advisory does not itself abort
ACCURACY or invalidate its measurements. This discovery correction was reviewed
as source only; no host's current route or browser preview was verified.

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


## Legacy names and provenance

Historical `[beacon_accel_homing]`, `beacon: beacon`, `beacon_accel:x/y`,
`BEACON_ACCEL_HOMING_STATUS` and `SET_BEACON_ACCEL_HOMING` remain accepted. Load
only one plugin section; existing Beacon thresholds are retained.

The original base is upstream commit
[`f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4`](https://github.com/Monolith3D/MISC/commit/f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4),
including its `Co-Authored-By: Claude Opus 5.5` provenance. The original project
credited FrankRamm and Ingstronmentalist for printer testing. The FrankRamm
fork adds generic backends, supervised workflows, saved models, charts and
transactional installation for eventual upstream contribution after physical
validation. Full attribution remains in [NOTICE](../NOTICE) and [LICENSE](../LICENSE).
