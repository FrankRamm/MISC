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
```

Focused installer/bootstrap check:

```bash
python -m unittest discover -s Beacon_Accel_Homing/tests -p test_installer.py -v
```

The focused publication run discovered 43 tests: 42 passed and one real POSIX
symlink test was skipped because the Windows host does not permit symlink
creation. Actual temporary regular-file installation, config idempotence,
backup/rollback, failed-write recovery, concurrent-edit rejection and later-edit
protection passed. The symlink operation must still be exercised on a Linux
host before claiming a host integration test. `bash -n` passed for both entry
points using Git Bash. No native Linux systemd/Unix-socket integration run was
performed here.

The combined offline publication run discovered **212 tests: 211 passed,
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

Core/backend and workflow suites separately cover trigger/session behavior,
mapped generic data and units, timing/rate/error guards, staged/independent
models, confirmation tokens, stock G28 requirements, ten validation repeats,
suspect travel without blind returns, and measured chart export. Synthetic
chart fixtures are explicitly marked as simulated.

`test_motion_policy.py` adds offline coverage for relative motion proof at low
speeds, preservation of legacy Beacon pressed-start behavior, generic
`no_motion` failures, startup deadline/budget calculations, and rejection of
plans above the fixed 3 mm nominal command budget before enabling/backoff.
Those checks establish software behavior only; no physical stop travel or
end-to-end host/MCU latency was measured.
