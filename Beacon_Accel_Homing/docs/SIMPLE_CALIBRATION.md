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
2. An impact at **20 mm or less** stops immediately, advances by one configured
   threshold step, clears the candidate and measurements, and releases XY for
   manual recentering. Repeat the same command; its original START_THRESHOLD
   does not reset the advanced candidate. Exactly 20 mm is still an early hit.
3. The first impact **beyond 20 mm** selects the candidate. Stock axis homing
   assigns the endstop coordinate. Move **only that printer axis** to its
   configured midpoint `(position_min + position_max) / 2`. The discovery
   distance is excluded from repeatability because its start was manual.
4. At the unchanged threshold, run **ten** center-to-impact trips, returning
   only the calibrated axis to that same configured midpoint between trips.
   Record commanded step distance at each first acceleration threshold crossing.
   A validation impact at or below 20 mm rejects the candidate and requests
   manual recentering. A sensor fault or missing impact aborts without raising g.
5. After all ten checks, calculate **maximum distance minus minimum distance**,
   not statistical variance and not just consecutive differences. A spread
   **at most 2.00 mm passes**. If it is **greater than 2.00 mm**, advance one
   threshold step, discard the candidate and all ten values, recenter the
   calibrated axis, and restart **impact discovery** with an empty window.
   The new discovery impact also stays outside its following ten measurements.
   All cycles share the configured attempt and threshold ceilings.

Discovery and validation use the same verified stock single-axis G28 path.
There is no standalone 20 mm probe move or successful no-contact endpoint.
Every automatic return targets the selected axis's configured midpoint,
not a distance inferred from the manually estimated first starting point.
Returns use the existing homing acceleration cap and `calibration_settle_time`
(default 0.25 s). No return command changes the other XY coordinate or Z.
Cancellation/failure restores settings; stock G28's fatal error handling may
turn off all motors, including Z. Keep Z supported as for ordinary homing faults.

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
supervise actual
travel and stop on unexpected behavior. No printer hardware was exercised by
the offline tests shipped with this change.
