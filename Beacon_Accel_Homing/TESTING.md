# Offline verification

All recorded verification uses synthetic Klipper objects, fake sensor
batches, temporary config files and mocked status/download responses. No
printer connection, SSH session, deployment, service restart, physical home,
calibration or hardware measurement was performed.

Run from the repository root with Python 3:

```bash
python -m unittest discover -s Beacon_Accel_Homing/tests -v
```

For shell-entry-point changes, also run:

```bash
bash -n Beacon_Accel_Homing/install.sh
bash -n Beacon_Accel_Homing/bootstrap.sh
bash -n Beacon_Accel_Homing/uninstall.sh
```

Focused installer/bootstrap/uninstall and workflow/homing-path checks:

```bash
python -m unittest discover -s Beacon_Accel_Homing/tests -p test_installer.py -v
python -m unittest discover -s Beacon_Accel_Homing/tests -p test_workflows.py -v
```

Focused motor-enable and fresh-reference fixtures, run from
`Beacon_Accel_Homing/tests/`:

```bash
python -m unittest test_session.EnablePreparationTest test_backends.GenericPreparationTest -v
```

The recorded combined run includes **133 installer/bootstrap/uninstall tests:
132 passed and one real POSIX symlink test was skipped** because the Windows
host does not permit symlink creation. Temporary regular-file fixtures cover
installation, config idempotence, backup/rollback, full-history restore,
failed-write recovery, concurrent-edit rejection and later-edit protection.
Full restore/link-history checks run with isolated link-state emulation.
Actual symlink creation must still be exercised on a Linux host before
claiming a host integration test. All three shell entry points are byte-identical to the source base
`923a0845c74fe0e1f77e55f641ccd73095d3f356`, so their syntax checks were not repeated. No native
Linux systemd/Unix-socket integration run was performed here.

