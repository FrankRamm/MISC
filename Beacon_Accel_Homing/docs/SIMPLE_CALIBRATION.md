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

## Screening and validation

1. Command exactly **20 mm toward the stop**, watching acceleration throughout.
   This is a bounded `HomingMove` with `probe_pos=True, check_triggered=False`,
   not a discovery collision and not an endstop-origin measurement.
2. An impact at **less than 20 mm** requests a stop, advances by one configured
   step, clears the candidate and measurements, and releases XY for manual
   recentering. Repeat the same command; its original START_THRESHOLD does not
   reset the pending advanced candidate.
3. Reaching the 20 mm endpoint without an earlier impact selects the candidate.
   Return to the starting center. The screen is never a repeatability sample.
4. Run **ten** center-to-impact trips at the unchanged candidate. Record
   commanded step distance at the first acceleration threshold crossing.
   A validation impact below 20 mm also rejects the candidate and requests
   manual recentering; a data fault or missing impact aborts instead of raising g.
5. After all ten checks, calculate **maximum distance minus minimum distance**,
   not statistical variance and not just consecutive differences. A spread
   **at most 2.00 mm passes**. If it is **greater than 2.00 mm**, advance one
   threshold step, discard the candidate and all ten values, automatically
   return to center, and restart from the 20 mm screen with an empty window.
   All cycles share the configured attempt and threshold ceilings.

Every return reverses **signed halted step travel**, including post-trigger
steps. It does not stamp every hit as a known physical endstop and return a
nominal half-bed distance. The return uses the existing homing acceleration
cap, then `calibration_settle_time` (default 0.25 s) before the next reference.
At screening, the selected coordinate is temporarily assigned the operator-
confirmed center so a bounded move is possible; that is not independent proof
of homing. Only the selected axis moves. Cancellation/failure restores settings
and invalidates/releases XY through the existing cleanup policy.

## Saving and ordinary homing

A clean cycle has one screen plus ten endstop checks. `SAVE_CONFIG=1` is still
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
`ACCEL_HOMING_STATUS` reports the screening/validation phase, threshold,
restart count and current ten-check spread.

## Limits of this method

The operator supplies the initial physical center. Step travel measures issued
motor movement, not encoder-confirmed carriage movement. Returns therefore
assume no skipped steps, slipping belts or blocked motion. Repeatable contact
with the wrong obstruction can pass. The 20 mm screen proves neither the true
endstop location nor adequate endstop-impact amplitude; supervise actual
travel and stop on unexpected behavior. No printer hardware was exercised by
the offline tests shipped with this change.
