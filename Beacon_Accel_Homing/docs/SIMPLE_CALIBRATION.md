# Simple supervised calibration

The existing command syntax is unchanged:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1
```

Use one axis per invocation. `AXIS` selects the printer direction to move,
not an accelerometer channel. Physically center XY, leave both axes unhomed,
keep Z supported/clear, and supervise with an accessible emergency stop.
`CENTERED=1` authorizes movement after stationary preparation succeeds.

## Stationary noise before movement

Every new per-axis calibration command first acquires three disjoint one-second
stationary-noise windows and exports their summary. This runs once before the
first candidate home, not before each threshold retry or same-run continuation.
X and Y each get a new measurement, including after a save/restart. A previous
standalone `ACCEL_HOMING_CALIBRATE_NOISE` result is never reused.

The measurement requires an idle, unpaused printer, an empty motion queue and
an exclusive sensor stream. It leaves motor enable/current and fan state as
they are. A failed measurement, changed context, sensor fault or failed summary
export aborts calibration before movement; it does not fall back to a default
threshold.

Without an explicit `START_THRESHOLD`, fresh noise selects the initial
candidate using the same baseline-subtracted XYZ magnitude as the detector.
The configured grid starts at 1.00 g with 0.25 g steps by default. Existing
`calibration_start_threshold` and `calibration_step` settings remain the grid
origin and spacing. An explicit `STEP` changes that spacing and still permits
noise-based selection. For an advanced initial-value override:

```gcode
ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1 START_THRESHOLD=1 STEP=0.25
```

This still performs the fresh noise measurement, but starts at the supplied
1.00 g and then uses 0.25 g increments. Command parameters are fixed for the
run. The noise-selected threshold is an initial candidate, not a calibrated
impact threshold or a certified minimum. Stationary measurements do not cover
motion, motor enabling, hold-to-run current changes or first-step transients.
See the [numerical policy and limits](COMPATIBILITY.md#stationary-accelerometer-noise-measurement).

## Detector

Each move acquires a fresh resting XYZ offset (normally 100 ms, at least
50 contiguous samples), separate from the initial three noise windows.
It does not test stationary peak-to-peak quietness. For each sample it evaluates
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

## Threshold and attempt bounds

Calibration starts and saved model thresholds must be finite and at least
0.5 g. `STEP` remains a finite value from 0.1 to 3 g. Calibration has no
configurable g maximum or fixed 15 g maximum. The legacy
`calibration_max_threshold` option is accepted but ignored, with a deprecation
warning, so an existing configuration can still load.

When the sensor range is known, the candidate must remain at or below
`0.8 * sensor_full_scale_g`. This existing 80% rule is a conservative software
headroom policy, not the physical saturation point. The active stream range is
checked again before arming; sensor saturation, invalid data and other fault
checks remain separate. An unknown range prevents automatic noise-based
selection; an explicit, otherwise valid `START_THRESHOLD` can still be used.
No candidate is clamped downward to fit a limit.
Before arming, the derived raw trigger limit and its squared value used by the
XYZ-magnitude detector must both be finite and positive. This validates numeric
representation rather than adding a configurable g maximum.

The existing monitored-home budget, 40 attempts by default, includes discovery,
validation and all retries. Before advancing a rejected candidate, at least
**11 attempts must remain**: one fresh discovery plus ten validation homes.
This is checked before a recovery return, distance reservation, threshold
increase or recentering. There is no separate recovery-count cap, but the
attempt and cumulative recovery-distance budgets still bound retries.

## Impact discovery and validation

1. Home the selected printer axis toward its mechanical endstop, watching
   acceleration throughout. **Reaching 20 mm alone does not stop the move or
   select a candidate.** Discovery requires a threshold-crossing impact after
   **more than 20 mm** of commanded travel that also passes the travel
   plausibility check below.
2. An impact at **20 mm or less**, or below the expected-distance band defined
   below, stops immediately and is premature. If all return checks and the
   command-wide recovery budgets pass, reverse the selected axis's recorded
   signed start-to-halt travel, including travel after the trigger, to its
   **estimated starting point**. After a successful return and settling, advance
   one threshold step and restart discovery with the previous candidate and
   measurements cleared. Exactly 20 mm is still an early hit; no further
   command or manual recentering is needed for an accepted recovery.
3. The first plausible impact **beyond 20 mm** selects the candidate. Stock
   axis homing assigns the endstop coordinate before its result is checked.
   After the travel check passes, move **only that printer axis** to its
   configured midpoint `(position_min + position_max) / 2`. The discovery
   distance is excluded from repeatability because its start was manual.
4. At the unchanged threshold, run **ten** center-to-impact trips, returning
   only the calibrated axis to that same configured midpoint between trips.
   Record commanded step distance at each first acceleration threshold crossing
   and check its travel plausibility before another automatic move.
   A premature validation impact rejects the candidate and follows the same
   bounded return and new discovery in step 2. A sensor fault or missing impact
   aborts without a return or an increase in g.
5. After **each** accepted validation impact, calculate the running **maximum
   distance minus minimum distance**, not statistical variance or just
   consecutive differences. If the spread is **greater than 2.00 mm**, reject
   the candidate immediately without waiting for ten checks. Advance one
   threshold step, discard the candidate and its values, recenter the calibrated
   axis, and restart **impact discovery** with an empty window. Passing still
   requires **ten** validation impacts with a spread **at most 2.00 mm**;
   exactly 2.00 mm passes. The new discovery impact also stays outside its
   following ten measurements. All cycles share the attempt budget and
   sensor-range headroom policy described above.

Discovery and validation use the same verified stock single-axis G28 path.
There is no standalone 20 mm probe move or successful no-contact endpoint.
Only returns after qualifying impacts target the configured midpoint.
CALIBRATE and ACCURACY share the helper for midpoint returns. These returns use
ordinary acceleration restored after homing, then wait `calibration_settle_time`
(default 0.25 s) before the next acquisition. They do not apply the homing
acceleration cap to midpoint returns. Homing speed and its acceleration cap
are unchanged. No return command changes the other XY coordinate or Z.
Cancellation/failure restores settings; stock G28's fatal error handling may
turn off all motors, including Z. Keep Z supported as for ordinary homing faults.

## Travel plausibility before midpoint returns

To qualify, discovery and every validation impact beyond 20 mm must agree with the
configured midpoint-to-endstop distance. Let `expected` be the absolute
distance from `(position_min + position_max) / 2` to `position_endstop`.
The recorded commanded trigger travel must fall inside the inclusive band
`expected * (1 - calibration_travel_tolerance)` through
`expected * (1 + calibration_travel_tolerance)`. The existing tolerance defaults
to `0.20` (20%); this is separate from the 2 mm repeatability limit.

The same classification applies to discovery and every validation impact:

- At or below 20 mm, or strictly below the lower edge of the band: premature;
  attempt the bounded recovery below. Exactly 20 mm never qualifies.
- Beyond 20 mm and inside the inclusive band: plausible; use the existing
  configured-midpoint return and qualification cycle.
- Beyond the upper edge of the band, unless it is at or below 20 mm: abort
  without a return, threshold increase or model write.

For example, `expected = 109.25 mm` and tolerance `0.20` give an inclusive band
of `87.4` through `131.1 mm`. A `51.175 mm` trigger is premature even though it
exceeds 20 mm; it can be recovered only if the separate return checks and
remaining budgets pass. Exactly `87.4 mm` is plausible, whereas a trigger above
`131.1 mm` aborts.

An aborted suspect impact invalidates only the selected axis's homed state;
the other XY axis and Z are preserved. Physically center XY and prepare a new
command after an abort. The midpoint return after a plausible impact is
unchanged; recovery does not introduce an independently measured physical
reference.

This is a commanded-travel plausibility check, not a measurement of the manual
starting position or proof of contact with the intended stop. Skipped steps,
slip or an obstruction can still produce plausible recorded travel.

## Automatic early-impact returns

An early return, including one after more than 20 mm but below the lower
expected-distance limit, requires a successful, complete stock single-axis home and
fresh, internally consistent trigger and halted-step evidence from that exact
attempt. Signed travel must agree with the configured homing direction.
The halted travel must be at least the trigger travel and satisfy all of these
per-return limits:

- Trigger travel is at most 20 mm or strictly below the lower expected-distance
  limit.
- Travel from trigger to halt is at most 2 mm.
- Total halted travel is at most `min(max(20 mm, lower limit) + 2 mm,
  expected - 2 mm)`. Thus every return stays at least 2 mm short of the nominal
  configured midpoint-to-endstop distance.
- The reverse target lies inside the configured axis limits.

The next candidate must pass the threshold checks and have at least 11 remaining
attempts before any return or reservation. There is no separate limit of eight
recoveries. A cumulative distance limit still applies to the entire command,
including both the original <=20 mm returns and the longer premature returns:
total reserved **absolute start-to-halt distance**, including post-trigger
travel, must be no greater than `nominal_opposite_clearance - margin`, where
`margin = max(2 mm, 10% of nominal_opposite_clearance)`.

Let `center = (position_min + position_max) / 2`. Nominal opposite clearance is
`center - position_min` for positive homing, or `position_max - center` for
negative homing; it is not measured physical clearance. For a nominal opposite
clearance of `109.25 mm`, the margin is `10.925 mm` and the total distance budget
is `98.325 mm`. The distance boundary is inclusive: exactly the limit is allowed
if every other check passes. This is a fixed internal geometric budget, with
no new configuration setting.

Only premature-impact recovery reversals consume this distance budget. Midpoint
returns after qualified impacts and other workflow motion are not charged;
it does not bound total workflow travel or establish physical position.

Reserve the distance and record the recovery count before issuing a return.
A zero-distance recovery still increments the count; its triggering home has
already used an attempt. Reservations persist across every threshold/candidate
reset and same-command continuation, and are
not refunded if a return is interrupted or fails. Only a new calibration
command starts a fresh budget. A denied recovery makes no return and does not
increase the threshold; it invalidates only the suspect axis's homed state and
requires manual XY centering before a new command. An accepted recovery commits
the next threshold only after its return succeeds.

The **2 mm trigger-to-halt allowance**, the **2 mm minimum nominal endstop
clearance** and the **2 mm running-spread repeatability limit** are separate
checks. Early reversals keep their existing homing acceleration cap and
`calibration_settle_time` wait (default 0.25 s).

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

New calibration metadata also records `travel_plausibility_required: true`,
`recenter_acceleration_policy: restored_normal_limit` and the configured
settling interval. `physical_position_verified` remains false.

`ACCEL_HOMING_CALIBRATE_NOISE` remains a separate stationary diagnostic. Its
mapped X/Y component hints do not select a later calibration's initial value;
that calibration uses its own fresh XYZ-magnitude measurement.
`ACCEL_HOMING_STATUS` reports the discovery/validation phase, threshold,
restart count and current ten-check spread. Its `stationary_noise` summary
identifies the fresh measurement, magnitude peak, initial-value source and PNG.
The nested `recovery` object in active calibration status, the final result and
saved model metadata reports reserved `count`/`distance_mm`, their limits and
remaining values, plus `nominal_opposite_clearance_mm` and `margin_mm`.
`count` remains telemetry; `count_limit` and `count_remaining` are `null` because
there is no separate recovery-count cap. Distance limits and remaining distance
remain numeric. Active calibration status and final results also expose
`attempt_limit`, `attempts_remaining` and `candidate_attempts_required` (11).
`accounting` is `reserved_start_to_halt_including_overshoot`, and
`physical_clearance_verified` remains false. A final result also retains
`candidate_g` and `restarts` when a recovery is denied. These counters describe
reserved commanded travel, including a return that did not complete.

## Limits of this method

The operator supplies the initial physical center. There is no encoder feedback:
step travel measures issued motor movement, not confirmed carriage movement.
Returns therefore assume no skipped steps, slipping belts or blocked motion.
Repeatable contact with the wrong obstruction can pass. A plausible impact
beyond 20 mm is a candidate,
not independent proof that the intended endstop was reached. Assigning an
endstop coordinate and returning to its midpoint assumes genuine contact;
supervise actual travel and stop on unexpected behavior. A premature trigger
may arise from vibration, a real obstruction or binding; the workflow does not
automatically diagnose which occurred. The return budgets bound commanded
recovery but do not prove that the carriage physically returned. This calibration
validation revision was reviewed from source only; its regression sources were
not run and no printer hardware or service was accessed.