The need for a full host-process restart follows inspected upstream
import/restart source, documented in
[compatibility evidence](docs/COMPATIBILITY.md#loading-code-and-configuration).
Installer activation fixtures use mocked systemd/process/privilege operations.
They cover default install/update restart, `--no-restart`, selected-unit and
live-process identity checks, a fresh idle/pause check before activation,
authorization before writes, unchanged-package activation, and retained
installation/backups on restart failure. Dry run, uninstall and rollback do
not automatically restart. No real service restart or live module reload was
tested. Configuration-only edits retain console `RESTART` and normal
calibration `SAVE_CONFIG` behavior.

The restart coverage includes **15 `test_installer.RestartFixture` tests**.
The final combined offline verification run discovered **427 tests: 426 passed,
zero failures/errors, and the same one Windows symlink skip**. The portable
machine-readable report and complete referenced log are together under
[`validation/VALIDATION.json`](validation/VALIDATION.json) and
[`validation/tests.txt`](validation/tests.txt).

Installer fixtures cover main/include discovery, stopped/running/multiple
instances, process command and service parsing, configured sensor selection,
mixed-case names, generic speed limits, legacy Beacon settings, wildcard include
deduplication, unchanged saved blocks/macros, required-file conflicts, custom
G28/homing_override refusal, standard safe_z_home preservation, read-only dry
run, wrong-socket identity, printing refusal, immutable-ref downloads, pinned
Git blob integrity, valid-Python-prefix
truncation, shell invocation truncation, cache integrity, and aborted downloads.
Publication fixtures also execute both bootstrap entry points with mocked
downloads to verify their FrankRamm testing-fork defaults, immutable revision
pinning and forwarded installer arguments, and verify that the real source
package retains its GPLv3 license and original CloakedWayne credit/provenance.
Installer speed fixtures verify preservation of configured homing speeds and
finite effective plans before writes. Explicit `--homing-speed` edits remain
opt-in; a targeted X=75/Y=50 fixture uses no travel-budget flags. Absent
`max_report_travel` and `max_startup_travel` settings disable those optional
admission policies. There is no default plugin 50 mm/s ceiling or nominal
startup-distance cap. Firmware velocity limits, actual accelerometer
freshness/rate/fault checks and the existing `no_motion` deadline remain.
If a user explicitly supplies either travel setting, that policy is checked
independently. Acceleration, report interval and sample-age settings are never
automatically changed to admit a speed. Updating also does not infer a
historical speed from backups.
Prepared 75 and 100 mm/s synthetic plans exercise both directions through
fresh-reference acquisition, arming and watchdog handling. Moving traces
establish motion; parked varying traces still fail with `no_motion` at the
unchanged calculated deadline. Separate advanced-policy fixtures exercise
explicit 1 mm/3 mm constraints and constructed faster reporting. These
signals do not qualify real hardware timing or stopping distances.

Full-uninstall fixtures cover multiple upgrades back to the original
activation/config state, restored original links and sensorless settings,
unrelated later edits and saved Beacon/bed-mesh data, removal of introduced
plugin models, restoration of original plugin models, and strict undo of
the uninstall transaction. They also cover repeated uninstall, CRLF files,
SAVE_CONFIG-commented original model options, incomplete history, missing
original sources, malformed/conflicting records and unowned plugin references.
Shared-checkout fixtures reject module-link changes when another discovered
config uses the checkout, including an unreadable peer config or a new
consumer found after planning. Hosted shell fixtures use mocked downloads
and exercise verified offline-cache dispatch and download/refusal paths.

Core/backend and workflow suites separately cover trigger/session behavior,
mapped generic data and units, timing/rate/error guards, staged/independent
models, explicit `CENTERED=1` consent, verified stock G28 requirements,
suspect travel without automatic returns, and measured chart export. Synthetic
chart fixtures are explicitly marked as simulated.

Safe Z compatibility fixtures verify the recognized built-in handler over
stock `G28`, including a nonzero configured `z_hop`. CALIBRATE, TEST and CHARTS
use only the verified stock X/Y handler, with no safe Z hop or change to the
public `G28` registration. Handler validation repeats before each trial.
Unknown/custom wrappers, nested handler chains, mismatched registrations,
`homing_override` and G28 macros remain refused. These synthetic checks do not
validate nozzle clearance, Cartographer probing or physical homing behavior.

Centered-calibration fixtures check `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`
as the start/resume command, with both XY axes unhomed before it can move or
change workflow state. The first home tests the 0.5 g default candidate from
the operator's manual center; a clean run has one qualifying discovery and ten
validation homes. Early triggers increase the default candidate to 1.0 g,
release/invalidate XY and wait for manual recentering followed by the same
calibration command, preserving the pending candidate and attempt count.
Automatic recentering follows only accepted homes. TEST/CHARTS start directly
with `ACCEL_HOMING_TEST CENTERED=1` and
`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`, from an existing manual XY center
with both axes unhomed. They do not release XY before the first home. Missing
consent or a homed XY axis fails before movement or a workflow-state change.
These checks do not detect or prove physical centering.

Repeatability fixtures require **ten distinct consecutive geometry-qualified
validation homes** after the separate discovery at the same candidate, with
`max(travel_mm) - min(travel_mm) < 1.5 mm`. They cover equality rejection,
decimal endpoint subtraction across binary exponent boundaries, just-below
acceptance, either-direction outliers, discovery/manual-center exclusion,
independent X/Y windows and recorded range/limit metadata. The previously
reported ten-distance example spans 3.625 mm and is rejected. A range failure
aborts before another automatic return/home, candidate increase, model staging,
config write or SAVE_CONFIG dispatch. Existing early hits, including a result
below the broad geometry tolerance, retain retry behavior while clearing all
validation evidence and requiring a fresh discovery. The save-path fixtures
also reject incomplete, reused, foreign-axis/candidate/run or malformed windows
and recompute the range before writes. Legacy saved models remain accepted;
their old distances do not acquire the new qualification on loading.

Direct-command fixtures check that no separate centering-confirmation command
is registered and no `action:prompt_*` messages are emitted during successful
calibration/test/chart runs, early-trigger recentering, cancellation, timeout
or failure. Early calibration hits retain console instructions, the same
calibration resume command, candidate/attempt state and pending timeout.

Save-and-reload fixtures run the real named-model loader for both X and Y,
including a clean 0.5 g calibration and a 1.0 g calibration after an early trigger.
They check that the saved threshold and ten successes survive restart loading,
with the same sensor/geometry checks and no extra homing request. Both axes
reject saved-model thresholds below 0.5 g and nonfinite values; previous models
at 1 g and 2 g remain accepted. Calibration/model bounds remain 0.5–15 g.
Manual configuration and temporary SET commands accept finite positive values
without a fixed 3 g minimum or 15 g maximum; known sensor range remains limited
to 80% of full-scale and the actual session scale is rechecked before arming.
The `SAVE_CONFIG=1` default retains automatic write/restart dispatch.
`SAVE_CONFIG=0` fixtures check model staging without write/restart dispatch
and restoration of the original active thresholds; saving and reloading are
still required to activate that model. All save/restart operations in these
fixtures are simulated.

Manual-threshold fixtures add **11 tests** in
`test_manual_thresholds.ManualThresholdRangeTests`, with backend, current/legacy
section, X/Y, shared-fallback and both SET-command alias subcases. They accept
0.5 g and smaller positive values, including 0.001 g and the minimum positive
IEEE-754 double, and values above 15 g when the actual sensor range permits.
They reject zero, negative and nonfinite values; accept exactly 80% of a known
range and reject the immediately following representable value; recheck a
narrower active Beacon stream for SET and before arming; and reject raw-limit
overflow/underflow before dispatch without a new decimal floor. Unknown native
Beacon clip ranges keep their existing behavior. Manual SET/status feedback
retains 0.001 g without displaying zero. Two installer fixtures preserve
existing shared/X/Y values below 3 g through initial installation and update
in either section. The next-float fixture uses portable struct operations;
Python 3.8 runtime compatibility was not exercised.

The recorded run used CPython 3.13.5 on Windows: **427 tests in 6.837 seconds**.
The full UTF-8 unittest output is retained in `validation/tests.txt`; subprocess
text decoding normalized CRLF to LF before storage. The JSON report hashes the
exact stored log and all 18 Python sources. The package inventory contains
**33 files**, with **22 immutable bootstrap payload files**; no private user log
or raw hardware samples are included.

Motor-enable and fresh-reference checks include **26 tests** in
`test_session.EnablePreparationTest` (16) and
`test_backends.GenericPreparationTest` (10), with parameterized subcases. They
cover disabled, partly enabled and already enabled CoreXY motors; actual
completion of queued settling/backoff/retract before the cutoff; a fresh
reference for the 0.5 g candidate and every pass; exclusion of old/buffered
impacts; ringdown and noise on all sensor axes; and rejection of invalid,
stale, future, nonmonotonic, gapped, clipped or scale-changed references.
They also exercise bounded failure with persistent noise or no data,
long references independent of retained-batch capacity, cancellation during
queue waits and acquisition, Beacon clock conversion/wrap, pending-sample
freshness, preservation of an immediate real impact in a batch crossing
arming, and other accelerometer clients surviving success/failure cleanup.

The diagnostic fixtures exercise ADXL-style 3200 Hz timestamps rounded to
microseconds, a 100 ms reference with a 10-batch retained deque, and a separate
print-clock epoch. They distinguish acquisition timeout from expiry,
invalidation or a missing reference before arming. Retained summaries include
current and best quiet candidate counts/spans, X/Y/Z peak-to-peak variation in
g, reset/pre-cutoff/accepted counts, and sample/batch ages in ms. Ages are
snapshots at the reported gate, not live readings after cleanup. Native Beacon
fixtures verify machine-axis mapping, status-copy isolation and rejection of
stale oldest data even when the actual newest batch sample is fresh.

Configured baseline-floor checks add **13 tests** in
`test_backends.BaselineFloorTest`, with parameterized subcases. The finite
`baseline_noise_floor` accepts 0–0.2 g and defaults to 0.2 g for configured
batch backends. Their effective quiet limit is the larger of that floor and
20% of the active impact threshold. A zero floor restores the prior rule;
thresholds at least 1 g retain their prior limit. Native Beacon keeps its
original 20% gate and raw moving/pressed-start decisions, including the 0.1 g
quiet limit at a 0.5 g candidate. The configured floor does not raise the
impact threshold or reduce the required sample count, duration or freshness.

For a configured reference whose actual accepted variation exceeds the old
20% gate, movement proof uses a conservative lower bound. Each axis freezes
`b = max(abs(low - mean), abs(high - mean))`; it subtracts `N*b` from the
absolute sum of baseline-relative readings, clamps at zero, then uses the
existing physical velocity conversion and vector norm. One atomic snapshot
contains sums, sample count, sample interval and final sample clock; `N`
includes the first accumulated sample. Automatic impact direction requires
the corresponding signed velocity interval to clear +30 or -30 mm/s;
otherwise raw impacts remain bidirectional. No raw post-arm sample or
integral is filtered, and raw first-impact detection and startup deadlines
remain unchanged. References admitted by the old gate retain their old path.
The status summary exposes `baseline.motion_proof_noise_bound_g` when this
correction applies. Statistics use constant-size summaries.

A floor-only experiment reproduced false movement proof with a parked,
changing-mean signal. The final fixtures reject balanced, slow-block and
asymmetric-reference bias on one or three axes, using the maximum deviation
from the mean rather than half the peak-to-peak range. Actual synthetic host
stop requests meet the unchanged startup timeout plus one 3 ms watchdog
interval; the modeled 50 mm/s request is also below 3 mm without establishing
a default distance limit. True 50 mm/s ramps at
4000 mm/s² prove movement in both directions and across pseudo-clock wrap;
that ideal acceleration stays below the unchanged 0.5 g impact threshold.
Separate first-sample ±0.51 g contacts still trigger, including after uncertain
raw integrated speed exceeds 30 mm/s. A real 1 mm/s synthetic ramp with too
little evidence under the expanded noise envelope fails with `no_motion`;
no speed or acceleration is automatically changed. Repeated 0.21 g excursions
and stale, future or pre-cutoff-only data still abort before arming. Modeled
0.164 g ranges are constructed signals, not a replay of a user's raw data.

The noise envelope is an observation from preparation, not a guaranteed bound
on future disturbances. These fixtures establish software behavior only and
do not certify hardware noise, physical contact or stopping distance.

These fixtures advance fake host/print/sensor clocks and check ordering before
trigger dispatch is armed. They do not measure mechanical settling, qualify
the configured floor or native 20% rule on hardware, or prevent a driver's
first-step hold-to-run current transient. No physical homing or motor-current measurement was made.
The 0.5 g starting value does not establish a physical fix for skipped steps
or inconsistent homing. Commanded/firmware step travel is not an encoder
measurement. The default 20% center-to-stop geometry check and new fixed
less-than-1.5 mm validation range check do not establish precise physical
repeatability, correct-stop contact or absence of skipped steps.

`test_motion_policy.py` adds offline coverage for relative motion proof at low
speeds, preservation of legacy Beacon pressed-start behavior, generic
`no_motion` failures, startup deadline/budget calculations, and rejection of
plans above an explicitly configured 3 mm nominal limit before enabling/backoff.
Additional fixtures cover read-only requested-axis preflight from an unhomed
workflow, live limits lowered after configuration, invalid limits, zero and
nonzero plugin caps, and rejection before workflow effects. Diagnostics include
effective speed/acceleration and, when a travel policy is enabled, its speed
ceiling. With unchanged timing, 50 mm/s calculates about 3.233333 mm at live
1500 mm/s², compared with 2.65 mm at 5000 mm/s²; these distances do not create
a default admission limit. Repeated checks protect later preparation. Normal public G28 and
its safe Z wrapper remain unchanged; these tests do not claim a workflow
preflight can prevent that public wrapper's configured Z hop.
Those checks establish software behavior only; no physical stop travel or
end-to-end host/MCU latency was measured.

Optional policy coverage includes **10 tests** in
`test_motion_policy.ConfigurableStartupBudgetTests`, **9 tests** in
`test_motion_policy.OptionalTravelPolicyTests`, and **31 additional installer
and uninstaller tests**. Both travel settings default to absent/disabled.
Explicit `max_startup_travel` values must be finite and within 0.1–5.0 mm;
explicit `max_report_travel` values retain their 0.1–2.0 mm range. Each opt-in
is enforced independently, with strict `required <= configured` comparison.
An advanced example with 1.5 mm reporting and 4.5 mm startup limits admits a
75 mm/s plan at effective 5000 mm/s² with the original 20 ms sample-age and
5 ms report settings: nominal startup travel is 4.1625 mm and the unchanged
startup timeout is 60 ms. At live 1500 mm/s² that example needs 5.475 mm and
fails its explicit 4.5 mm policy. Without that opt-in, no distance policy
rejects the otherwise numerically valid plan.

Default 75 and 100 mm/s cases pass connect, workflow preflight and real
prepare/arm callbacks without editing speed, freshness, intervals or
acceleration. Real moving and parked traces verify the existing movement
proof and `no_motion` deadline. Missing, stale and frozen streams still
request failure. Absent-policy status uses JSON null and console text
`disabled`, including replay fallbacks with no prepared effective speed.
Finite-positive parameters and finite derived startup timeout, nominal
travel and homing deadline remain mandatory; numeric extremes cannot leak
infinity/NaN into status or create an unbounded deadline. The test harness
returns an absent numeric default of `None` only when the option is absent;
explicit invalid `None` values are not treated as an opt-out.

Installer fixtures exercise the X=75/Y=50 target without budget flags,
default preservation, explicit `--max-report-travel`/`--max-startup-travel`
opt-ins, selected-axis-only speed edits, invalid-value and native-Beacon
refusals, and pre-write validation of retained plugin axes affected by shared
policy edits. Retained active second-pass speeds, retract/rehome distances
and finite positive rail acceleration overrides are checked. Selected rails
use their planned single-pass settings after installer-owned overrides are
removed; unselected rails keep their settings. Uninstall validates owned
opt-in values while retaining later-edit and unrelated-setting protections.

The workflow source is byte-identical to the public source base: the strict
**max-minus-min travel <1.5 mm across ten qualified validation homes** remains
separate for each calibrated axis. This is a repeatability rule, not an
impact threshold, latency distance or physical stopping guarantee. Optional
travel limits describe nominal command travel before a host request; their
engineering ranges do not certify hardware safety. No current printer
settings were changed and no private user log or raw measurements were
included in this package.

All **18 production and test Python files** listed in
`validation/VALIDATION.json` passed Python 3.8
grammar parsing on the current Python 3.13 host. No Python 3.8 runtime was
executed; syntax parsing alone does not establish complete runtime compatibility.
