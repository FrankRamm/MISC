# Compatibility evidence and limits

This rewrite was verified offline. No new sensor, printer, homing impact,
calibration or first-layer result was tested on hardware. The original
Beacon-only project's historical printer reports are not evidence that these
changes work on every ADXL carrier, firmware version, fork or mechanism.

The implementation uses capability checks instead of identifying forks from
their checkout name. Required host APIs include Klipper endstop registration,
`mcu.TriggerDispatch`/trsync support, the normal homing event path, and a
supported accelerometer delivery contract. Missing interfaces fail explicitly.
Supporting an older driver's `api_dump` does not imply that all of Klipper
v0.12 is supported; older hosts may still lack required MCU/homing APIs.

Source contracts inspected for this work:

| Source | Pinned revision and relevant interface |
| --- | --- |
| [Klipper](https://github.com/Klipper3d/klipper/tree/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51) | `ee2c6fb6b00b2b5d097ef70d0868df7d74943b51`: `klippy/mcu.py`, `extras/adxl345.py`, `bulk_sensor.py`, LIS2DW/LIS3DH, MPU9250 and ICM20948 drivers |
| [Kalico](https://github.com/KalicoCrew/kalico/tree/0057b6a153170807e99080360c1ef15a2078f672) | `0057b6a153170807e99080360c1ef15a2078f672`: corresponding MCU, bulk sensor and ADXL interfaces |
| [Beacon](https://github.com/beacon3d/beacon_klipper/blob/3eb0134607664734ccf5cd94cb51840e1c8b27b3/beacon.py) | `3eb0134607664734ccf5cd94cb51840e1c8b27b3`: raw accelerator helper, scale and axes map |
| [Cartographer configuration](https://github.com/Cartographer3D/docs/blob/2e7c69641fb106cf9c59d70f3987008a1117c887/cartographer-probe/installation-and-setup/installation/klipper-configuation.md) | Separate configured `[adxl345]` or `[lis2dw]` object through its MCU; scanner distance data is not substituted for acceleration |
| [Klipper safe_z_home](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/extras/safe_z_home.py), [Kalico safe_z_home](https://github.com/KalicoCrew/kalico/blob/0057b6a153170807e99080360c1ef15a2078f672/klippy/extras/safe_z_home.py) | Built-in `SafeZHoming.cmd_G28` retains the previous handler as `prev_G28`; configured `z_hop` runs before axis selection |

These revisions establish API assumptions, not a promise that only those
revisions run or that later versions remain compatible. Standard generic
decoded batches are timestamped, machine-mapped acceleration in mm/s² and are
normalized to g. The verified generic driver ranges are 16 g for ADXL345,
LIS2DW/LIS3DH and ICM20948, and 8 g for MPU9250. Unknown compatible sensor
objects need their actual `sensor_range`; brand/connector names do not reveal
that configured range.

The generic path temporarily shortens batching and rejects stale/low-rate,
error-incremented, unchanged/frozen, malformed or saturated data. It restores
the previous reporting interval on completion/failure. Beacon retains its
direct raw path and existing trigger dispatch. Both rely on host/transport
scheduling and cannot guarantee a hard end-to-end stop latency.

## Loading code and configuration

Installing or updating Python code/module links automatically restarts the
verified systemd service for the selected Klipper instance, after successful
installation and a fresh identity/idle check. The installer obtains any
needed sudo privilege before writing files. `--no-restart` opts out; use it
for a service manager the installer cannot verify and restart that host
process manually while idle. Full uninstall and explicit rollback also leave
the host-process restart to the operator. See
[the activation contract](INSTALLATION.md#automatic-host-service-restart).

Console `RESTART` and `FIRMWARE_RESTART` recreate printer objects within the
same Python process. Extras use normal cached imports, so those commands do
not reload already imported modules after a source/link change. This follows
the inspected [Klipper import path](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/klippy.py#L87-L110),
[Klipper restart loop](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/klippy.py#L343-L359)
and [Kalico restart loop](https://github.com/KalicoCrew/kalico/blob/0057b6a153170807e99080360c1ef15a2078f672/klippy/printer.py#L632-L648).
Configuration-only threshold edits can still use console `RESTART`, and
calibration `SAVE_CONFIG` retains its normal configuration restart.

## safe_z_home and X/Y-only workflows

The standard Klipper/Kalico `[safe_z_home]` is supported without the installer's
`--acknowledge-homing-override` flag. Cartographer's
[Klipper setup guide](https://docs.cartographer3d.com/cartographer-probe/installation-and-setup/software-configuration/klipper-setup)
recommends this built-in path for homing Z at a suitable bed position.
The installer preserves the section and probe/Z settings.

Even `G28 X` sent to the standard safe Z handler can run its configured
`z_hop` before checking which axes were requested. CALIBRATE, TEST and CHARTS
therefore verify that the active `G28` is either stock or the recognized
built-in `SafeZHoming.cmd_G28` whose `prev_G28` is stock. They call the verified
stock handler directly for the requested X or Y axis only, rechecking the path
before every trial. Both active and ready G28 registrations must agree.
This bypass is limited to workflow homes; it neither replaces the registered
`G28` command nor changes normal safe Z homing, Cartographer probing, saved Z
data or the wrapper's settings. Ordinary `G28 X/Y` retains any configured safe
Z hop.

`homing_override`, G28 macros, unrecognized/replaced handlers and chains with
a custom handler above or below `safe_z_home` remain unsupported by workflows.
Validation fails before the affected homing attempt, including when
installation was manually acknowledged. This support follows inspected source
contracts and offline fixtures; no Cartographer or safe Z path was physically
tested for this fix.
The independent-Z and other workflow restrictions still apply.
Before manual centering and these X/Y-only workflows, the operator must ensure
nozzle/bed clearance for XY travel because the safe Z hop will not run.

## Pre-motion stationary preparation

Motor enable and current setup can produce an acceleration transient that
resembles a stop impact. Both Beacon and generic paths therefore acquire a
new quiet reference before every homing pass, including normal `G28`, workflow
homes, already enabled motors and second touches.

The preparation order is:

1. Enable any disabled motors required for the active axis, including both
   CoreXY drives. Queue `enable_settle_time` after a new enable (0.25 s by
   default; finite values from 0 to 2 s). A zero delay does not bypass the
   stationary-data requirement.
2. At `homing_move_begin`, flush step generation and wait for queued setup,
   enable settling, plugin backoff and any homing retract to complete. This
   check precedes the upcoming homing move; it does not prohibit the earlier
   configured backoff or retract.
3. Establish a new sensor-time cutoff after that wait. Discard buffered
   samples from before the cutoff and require at least 50 fresh,
   contiguous samples spanning `baseline_time` (default 0.1 s). For native
   Beacon, peak-to-peak variation on each of the three sensor axes must not
   exceed `max(0.20 * impact_threshold, min(0.5 g, impact_threshold))`, including
   a calibration candidate. The floor is bounded by 0.5 g and the actual
   threshold; it is not a user setting. Configured generic accelerometers use
   `max(0.20 * impact_threshold, baseline_noise_floor)` on each sensor axis.
4. Freeze the validated reference before `home_start` arms trigger dispatch.
   The first valid impact at or after arming remains eligible, including
   samples delivered in the batch that crosses the arming boundary. There is
   no post-arm blanking interval or minimum-travel impact filter.

`baseline_noise_floor` is in g, defaults to 0.2, and accepts finite values
from 0 to 0.2 inclusive. Zero restores the previous 20% limit. It affects only
configured generic accelerometers, including Cartographer's separately
configured ADXL/LIS object. At a 0.5 g calibration candidate, the default
stationary variation limit becomes 0.2 g; the impact threshold stays 0.5 g.
Native Beacon independently uses its bounded 0.5 g floor. At a 0.5 g candidate
its stationary variation limit is 0.5 g and its raw impact threshold remains
0.5 g. Manual thresholds below 0.5 g cap the floor at the actual threshold;
they remain finite-positive settings without a new minimum. Native thresholds
at least 2.5 g retain their previous quiet limit.
References admitted only by the wider limit use the motion checks below.

Acquisition has a `baseline_time + 0.5 s` deadline measured after queued setup
and settling finish. Persistent vibration or no sample flow fails with
`no_baseline` before trigger dispatch is armed. Stale, future, nonmonotonic,
gapped, clipped, saturated or scale-changed data also aborts preparation.
The reference is accumulated in constant storage; `baseline_batches` does
not determine whether this new interval is long enough.

The quiet limits are engineering policies checked by synthetic fixtures;
they are not physically validated boundaries between vibration and contact.
These checks cannot exclude every false home. A driver's transition from hold
current to run current on the first step occurs after the stationary check.
Klipper documents that current changes can themselves move a motor in its
[`hold_current` guidance](https://www.klipper3d.org/TMC_Drivers.html#prefer-to-not-specify-a-hold_current).
This preparation does not change TMC currents, modes or other driver settings.

## Centered calibration

Start or resume with `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`, or `AXIS=Y`
for Y. Before that command, the operator physically centers both XY axes by
hand using motor controls appropriate for the machine, with Z supported.
Both XY axes must be unhomed in Klipper. `CENTERED=1` declares that this manual
preparation is complete and authorizes movement; it is not a sensor position
measurement. The workflow does not assign a center coordinate or mark either
axis homed before the actual home. No further confirmation is requested; the
first homing pass starts after stationary preparation succeeds.

The first home tests the candidate threshold, starting at 0.5 g by default.
Calibration and saved models accept finite thresholds from 0.5 to 15 g, with
the configured calibration ceiling and sensor-range limit still applied.
The default step stays 0.5 g, so the first early-trigger retry uses 1.0 g.
Previous models at 1 g and 2 g remain valid. Manual `threshold`, `x_threshold`
and `y_threshold` settings in either section, and both `SET_ACCEL_HOMING` and
`SET_BEACON_ACCEL_HOMING`, accept any finite value greater than 0 g. They have
no fixed 3 g minimum or 15 g maximum. When the configured/detected sensor range
is known, the effective threshold must be at most 80% of full-scale (12.8 g for
a generic ±16 g sensor); a wider sensor range permits values above 15 g.
The active stream range is checked again before arming, including a narrower
Beacon stream already in use. Native Beacon without known clip/range data
retains the existing behavior without an invented upper limit. A derived raw
trigger limit must also remain finite and positive before arming. These manual
settings do not change the separate 0.5–15 g calibration/model bounds.
An explicit existing `calibration_start_threshold` is preserved. The 0.5 g
default applies when that setting is absent or set to 0.5; updating does not
replace any saved model threshold.
One qualifying discovery is followed by ten additional accepted validation
homes, returning only the tested axis to center between accepted trials.
Each accepted home must exceed 10 mm of commanded travel and fall within the
configured tolerance of expected center-to-stop travel (20% by default).
These geometry checks apply to the discovery and each validation.
New calibrations also require the ten geometry-qualified validation
homes at the same candidate to satisfy `max(travel_mm) - min(travel_mm) < 1.5 mm`.
The limit is fixed; exactly 1.5 mm fails. Discovery and manual centering are
excluded, and X/Y have separate validation windows. If the range reaches the
limit before all ten validations, no later result can make that window pass:
calibration aborts immediately without a higher threshold, another automatic
return/home, model staging, config write or restart. The save path independently
checks ten distinct consecutive homes from the same axis, run and candidate
after the separate discovery. Metadata and progress expose the range and limit.
The comparison uses the decimal reported distances; it does not add physical
precision to the underlying step-count measurement.
An early trigger raises the candidate, resets validation progress, releases
XY and invalidates its homed state while retaining Z. The operator manually
recenters and repeats the same calibration command to resume the pending
candidate and attempt count, keeping the same `AXIS` and optional `SAVE_CONFIG`
value. This existing early-hit classification includes pressed starts, travel
at most 10 mm and travel below the geometry tolerance. It clears all previous
validation distances; the new candidate requires a fresh qualifying discovery
and ten new validation homes. Sensor faults and excessive travel abort.
Native homing errors still
use stock fault cleanup, which may release all motors.
TEST/CHARTS use the same manual preparation, with both XY axes unhomed, and
start directly with `ACCEL_HOMING_TEST CENTERED=1` or
`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`. They retain the prepared XY state
until homing starts; there is no preliminary XY release or separate
confirmation command. All three workflows require `CENTERED=1` before any
workflow movement. Early calibration hits use plain console instructions for
manual recentering and the same calibration command; the timeout remains
active. The workflows emit no UI prompt actions.

Successful calibration defaults to `SAVE_CONFIG=1`, writing the staged model
to `printer.cfg` and restarting Klipper. `SAVE_CONFIG=0` stages only in memory;
it does not write or restart, and the workflow restores the previous active
thresholds. An explicit `SAVE_CONFIG` is still required to persist and reload
the new model; an ordinary `RESTART` discards unsaved staged values. Manual
threshold overrides retain priority after reload.
Existing saved models remain compatible without a new range check on loading.
Their historical validations are not certified by this rule; only a new
successful calibration records the new range qualification.

## Native Beacon with an expanded reference

A native reference whose accepted peak-to-peak variation exceeds 20% of the
active impact threshold carries an observed noise envelope. Each sensor axis
uses `max(abs(low - mean), abs(high - mean))`; a reference within the previous
limit keeps the legacy native motion and already-pressed path.

For an expanded reference, motion proof uses a lower velocity bound after
subtracting that envelope from the accumulated acceleration. A bounded
history retains an early lower-speed peak so watchdog/report alignment does
not discard valid ramp evidence. A retained peak must reach at least half the
full effective homing speed, with the usual later commanded-history evidence;
the instantaneous lower-bound comparison remains available. Automatic impact
direction likewise needs a signed velocity interval entirely beyond +30 or
-30 mm/s; uncertain drift keeps detection bidirectional.

Already-pressed detection uses the **current upper velocity bound**, rather
than treating a small lower bound as proof that the toolhead is stationary.
If uncertainty prevents a valid decision and no raw impact occurs, a finite
startup deadline requests a host stop and reports `no_motion`. That deadline
is the earlier of the existing travel deadline and the generic startup-time
calculation below with native `stream_timeout`
as the sample-age allowance and the native early-report interval as its
reporting interval (3 ms). At 75 mm/s, 5000 mm/s² and the default 50 ms
`stream_timeout`, the startup calculation is 90 ms. No generic travel-budget
setting is applied to Beacon. Expanded native packets require a finite
positive sample interval and strict sample-clock order. Their oldest retained
post-arm sample must be no older than `stream_timeout`, and their newest
sample no more than 5 ms in the future, before proof or trigger dispatch.
Missing, stale, invalid or discontinuous data cannot establish initial motion
proof on this path. A later gap does not revoke already established motion.

Raw baseline-relative impact samples and the configured threshold remain
unchanged. The first valid impact after arming is eligible, including an
impact before motion proof; there is no blanking interval, minimum travel,
automatic threshold increase or speed change. Workflows retain their separate
travel and motion qualification. The observed envelope is not a guaranteed
bound on later disturbances, and the host deadline is not a physical
stopping-distance guarantee. This path has synthetic validation only.

## Generic startup motion policy

While no impact has completed a generic home, fresh acceleration must prove
relative motion by the startup deadline. If it has not, the generic path
requests a host stop and reports `no_motion` as a homing failure. It never
classifies missing motion as a successful already-pressed home. Ordinary `G28`
can finish immediately on a fresh threshold impact even before motion proof;
the calibration/test/chart workflows separately reject suspect motion/travel.
A separate `motion_seen` proof allows slow workflow homes without changing
Beacon's historical already-pressed cutoff.

If any axis of an accepted generic reference exceeds the original 20% quiet
limit, movement proof subtracts an allowance for observed stationary noise.
For each sensor axis `i`, the accepted window supplies its low, high and mean:

```text
noise_bound_i = max(abs(low_i - mean_i), abs(high_i - mean_i))
proof_sum_i = max(0, abs(sum(sample_i - mean_i)) - N * noise_bound_i)
```

`N` includes the first accumulated sample. The existing physical velocity
conversion and vector norm use these bounded sums for movement proof.
Automatic impact-direction selection also uses the noise bound for these
generic references. The relevant signed velocity interval must lie entirely
at least +30 mm/s or at most -30 mm/s before inferring a direction; uncertain
drift leaves automatic detection bidirectional.

The applied bounds appear in status as
`baseline.motion_proof_noise_bound_g` (an X/Y/Z mapping, or `None` when not
applied) and in the console as `motion-proof noise bound XYZ ... g`.
Raw baseline-relative acceleration, impact thresholds, manual direction
overrides, first-impact eligibility and the startup deadline are unchanged.
Native Beacon uses the separate expanded-reference policy above.

The generic adjustment has synthetic validation only. It assumes later
stationary noise stays within the deviations observed in the accepted
window; a larger later disturbance can still resemble motion. Very noisy
or slow cases may fail with `no_motion`. That failure does not automatically
increase speed or impact thresholds.

The startup deadline calculation is unchanged. With effective homing speed `v`
(the lesser of rail speed and toolhead velocity limit), effective acceleration
`a` (current toolhead acceleration limited by nonzero `homing_accel`), and all
times in seconds, the calculation is:

```text
w = max(0.004, min(0.050, 0.050 / v))
T = v / a + 0.015 + max_sample_age + max(0.010, 2 * stream_interval, w)
nominal request travel = v * (T + 0.003) - 0.5 * v * v / a
```

`T` includes the acceleration ramp, 15 ms command/sensor lag allowance,
accepted sample age, and reporting/proof-window margin. The startup watchdog
still requests a stop when motion proof is absent by that deadline. There is
no default report-distance or startup-distance admission cap: a finite positive
configured homing speed is preserved without inserting either option.
Freshness, stream-fault and finite timing checks remain mandatory.

`max_startup_travel` is an optional nominal request-travel limit, disabled when
absent and bounded to finite 0.1..5 mm values when explicitly configured.
`max_report_travel` is independently optional, disabled when absent and bounded
to finite 0.1..2 mm values when configured. An explicit startup limit is checked
before enable/backoff and again before the homing pass; it does not change the
startup deadline or become a physical stopping guarantee.
Resolve the speed/acceleration configuration within the machine's supported
limits before retrying. The installer does not raise acceleration.
The installer preserves the configured `homing_speed` unless an explicit
`--homing-speed N` requests an edit. An incompatible configured or explicitly
requested plan is refused before installation writes; there is no automatic
speed edit or reduction. When a reporting limit is explicitly configured, its
speed ceiling is `max_report_travel / max_sample_age`. For example, an explicit
1 mm limit with 0.020 s sample age imposes 50 mm/s. This is an opt-in limit,
not a default ceiling. Each configured budget applies independently.
Optional installer `--max-report-travel N` and `--max-startup-travel N` flags
select those settings before validation and persist only explicit overrides.
Existing limits and homing speeds are preserved when no matching flag is
given. The flags are refused for native Beacon. The installer changes neither
sample age, reporting interval, current nor acceleration to make a speed pass,
and does not restore a historical speed from older backups.
CALIBRATE/TEST/CHARTS also make a read-only check of their requested generic X/Y
plans before workflow effects, including when both axes are still unhomed.
The owner method `preflight_motion_policy(axes, error=None)` validates selected
module endstops and calls the backend rail validator without moving, enabling
motors, changing settings or consulting a presumed physical start position.
Beacon retains its existing path. Invalid/nonpositive live toolhead limits are
rejected; `homing_accel: 0` leaves the live acceleration in force, and a nonzero
value only caps it. Later preparation checks remain necessary because live
limits can change after this first check.
Ordinary public `G28` is not wrapped by this preflight. With `safe_z_home`, a
configured Z hop can occur before a later X/Y policy rejection. These budget
options do not change the safe Z section or Z settings.

For `v=50 mm/s`, `a=5000 mm/s²`, `max_sample_age=0.020`, and
`stream_interval=0.005`, `T=0.055 s` and nominal request travel is `2.65 mm`.
At 100 mm/s with those timing/acceleration settings, `T=0.065 s` and nominal
request travel is `5.8 mm`. Without an explicit distance cap, neither plan is
rejected for this distance. An explicit `max_startup_travel: 3` would reject
the latter. These are synthetic software calculations, not physical validation
of either speed. Lower live acceleration increases the startup time and travel;
a plugin cap does not raise a lower live value left by a macro or tuning run.
The extra 3 ms covers one nominal watchdog interval. It excludes
unbounded host scheduling stalls, transport and MCU stop delay, and actual
mechanical response. **The deadline and optional nominal travel limits are not
physical stopping guarantees.**
Backend status exposes `startup_motion_request_limit_mm`,
`startup_motion_timeout_s`, and `startup_motion_request_travel_mm` for
inspection; the limit key reflects the explicit `max_startup_travel` or `null`
when disabled. These remain software planning values.

The calibration criterion `max(travel_mm) - min(travel_mm) < 1.5 mm` measures
spread across ten accepted validation homes. It does not constrain the travel
of a single home and does not set either optional distance cap. Host latency,
transport and the mechanical response remain relevant at every speed.

Mechanics remain a separate requirement. The sensor must move with the tested
axis, be mounted firmly and have the correct machine `axes_map`. Toolhead-only
sensors do not measure moving-bed Y impacts. Multi-MCU motor arrangements,
unusual kinematics, independent carriages, endstop geometry and every possible
ADXL carrier have not been physically validated. Supervised workflows limit
their supported kinematics to Cartesian/CoreXY with independent Z and verified
stock G28, either directly or beneath the recognized built-in `safe_z_home`.
Other homing wrappers and potentially Z-coupled paths remain rejected.

Distance qualification uses commanded/firmware step positions, not encoder
measurements. An accelerometer plus step counts cannot distinguish every jam,
skipped step or wrong physical stop from valid motion. A qualifying discovery,
ten validation repeats and a saved model are tuning evidence, not independent
physical metrology or safe stopping certification. Starting at 0.5 g allows a
lower candidate to be evaluated; it does not prove that skipped steps or
inconsistent physical homing are fixed.
