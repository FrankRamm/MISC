# Offline verification

All verification for this rewrite uses synthetic Klipper objects, fake sensor
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

The recorded combined run includes **100 installer/bootstrap/uninstall tests:
99 passed and one real POSIX symlink test was skipped** because the Windows
host does not permit symlink creation. Temporary regular-file fixtures cover
installation, config idempotence, backup/rollback, full-history restore,
failed-write recovery, concurrent-edit rejection and later-edit protection.
Full restore/link-history checks run with isolated link-state emulation.
Actual symlink creation must still be exercised on a Linux host before
claiming a host integration test. Shell entry points are unchanged by this
calibration/activation change, so their syntax checks were not repeated. No native
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
The combined offline verification run discovered **351 tests: 350 passed,
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
refusal of incompatible generic plans before writes, using the exact runtime
startup budget. Explicit `--homing-speed` edits remain opt-in and must satisfy
the same limits. Low-acceleration cases include nonzero and zero plugin caps
and toolhead velocity limits; no automatic speed reduction is expected.
The reporting ceiling uses `max_report_travel/max_sample_age`, without an
independent fixed 50 mm/s cap. Defaults still give 50 mm/s; higher-speed
fixtures must satisfy both that reporting ceiling and the unchanged 3 mm
startup budget with explicit settings. No timing/acceleration is auto-edited,
and updating does not automatically recover an earlier speed from backups.
Admitted 75 mm/s fixtures exercise both directions through fresh-reference
preparation, arming and watchdog handling. Moving traces establish motion;
parked noisy traces still fail with `no_motion` and request a host stop within
the nominal 3 mm budget. Their constructed delivery timing is synthetic and
does not qualify a sensor, MCU or host for those settings on hardware.

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
reject thresholds below 0.5 g and nonfinite values; previous models at 1 g and
2 g remain accepted. Manual threshold and temporary SET limits remain 3–15 g.
The `SAVE_CONFIG=1` default retains automatic write/restart dispatch.
`SAVE_CONFIG=0` fixtures check model staging without write/restart dispatch
and restoration of the original active thresholds; saving and reloading are
still required to activate that model. All save/restart operations in these
fixtures are simulated.

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

The new baseline fixtures preserve the existing quiet/freshness gates. ADXL-style
3200 Hz timestamps rounded to microseconds acquire a 100 ms reference with a
10-batch retained deque and a separate print-clock epoch. Synthetic failures
exercise repeated sub-100 ms quiet candidates, absent data, only pre-cutoff
data, and a ready reference that expires before arming. Retained error/status
summaries distinguish acquisition timeout from before-arm expiry, invalidation
or a missing reference. They include current and best quiet candidate counts
and spans, all-axis peak-to-peak values in g, reset/pre-cutoff/accepted counts,
and sample/batch ages in ms. Those ages are snapshots at the reported gate,
not a live sensor reading after cleanup. Native Beacon fixtures also verify
machine-axis mapping, status-copy isolation, and rejection of a batch with a
stale oldest sample even when its actual newest sample is fresh. Statistics
use constant-size summaries; these diagnostics retain no raw sample stream.
The supplied failure log contains no such baseline statistics and does not
establish a physical noise, sensor-latency or current-transition cause.

These fixtures advance fake host/print/sensor clocks and check ordering before
trigger dispatch is armed. They do not measure mechanical settling, validate
the 20% quiet limit on hardware, or prevent a driver's first-step hold-to-run
current transient. No physical homing or motor-current measurement was made.
The 0.5 g starting value does not establish a physical fix for skipped steps
or inconsistent homing. Commanded/firmware step travel is not an encoder
measurement. The default 20% center-to-stop geometry check and new fixed
less-than-1.5 mm validation range check do not establish precise physical
repeatability, correct-stop contact or absence of skipped steps.

`test_motion_policy.py` adds offline coverage for relative motion proof at low
speeds, preservation of legacy Beacon pressed-start behavior, generic
`no_motion` failures, startup deadline/budget calculations, and rejection of
plans above the fixed 3 mm nominal command budget before enabling/backoff.
Additional fixtures cover read-only requested-axis preflight from an unhomed
workflow, live limits lowered after configuration, invalid limits, zero and
nonzero plugin caps, and rejection before workflow effects. Diagnostics include
effective speed/acceleration and a lower speed ceiling: with default timing,
50 mm/s needs about 3.233333 mm at live 1500 mm/s², compared with 2.65 mm at
5000 mm/s². Repeated checks protect later preparation. Normal public G28 and
its safe Z wrapper remain unchanged; these tests do not claim a workflow
preflight can prevent that public wrapper's configured Z hop.
Those checks establish software behavior only; no physical stop travel or
end-to-end host/MCU latency was measured.

The changed production and test Python files listed in
`validation/VALIDATION.json` passed Python 3.8
grammar parsing on the current Python 3.13 host. No Python 3.8 runtime was
executed; syntax parsing alone does not establish complete runtime compatibility.
