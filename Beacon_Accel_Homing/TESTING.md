# Offline verification

All verification for this rewrite uses synthetic Klipper objects, fake sensor
batches, temporary config files and mocked status/download responses. No
printer connection, SSH session, deployment, service restart, physical home,
calibration or hardware measurement was performed.

Run from the repository root with Python 3:

```bash
python -m unittest discover -s Beacon_Accel_Homing/tests -v
bash -n Beacon_Accel_Homing/install.sh
bash -n Beacon_Accel_Homing/bootstrap.sh
bash -n Beacon_Accel_Homing/uninstall.sh
```

Focused installer/bootstrap/uninstall check:

```bash
python -m unittest discover -s Beacon_Accel_Homing/tests -p test_installer.py -v
```

Focused motor-enable and fresh-reference fixtures, run from
`Beacon_Accel_Homing/tests/`:

```bash
python -m unittest test_session.EnablePreparationTest test_backends.GenericPreparationTest -v
```

The recorded combined run includes **73 installer/bootstrap/uninstall tests:
72 passed and one real POSIX symlink test was skipped** because the Windows
host does not permit symlink creation. Temporary regular-file fixtures cover
installation, config idempotence, backup/rollback, full-history restore,
failed-write recovery, concurrent-edit rejection and later-edit protection.
Full restore/link-history checks run with isolated link-state emulation.
Actual symlink creation must still be exercised on a Linux host before
claiming a host integration test. All three shell entry points (`install.sh`,
`bootstrap.sh` and `uninstall.sh`) passed Git Bash `bash -n`. No native Linux
systemd/Unix-socket integration run was performed here.

The combined offline verification run discovered **273 tests: 272 passed,
zero failures/errors, and the same one Windows symlink skip**. The portable
machine-readable report and complete referenced log are together under
[`validation/VALIDATION.json`](validation/VALIDATION.json) and
[`validation/tests.txt`](validation/tests.txt).

Installer fixtures cover main/include discovery, stopped/running/multiple
instances, process command and service parsing, configured sensor selection,
mixed-case names, generic speed limits, legacy Beacon settings, wildcard include
deduplication, unchanged saved blocks/macros, required-file conflicts, custom
G28/safe_z_home refusal, read-only dry run, wrong-socket identity, printing
refusal, immutable-ref downloads, pinned Git blob integrity, valid-Python-prefix
truncation, shell invocation truncation, cache integrity, and aborted downloads.
Publication fixtures also execute both bootstrap entry points with mocked
downloads to verify their FrankRamm testing-fork defaults, immutable revision
pinning and forwarded installer arguments, and verify that the real source
package retains its GPLv3 license and original CloakedWayne credit/provenance.
Low-acceleration fixtures verify automatic speed reduction using the exact
runtime startup budget, explicit invalid-speed refusal before writes, plugin
acceleration limits including the zero override, and toolhead velocity limits.

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
models, explicit manual-centering confirmation, stock G28 requirements,
suspect travel without automatic returns, and measured chart export. Synthetic
chart fixtures are explicitly marked as simulated.

Centered-calibration fixtures check `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`
as the start/resume command, with both XY axes unhomed before it can move or
change workflow state. The first home tests the 2 g default candidate from
the operator's manual center; a clean run has one qualifying discovery and ten
validation homes. Early triggers increase the default candidate to 2.5 g,
release/invalidate XY and wait for manual recentering followed by the same
calibration command, preserving the pending candidate and attempt count.
Automatic recentering follows only
accepted homes. TEST/CHARTS retain their argument-free prompt starts and
`ACCEL_HOMING_CONFIRM CENTERED=1` response; that response cannot continue
calibration. These checks do not detect or prove physical centering.

Save-and-reload fixtures run the real named-model loader for both X and Y,
including a clean 2 g calibration and a 2.5 g calibration after an early trigger.
They check that the saved threshold and ten successes survive restart loading,
with the same sensor/geometry checks and no extra homing request.

Motor-enable and fresh-reference checks add **19 tests** in
`test_session.EnablePreparationTest` (13) and
`test_backends.GenericPreparationTest` (6), with parameterized subcases. They
cover disabled, partly enabled and already enabled CoreXY motors; actual
completion of queued settling/backoff/retract before the cutoff; a fresh
reference for the 2 g candidate and every pass; exclusion of old/buffered
impacts; ringdown and noise on all sensor axes; and rejection of invalid,
stale, future, nonmonotonic, gapped, clipped or scale-changed references.
They also exercise bounded failure with persistent noise or no data,
long references independent of retained-batch capacity, cancellation during
queue waits and acquisition, Beacon clock conversion/wrap, pending-sample
freshness, preservation of an immediate real impact in a batch crossing
arming, and other accelerometer clients surviving success/failure cleanup.

These fixtures advance fake host/print/sensor clocks and check ordering before
trigger dispatch is armed. They do not measure mechanical settling, validate
the 20% quiet limit on hardware, or prevent a driver's first-step hold-to-run
current transient. No physical homing or motor-current measurement was made.

`test_motion_policy.py` adds offline coverage for relative motion proof at low
speeds, preservation of legacy Beacon pressed-start behavior, generic
`no_motion` failures, startup deadline/budget calculations, and rejection of
plans above the fixed 3 mm nominal command budget before enabling/backoff.
Those checks establish software behavior only; no physical stop travel or
end-to-end host/MCU latency was measured.

All plugin Python source and test files passed Python 3.8 grammar parsing on
the current Python 3.13 host. No Python 3.8 runtime was executed; syntax
parsing alone does not establish complete runtime compatibility.
