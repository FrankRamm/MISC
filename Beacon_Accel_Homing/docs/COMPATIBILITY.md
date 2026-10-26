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

## Centered calibration

Start or resume with `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`, or `AXIS=Y`
for Y. Before that command, the operator physically centers both XY axes by
hand using motor controls appropriate for the machine, with Z supported.
Both XY axes must be unhomed in Klipper. `CENTERED=1` declares that this manual
preparation is complete and authorizes movement; it is not a sensor position
measurement. The workflow does not assign a center coordinate or mark either
axis homed before the actual home.

The first home tests the candidate threshold, starting at 3 g by default.
One qualifying discovery is followed by ten additional accepted validation
homes, returning only the tested axis to center between accepted trials.
An early trigger raises the candidate, resets validation progress, releases
XY and invalidates its homed state while retaining Z. The operator manually
recenters and repeats the same calibration command to resume the pending
candidate and attempt count, keeping the same `AXIS` and optional `SAVE_CONFIG`
value. Sensor faults and excessive travel abort. Native homing errors still
use stock fault cleanup, which may release all motors.
TEST/CHARTS instead start without arguments and wait for
`ACCEL_HOMING_CONFIRM CENTERED=1` or the prompt button; this confirmation
cannot start or resume calibration.

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
their supported kinematics to Cartesian/CoreXY with independent Z and stock
G28; they reject homing wrappers and potentially Z-coupled paths.

Distance qualification uses commanded/firmware step positions. An accelerometer
plus step counts cannot distinguish every jam, skipped step or wrong physical
stop from valid motion. A qualifying discovery, ten validation repeats and a
saved model are tuning evidence, not independent physical metrology or safe
stopping certification.
