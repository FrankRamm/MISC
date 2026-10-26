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

After installing, updating, uninstalling or rolling back Python code/module
links, manually restart the selected Klipper host process while idle. Use the
actual service and restart command printed by the installer, or the correct
host process manager when no service was detected. No restart is automatic.

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
   contiguous samples spanning `baseline_time` (default 0.1 s). On each of
   the three sensor axes, peak-to-peak variation must not exceed 20% of the
   active pass's impact threshold, including a calibration candidate.
4. Freeze the validated reference before `home_start` arms trigger dispatch.
   The first valid impact at or after arming remains eligible, including
   samples delivered in the batch that crosses the arming boundary. There is
   no post-arm blanking interval or minimum-travel impact filter.

Acquisition has a `baseline_time + 0.5 s` deadline measured after queued setup
and settling finish. Persistent vibration or no sample flow fails with
`no_baseline` before trigger dispatch is armed. Stale, future, nonmonotonic,
gapped, clipped, saturated or scale-changed data also aborts preparation.
The reference is accumulated in constant storage; `baseline_batches` does
not determine whether this new interval is long enough.

The 20% quiet limit is an engineering policy checked by synthetic fixtures,
not a physically validated boundary between vibration and contact. It cannot
exclude every false home. In particular, a driver's transition from hold
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

The first home tests the candidate threshold, starting at 2 g by default.
One qualifying discovery is followed by ten additional accepted validation
homes, returning only the tested axis to center between accepted trials.
An early trigger raises the candidate, resets validation progress, releases
XY and invalidates its homed state while retaining Z. The operator manually
recenters and repeats the same calibration command to resume the pending
candidate and attempt count, keeping the same `AXIS` and optional `SAVE_CONFIG`
value. Sensor faults and excessive travel abort. Native homing errors still
use stock fault cleanup, which may release all motors.
TEST/CHARTS use the same manual preparation, with both XY axes unhomed, and
start directly with `ACCEL_HOMING_TEST CENTERED=1` or
`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`. They retain the prepared XY state
until homing starts; there is no preliminary XY release or separate
confirmation command. All three workflows require `CENTERED=1` before any
workflow movement. Early calibration hits use plain console instructions for
manual recentering and the same calibration command; the timeout remains
active. The workflows emit no UI prompt actions.

## Generic startup motion policy

While no impact has completed a generic home, fresh acceleration must prove
relative motion by the startup deadline. If it has not, the generic path
requests a host stop and reports `no_motion` as a homing failure. It never
classifies missing motion as a successful already-pressed home. Ordinary `G28`
can finish immediately on a fresh threshold impact even before motion proof;
the calibration/test/chart workflows separately reject suspect motion/travel.
A separate `motion_seen` proof allows slow workflow homes without changing
Beacon's historical already-pressed cutoff.

There are no new settings for this policy. With effective homing speed `v`
(the lesser of rail speed and toolhead velocity limit), effective acceleration
`a` (current toolhead acceleration limited by nonzero `homing_accel`), and all
times in seconds, the calculation is:

```text
w = max(0.004, min(0.050, 0.050 / v))
T = v / a + 0.015 + max_sample_age + max(0.010, 2 * stream_interval, w)
nominal request travel = v * (T + 0.003) - 0.5 * v * v / a
```

`T` includes the acceleration ramp, 15 ms command/sensor lag allowance,
accepted sample age, and reporting/proof-window margin. Before motor enabling
or plugin backoff, a generic plan with nominal request travel above a fixed
**3 mm** limit is rejected; planning checks run again before the homing pass.
Resolve the speed/acceleration configuration within the machine's supported
limits before retrying. The installer does not raise acceleration.

For `v=50 mm/s`, `a=5000 mm/s²`, `max_sample_age=0.020`, and
`stream_interval=0.005`, `T=0.055 s` and nominal request travel is `2.65 mm`.
At `v=20 mm/s` with the same other settings, `T=0.049 s` and the budget is
`1.0 mm`. The extra 3 ms covers one nominal watchdog interval. It excludes
unbounded host scheduling stalls, transport and MCU stop delay, and actual
mechanical response. **The 3 mm policy is not a physical stopping guarantee.**
Backend status exposes `startup_motion_request_limit_mm`,
`startup_motion_timeout_s`, and `startup_motion_request_travel_mm` for
inspection; these remain software planning values.

Mechanics remain a separate requirement. The sensor must move with the tested
axis, be mounted firmly and have the correct machine `axes_map`. Toolhead-only
sensors do not measure moving-bed Y impacts. Multi-MCU motor arrangements,
unusual kinematics, independent carriages, endstop geometry and every possible
ADXL carrier have not been physically validated. Supervised workflows limit
their supported kinematics to Cartesian/CoreXY with independent Z and verified
stock G28, either directly or beneath the recognized built-in `safe_z_home`.
Other homing wrappers and potentially Z-coupled paths remain rejected.

Distance qualification uses commanded/firmware step positions. An accelerometer
plus step counts cannot distinguish every jam, skipped step or wrong physical
stop from valid motion. A qualifying discovery, ten validation repeats and a
saved model are tuning evidence, not independent physical metrology or safe
stopping certification.
