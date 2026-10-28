# Simple supervised calibration

The existing command syntax is unchanged:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1 START_THRESHOLD=1 STEP=0.25
```

Use one axis per invocation. `AXIS` selects the printer direction to move,
not an accelerometer channel. Physically center XY, leave both axes unhomed,
keep Z supported/clear, and supervise with an accessible emergency stop.
`CENTERED=1` authorizes movement immediately. Default start/step are 1.00/0.25 g;
explicit older configuration is retained, and command parameters override it.

## Detector

Each move acquires a fresh resting XYZ offset (normally 100 ms, at least
50 contiguous samples). It does not test stationary peak-to-peak quietness or
run a separate noise calibration. For each sample it evaluates
`sqrt((ax-bx)^2 + (ay-by)^2 + (az-bz)^2)` in g, with the offset fixed for the move.
The first sample at or above the candidate threshold requests an immediate
stop. The later recorded impact peak does not delay stopping. Channel
permutations and sign reversals do not change this metric; no homing-axis
mapping or impact-sign choice is needed. Ordinary motion can also cross the
threshold: it is not an independent classifier of mechanical contact.

There is no acceleration-derived velocity integration, noise-envelope motion
proof or startup-motion deadline in this detector. Missing, stale, invalid,
gapped/frozen streams and the full-move travel/time limit still stop with an
error. Host-delivered sensor data does not provide a hard real-time stop bound.

## Impact discovery and validation

1. Home the selected printer axis toward its mechanical endstop, watching
   acceleration throughout. **Reaching 20 mm alone does not stop the move or
   select a candidate.** Discovery requires an actual threshold-crossing impact
   after **more than 20 mm** of commanded travel.
2. An impact at **20 mm or less** stops immediately. If the return checks below
   pass, reverse the selected axis's recorded signed start-to-halt travel,
   including travel after the trigger, to its **estimated starting point**.
   Settle and restart discovery automatically at the next configured threshold
   step, with the previous candidate and measurements cleared. Exactly 20 mm
   is still an early hit; no further command or manual recentering is needed.
3. The first impact **beyond 20 mm** selects the candidate. Stock axis homing
   assigns the endstop coordinate. Move **only that printer axis** to its
   configured midpoint `(position_min + position_max) / 2`. The discovery
   distance is excluded from repeatability because its start was manual.
4. At the unchanged threshold, run **ten** center-to-impact trips, returning
   only the calibrated axis to that same configured midpoint between trips.
   Record commanded step distance at each first acceleration threshold crossing.
   A validation impact at or below 20 mm rejects the candidate and follows the
   same automatic early-impact return and new discovery in step 2. A sensor
   fault or missing impact aborts without a return or an increase in g.
5. After all ten checks, calculate **maximum distance minus minimum distance**,
   not statistical variance and not just consecutive differences. A spread
   **at most 2.00 mm passes**. If it is **greater than 2.00 mm**, advance one
   threshold step, discard the candidate and all ten values, recenter the
   calibrated axis, and restart **impact discovery** with an empty window.
   The new discovery impact also stays outside its following ten measurements.
   All cycles share the configured attempt and threshold ceilings.

Discovery and validation use the same verified stock single-axis G28 path.
There is no standalone 20 mm probe move or successful no-contact endpoint.
Only returns after qualifying impacts target the configured midpoint.
Returns use the existing homing acceleration cap and `calibration_settle_time`
(default 0.25 s). No return command changes the other XY coordinate or Z.
Cancellation/failure restores settings; stock G28's fatal error handling may
turn off all motors, including Z. Keep Z supported as for ordinary homing faults.

## Automatic early-impact returns

An early return requires a successful, complete stock single-axis home and
fresh, internally consistent trigger and halted-step evidence from that exact
attempt. Signed travel must agree with the configured homing direction.
The halted travel must be at least the trigger travel and satisfy all of these
per-return limits:

- Trigger travel is at most 20 mm.
- Travel from trigger to halt is at most 2 mm; total halted travel is at most 22 mm.
- Total halted travel stays at least 2 mm short of the nominal configured
  midpoint-to-endstop distance.
- The reverse target lies inside the configured axis limits.

The existing attempt budget and next-candidate threshold ceiling are checked
before returning. These limits apply to each early return; there is no separate
cumulative return-distance budget. The **2 mm trigger-to-halt allowance** is
independent of the **2 mm ten-check repeatability limit**.

The reverse target is the current coordinate assigned by stock G28 minus the
recorded signed start-to-halt displacement; the return does not reset coordinates.
After returning it clears only the selected axis's homed state before retrying.
It does not accept the early hit as the endstop or command the other XY axis or Z.
Zero halted travel needs no reverse move; it still settles before retrying.
This reverses commanded step travel, not measured physical carriage travel.
Lost steps, slip or an obstruction can
make the estimated starting point wrong, including across repeated retries.

Any homing error, sensor/transport fault, missing or stale evidence, inconsistent
travel, motor release, cancellation, shutdown or failed return check aborts.
There is no automatic recovery from these faults and no manual-recentering
fallback that continues the same run. Prepare the printer again before a new
command after an abort.

## Saving and ordinary homing

A clean cycle has one discovery impact plus ten endstop checks. `SAVE_CONFIG=1` is still
the default: successful calibration stages the selected axis model, saves it,
and restarts Klipper. `SAVE_CONFIG=0` only stages it until an explicit save.
The other axis's saved model is not changed.

New models record `impact_metric: xyz_magnitude` in their JSON metadata. After
reload, that axis uses the **same vector detector and offset-only reference**
for ordinary G28, TEST, CHARTS and ACCURACY. Charts label and plot the magnitude.
Older selected-axis models and explicit `threshold`/`x_threshold`/`y_threshold`
overrides retain their legacy detector. Leave manual overrides commented out
to use the new calibrated model; thresholds from different metrics are not
interchangeable. Existing axes_map remains useful for other Klipper features.

`ACCEL_HOMING_CALIBRATE_NOISE` remains a separate stationary diagnostic. Its
threshold hints are not automatically consumed by simple calibration.
`ACCEL_HOMING_STATUS` reports the discovery/validation phase, threshold,
restart count and current ten-check spread.

## Limits of this method

The operator supplies the initial physical center. Step travel measures issued
motor movement, not encoder-confirmed carriage movement. Returns therefore
assume no skipped steps, slipping belts or blocked motion. Repeatable contact
with the wrong obstruction can pass. An impact beyond 20 mm is a candidate,
not independent proof that the intended endstop was reached. Assigning an
endstop coordinate and returning to its midpoint assumes genuine contact;
supervise actual travel and stop on unexpected behavior. This early-return
revision was reviewed from source only; its regression sources were not run
and no printer hardware or service was accessed.
