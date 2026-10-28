# Compatibility evidence and limits

## Current simple-calibration exception

[Simple calibration](SIMPLE_CALIBRATION.md) replaces the selected-axis velocity-
proof workflow for CALIBRATE and its newly saved vector models. It requires
a discovery impact after >20 mm using verified stock single-axis G28, then
ten impact measurements from the selected axis's configured midpoint. Merely
crossing 20 mm never qualifies a candidate. Each impact beyond 20 mm must fit
the configured midpoint-to-endstop distance within the existing tolerance
(default 20%). A >2 mm running spread immediately raises the candidate and
restarts discovery; passing still requires ten checks. An implausible >20 mm
hit aborts without automatic recentering. Old models/manual overrides retain the
legacy algorithms described below. Regression sources cover both paths; neither
commands nor step counts establish actual physical position.

Historical offline verification is recorded in [TESTING.md](../TESTING.md).
The current calibration validation/return changes and the new built-in
`homing_override` support were reviewed from source without execution.
No new sensor, printer, homing impact,
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
| [Klipper homing_override](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/extras/homing_override.py), [Kalico homing_override](https://github.com/KalicoCrew/kalico/blob/0057b6a153170807e99080360c1ef15a2078f672/klippy/extras/homing_override.py) | Built-in `HomingOverride.cmd_G28` retains `prev_G28` and uses `in_script` to guard recursive script calls |

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

Installing or updating Python code/module links automatically restarts the
verified systemd service for the selected Klipper instance, after successful
installation and a fresh identity/idle check. The installer obtains any
needed sudo privilege before writing files. `--no-restart` opts out; use it
for a service manager the installer cannot verify and restart that host
process manually while idle. Full uninstall and explicit rollback also leave
the host-process restart to the operator. See
[the activation contract](INSTALLATION.md#automatic-host-service-restart).

Console `RESTART` and `FIRMWARE_RESTART` recreate printer objects within the
same Python process. Extras use normal cached imports, so those commands do
not reload already imported modules after a source/link change. This follows
the inspected [Klipper import path](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/klippy.py#L87-L110),
[Klipper restart loop](https://github.com/Klipper3d/klipper/blob/ee2c6fb6b00b2b5d097ef70d0868df7d74943b51/klippy/klippy.py#L343-L359)
and [Kalico restart loop](https://github.com/KalicoCrew/kalico/blob/0057b6a153170807e99080360c1ef15a2078f672/klippy/printer.py#L632-L648).
Configuration-only threshold edits can still use console `RESTART`, and
calibration `SAVE_CONFIG` retains its normal configuration restart.

## Moonraker update transport

The initial short command buffers the complete successful `curl` response
before Bash executes it. A final completeness marker is checked from the
command string or completed file before the Python bootstrap starts. Direct
unbuffered `curl | bash` is unsupported. The existing bootstrap still resolves
one immutable commit and verifies the complete payload's Git blob sizes/hashes.

Native updates require a dedicated clean Git checkout whose module files are
the actual symlink targets. An immutable snapshot alone cannot provide that.
The installer registers the correlated Moonraker instance with `type: git_repo`,
`channel: dev`, the exact origin and `primary_branch: codex/universal-accel-homing`.
`managed_services: klipper` is the supported alias, resolved by Moonraker to
its associated Klippy unit; arbitrary numbered unit names are not substituted.
Current Mainsail/Fluidd can expose commit-based dev updates without a repository
tag, while retaining their normal UI confirmation and service permissions; see
the inspected [Mainsail update entry](https://github.com/mainsail-crew/mainsail/blob/03a4d7b1d70b6df1061c8e1d998620320c126627/src/components/panels/Machine/UpdatePanel/Entry.vue)
and [Fluidd version status](https://github.com/fluidd-core/fluidd/blob/738ff932030588adb65684f49be4db893ad03216/src/components/settings/VersionStatus.vue).

This follows inspected Moonraker
[configuration](https://github.com/Arksine/moonraker/blob/b559b9a81161f958a43c2f527b6ad96f73310d5d/docs/configuration.md)
and [application updater](https://github.com/Arksine/moonraker/blob/b559b9a81161f958a43c2f527b6ad96f73310d5d/moonraker/components/update_manager/app_deploy.py)
contracts: `install_script` is a deprecated dependency-text parser and is not
executed. The generated updater therefore has no installer hook or shared
Klipper `env`/requirements override. Normal code updates retain the ignored,
private PNG dependency cache. Hard Recovery may replace that cache; rerun the
installer afterward. Releases requiring installer/config/dependency changes
also need that explicit installer step.

Initial source anchoring, ownership-aware migration and Moonraker configuration
changes share the installer transaction and selected-instance checks. The first
updater activation restarts the verified Moonraker service automatically when
needed; `--no-restart` opts out. Future updates use Moonraker's native Git/service
path. These contracts have offline fixtures; no live updater, service restart
or physical printer was exercised here.

## safe_z_home and X/Y-only workflows

The standard Klipper/Kalico `[safe_z_home]` and `[homing_override]` are supported
without the installer's `--acknowledge-homing-override` flag. They cannot coexist
in one configuration. The installer preserves the selected section and its
settings; that alone does not certify the running handler chain. Cartographer's
[Klipper setup guide](https://docs.cartographer3d.com/cartographer-probe/installation-and-setup/software-configuration/klipper-setup)
recommends `safe_z_home` for homing Z at a suitable bed position.
The installer preserves existing probe/Z settings.

Even `G28 X` sent to the standard safe Z handler can run its configured
`z_hop` before checking which axes were requested. CALIBRATE, TEST, CHARTS and ACCURACY
therefore verify that the active `G28` is either stock or the recognized
built-in `SafeZHoming.cmd_G28` or `HomingOverride.cmd_G28` whose `prev_G28` is
stock. The override must not already be running its script (`in_script` must
be exactly false). They call the verified stock handler directly for the
requested X or Y axis only, rechecking the path
before every trial. Exact built-in class/method identities and both active and
ready G28 registrations must agree.
This bypass is limited to workflow homes; it neither replaces the registered
`G28` command nor changes normal safe Z homing, Cartographer probing, saved Z
data or the wrapper's settings. Ordinary `G28 X/Y` retains any configured safe
Z hop. Normal `G28` also retains the complete configured homing-override sequence,
including `SYNC_MOTORS` and Beacon Z actions in their existing order.

Workflow homes skip all override G-code and its forced `set_position_*` setup:
current/enable hooks, `SYNC_MOTORS`, Z moves and Beacon probing are not run.
The operator must establish suitable XY test currents/enable behavior and
nozzle/bed clearance independently. Direct stock homing retains its normal
motor-enable lifecycle; custom preparation macros are not inferred or replayed.

G28 macros, unrecognized/replaced handlers and nested wrapper chains remain
unsupported by workflows, including a custom handler above or below either
built-in wrapper.
Validation fails before the affected homing attempt, including when
installation was manually acknowledged. `--acknowledge-homing-override` applies
only to reviewed `gcode_macro G28` installation; it does not enable that path
for workflows. The new override support follows inspected source contracts
and unexecuted regression source; no physical path was tested for this fix.
The independent-Z and other workflow restrictions still apply.
Before manual centering and these X/Y-only workflows, the operator must ensure
nozzle/bed clearance for XY travel because the safe Z hop will not run.

## Pre-motion stationary preparation

Motor enable and current setup can produce an acceleration transient that
resembles a stop impact. Both Beacon and generic paths therefore acquire a
new fresh reference before every homing pass, including normal `G28`, workflow
homes, already enabled motors and second touches. Ordinary homing, TEST,
CHARTS and ACCURACY additionally require its peak-to-peak range to be quiet.
Active calibration trials skip that range requirement as described below.

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
   contiguous samples spanning `baseline_time` (default 0.1 s). For native
   Beacon, peak-to-peak variation on each of the three sensor axes must not
   exceed `max(0.20 * impact_threshold, min(0.5 g, impact_threshold))` when
   quietness is required. The floor is bounded by 0.5 g and the actual
   threshold; it is not a user setting. Configured generic accelerometers use
   `max(0.20 * impact_threshold, baseline_noise_floor)` on each sensor axis.
   Calibration still requires the full fresh interval but does not reject it
   for exceeding either range rule.
4. Freeze the validated reference before `home_start` arms trigger dispatch.
   The first valid impact at or after arming remains eligible, including
   samples delivered in the batch that crosses the arming boundary. There is
   no post-arm blanking interval or minimum-travel impact filter.

`baseline_noise_floor` is in g, defaults to 0.7, and accepts finite values
from 0 to 0.7 inclusive. Zero restores the previous 20% limit. It affects only
configured generic accelerometers, including Cartographer's separately
configured ADXL/LIS object outside calibration. At a 0.5 g or 1 g homing
threshold, the default stationary variation limit is 0.7 g; the impact
threshold stays selected. An explicit older `baseline_noise_floor` still takes precedence;
set it to 0.7 to use this allowance on an existing configuration. The required
reference duration, sample count and timestamp limits remain unchanged.
Generic references above the original 20% limit retain the measured-noise
envelope in movement proof, which can still fail closed with `no_motion`.
Native Beacon independently uses its bounded 0.5 g floor outside calibration. At a 0.5 g threshold
its stationary variation limit is 0.5 g and its raw impact threshold remains
0.5 g. Manual thresholds below 0.5 g cap the floor at the actual threshold;
they remain finite-positive settings without a new minimum. Native thresholds
at least 2.5 g retain their previous quiet limit.
References admitted only by the wider limit use the motion checks below.

The calibration exception is derived from the current workflow context:
mode `calibration`, state `running`, the matching selected axis, and an active
`_home` call for that same axis. It is not a global switch and is not active
while awaiting manual centering or after cleanup. The reference retains the
actual XYZ minimum, maximum and mean over all samples in its full rolling
duration. It always supplies the measured maximum deviations to movement proof,
even if a later, higher impact threshold would otherwise disable that envelope.
High noise can still produce `no_motion`; it cannot establish motion or justify
another threshold increment by itself. The `quiet_range_enforced` status field
and diagnostic text distinguish this fresh reference from a quiet-qualified one.

Acquisition has a `baseline_time + 0.5 s` deadline measured after queued setup
and settling finish. No sample flow fails with `no_baseline` before trigger
dispatch is armed. Persistent vibration can also cause that failure outside
calibration. Stale, future, nonmonotonic,
gapped, clipped, saturated or scale-changed data also aborts preparation.
The reference uses a bounded rolling interval. When quietness is required, a range violation removes only
the oldest prefix needed to leave a contiguous suffix within the XYZ limits.
Older samples are otherwise removed only when the remaining samples still
span the complete duration and include at least 50 samples. The sample at or
just before the duration boundary is retained, including its extrema. The
mean and motion-proof noise envelope use exactly the same retained samples.
New data can invalidate readiness before arming; a prior ready window is not
latched. No in-window outlier is ignored or averaged out of the range check.

Storage is capped at 65,536 samples. If the required interval cannot fit, the
acquisition fails with `baseline_overflow` instead of silently evicting data
or permitting a shorter interval. `baseline_batches` does not determine
whether this interval is long enough.

Before acquisition begins, the generic warmup path drains expired buffered
data. Timestamp continuity and pending data discarded by that age cutoff are
discarded together; the first retained sample must not be compared against an
obsolete timestamp. Gaps between retained samples and all gaps during actual
baseline acquisition, stationary-noise capture or an armed move remain fatal.

The quiet limits are engineering policies checked by synthetic fixtures;
they are not physically validated boundaries between vibration and contact.
These checks cannot exclude every false home. A driver's transition from hold
current to run current on the first step occurs after the stationary check.
Klipper documents that current changes can themselves move a motor in its
[`hold_current` guidance](https://www.klipper3d.org/TMC_Drivers.html#prefer-to-not-specify-a-hold_current).
This preparation does not change TMC currents, modes or other driver settings.

## Final release after successful homing

The installed X/Y `homing_retract_dist: 0` setting retains one touch. The
plugin's `back_off_dist` (default 5 mm; finite 0–20 mm; zero disables it)
controls its existing cross-axis release and the final release after a
successful ordinary home. It does not enable a second touch. Configured
homing/retract speeds, firmware acceleration limits and driver currents are
preserved.

The final release wraps the kinematics `home` call synchronously and waits
until it returns, after firmware homing-coordinate corrections. It leaves the
public `G28` handler and safe Z/override registration unchanged. Each underlying
homing call is handled separately: a safe Z wrapper can still position
XY after that call's release, so the final public `G28` position need not be
5 mm from a stop. Release applies only to axes completed by that invocation
and still associated with its current
successful detector record. A failed home, shutdown, nested/untrusted home or
stale record does not receive this move. The target is the configured endstop
position plus the release distance away from the stop, bounded by the rail's
minimum/maximum. The rail retract speed is used, with homing speed as fallback.
The existing `homing_accel` limit temporarily caps the release acceleration;
it does not raise a lower live value. Normal acceleration is restored after
the move completes or fails.
An equal or larger firmware release is not repeated; the move never returns
toward the stop to enforce an exact distance. The release does not include Z.

Supervised trial homes suppress the ordinary release. Accepted intermediate
trials keep their existing tested-axis return to center. CALIBRATE releases
its final tested axis only after geometry and all ten repeatability validations,
before model staging or `SAVE_CONFIG`. TEST/CHARTS release their final Y only
after both axes pass quality checks and any per-home charts are exported.
ACCURACY validates the final accepted home and all required per-home captures,
then releases its final tested axis before writing the aggregate summary.
A later aggregate-export failure marks ACCURACY aborted while preserving
any published final summary and the preceding trusted release. Its temporary
per-home PNGs are removed on success, abort or cancellation. Rejected, cancelled or
failed trial data never requests a terminal release. Stationary NOISE performs
no release or other motion. These paths are verified with synthetic objects;
no physical clearance, stop accuracy or skipped-step result is established.

## Stationary accelerometer noise measurement

`ACCEL_HOMING_CALIBRATE_NOISE` acquires three disjoint one-second windows of
stationary, machine-mapped XYZ acceleration in g. This is a vibration
measurement, not a sound-level measurement or a noise filter. It requires an
idle, unpaused printer, an empty verified motion queue and an exclusive sensor
stream. It does not require homed axes, manual centering or `CENTERED=1` and
does not change motor enable/current, position, fan state, active thresholds
or saved models. No homing, model save or restart follows automatically.
The command rejects `SAVE_CONFIG`.

Native Beacon raw data and configured generic sensors use their existing
mapping, scale and stream contracts. Each window excludes earlier buffered
data and requires at least 50 fresh, increasing samples spanning one second.
Stale/future/gapped/frozen/malformed/saturated data, changed sensor identity,
mapping/scale/range, concurrent clients, queue activity, cancellation or
disconnect aborts the measurement. Generic reporting changes are restored and
only the measurement's sensor subscription is stopped. There is no hidden
motor-enable or motion test in this command.

For configured batch sensors, the active noise cutoff is applied before
freshness or sequence validation, including any pending singleton sample.
Rows at or before that cutoff cannot abort a later window merely by becoming
old during batch processing. Between-window callbacks drain the stream and
reset the pending timestamp sequence; an active window never uses the
permissive homing-startup age trim to discard late eligible samples. Freshness
and continuity failures for eligible samples still abort the measurement.

The first timing rejection is retained separately as `sample_timing_fault`
in the owner status and as `timing_fault` in an aborted noise result. It
identifies the checking phase, noise-window index, cutoff, sample timestamps,
signed ages, limits and whether the rejected timestamp was old or future.
The live summary survives cleanup and is cleared when a new stream starts;
it is independent of any retained homing-baseline summary.

Each window's own XYZ mean is removed. A single PNG contains all three signed
XYZ plots on a common vertical scale, absolute peaks, RMS and starting
candidates. Exact duration, timestamps, count, sensor/scale/map context,
absolute peak, RMS and peak-to-peak values remain in memory/status. The image
uses a bounded envelope that retains positive and negative extrema; it does
not export every raw sample. Intermediate windows remain in memory and create
no files.
Run/iteration identity and disjoint timestamps are checked. A successful
summary requires all three complete valid window receipts and a completed
PNG export. Failed/partial runs may create an explicitly incomplete PNG when
render/context checks permit and cannot recommend a candidate. PNG publication is
atomic and never overwrites an earlier output. Descriptive filenames and
image titles share the host's local `YYYY-MM-DD HH:MM:SS` timestamp;
PNG metadata retains the timezone offset and UTC instant. Colons in the Linux
filenames require renaming for Windows downloads. Bounded collision suffixes
distinguish coincident clocks. No user-facing run
UUID, chart sidecars or private printer configuration/logs are exported.

All chart producers use PNG only, including ordinary capture exports,
ACCURACY overviews and failure reports. ACCURACY's per-home PNGs live in a private
run-specific OS temporary directory outside the chart/config watched roots;
only the final PNG per requested axis is published to the chart directory. Cleanup removes its
temporary directory after final publication and on abort/cancel, without deleting
existing charts. Temporary paths are omitted from public status/results.
Per-home PNG references carry private
immutable in-memory receipts binding run/axis/attempt, exact record, image
path/hash, sample count/baseline and a bounded trace. ACCURACY rechecks receipt
identity, record changes and PNG bytes/CRC with reactor yields between files;
copied plain path dictionaries cannot establish provenance. Per-axis overview
dimensions remain fixed for the maximum 100 measured homes. No raw data is
reconstructed from image pixels or a sidecar file.

Impact capture validation additionally requires exact original trigger-packet
and sample-index provenance identifying one retained onset row. The copied
report uses that row's exact timestamp and preserves the original trigger time
separately. Missing, altered or ambiguous proof rejects the capture with bounded
numeric diagnostics; no time tolerance or synthetic row is used to make it pass.
This correction and its regression source were reviewed without execution.

Matplotlib/NumPy/Pillow are loaded only by an isolated worker using the selected
Klipper Python and a private dependency target keyed by Python ABI and venv
prefix. The installer verifies or prepares that target without changing global
pins. The host-only `.plot-deps/` cache is ignored by Git and retained on
uninstall. Klippy imports only standard-library chart orchestration: it polls
the child through the reactor, checks cancellation and a finite deadline, and
cleans private OS-temporary input/output/cache/log files. The PNG output
directory contains only completed PNG files. Child pixel validation and parent
bounded PNG structure/CRC/hash checks precede publication. A real small worker
render is required before a chart-producing workflow moves or measures noise.
Rendering/preflight does not query the printer or perform motion.

Inline display uses a file URL only when the current Moonraker process/data
directory and Klippy socket can be correlated. A direct image GET needs a
same-origin Moonraker route accessible to an ordinary browser request, such
as a trusted LAN or session-cookie route; it does not inherit JavaScript
Authorization headers. Remote frontends or header-token-only authentication
may require the frontend file browser. No tokens are embedded in image links.
Otherwise the console/STATUS provides the saved PNG path; this preview advisory
does not itself abort ACCURACY or invalidate captures.
Formatted console support varies; Mainsail raw-output mode displays plain text.

Runtime discovery follows Moonraker's
[path defaults](https://github.com/Arksine/moonraker/blob/b559b9a81161f958a43c2f527b6ad96f73310d5d/moonraker/server.py):
explicit data/config arguments override `MOONRAKER_DATA_PATH` and
`MOONRAKER_CONFIG_PATH`; absent/empty data uses the process's `~/printer_data`.
Home expansion uses its `HOME` or same-UID passwd home, and relative data/config
arguments use its working directory. Missing config uses `data/config/moonraker.conf`.
An omitted `klippy_uds_address` uses Moonraker's
[`/tmp/klippy_uds` default](https://github.com/Arksine/moonraker/blob/b559b9a81161f958a43c2f527b6ad96f73310d5d/moonraker/components/klippy_connection.py), never a guessed
`data/comms` socket. Only the three allowlisted path environment values are
retained after UID/entrypoint checks, with bounded reads. A mandatory `[server]`,
exact Klippy socket and one matching process establish `data/config` as the root;
two matching PIDs are ambiguous even if their roots agree. Unresolved includes,
templates, relative socket values and unsupported compact/abbreviated path
arguments remain unmapped. Main `printer.cfg`
containment does not establish the root. This correction has source review only;
the affected host's warning cause and current preview route were not verified.

For each X/Y component in the standalone diagnostic, let `peak` be its largest
absolute mean-removed deviation across all three windows. The initial candidate
is the smallest configured
`calibration_start_threshold + n * calibration_step` value at least
`max(calibration_start_threshold, 1.2 * peak, peak + 0.1 g)`. Thus a 2 g peak
gives 2.5 g with the default 1 g start and 0.25 g step. Exact measured peaks and the
upward-rounded candidates remain separate fields. The candidate must not
exceed `min(calibration_max_threshold, 15 g, 0.8 * sensor_full_scale_g)`.
An over-ceiling candidate is unavailable rather than clamped downward.
An unknown physical sensor range also prevents a candidate; either case still
exports all three valid measured windows and their statistics.

These are **initial calibration candidates, NOT calibrated impact thresholds**.
Standalone X/Y component hints remain diagnostic; a later calibration does not
consume them. Each new `ACCEL_HOMING_CALIBRATE AXIS=X|Y CENTERED=1` invocation
requires its own three-window stationary measurement and completed summary
export before its first movement. Candidate retries and same-run continuations
do not repeat that measurement. Saving X normally restarts Klipper; the new Y
command measures again automatically. Measurement/context/export failures
abort before movement, with no fallback to a configured initial value.

For automatic selection, each window removes its own full XYZ mean and measures
`sqrt(dx^2 + dy^2 + dz^2)` for every sample. Let `peak` be the largest such
**magnitude** across all three windows. The required value is
`max(calibration_start_threshold, 1.2 * peak, peak + 0.1 g)` and is rounded
upward on `calibration_start_threshold + n * STEP`. An explicit `STEP` supplies
the grid spacing; otherwise `calibration_step` is used. This matches the simple
calibration detector rather than using a mapped X or Y component hint.
Without an explicit `START_THRESHOLD`, an unknown range or a candidate above
`min(calibration_max_threshold, 15 g, 0.8 * sensor_full_scale_g)` aborts before
movement; it is never clamped downward or taken from an older run.
An explicit `START_THRESHOLD` retains its validated initial value even when
noise suggests another value, but measurement and summary export still must
succeed. These parameters remain fixed during the run. Noise acquisition alone
does not change manual thresholds or saved models.
See the [current calibration sequence](SIMPLE_CALIBRATION.md) for threshold
selection, automatic early-impact returns and ten-check qualification.

Stationary vibration in the current motor/fan state does not predict motor
enable, hold-to-run-current or first-step transients. It does not certify
contact detection or guarantee that noise will be filtered. Calibration still
requires a fresh offset reference for every move. Neither the initial noise
measurement nor the XYZ-magnitude detector adds a quiet-range gate or
acceleration-derived proof of movement. Legacy selected-axis models/manual
overrides retain their quiet-reference and movement-proof path. Raw impact
eligibility, freshness/fault gates and timing limits remain active. The
noise-based initial value is a heuristic, not a certified minimum.

## Centered calibration

Start with `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1`, or `AXIS=Y`
for Y. Before that command, the operator physically centers both XY axes by
hand using motor controls appropriate for the machine, with Z supported.
Both XY axes must be unhomed in Klipper. `CENTERED=1` declares that this manual
preparation is complete and authorizes movement; it is not a sensor position
measurement. The workflow does not assign a center coordinate or mark either
axis homed before the actual home. No further confirmation is requested; the
first homing pass starts after stationary preparation succeeds.

The first home tests the candidate selected from the fresh noise measurement,
or the explicit `START_THRESHOLD` value.
Calibration and saved models accept finite thresholds from 0.5 to 15 g, with
the configured calibration ceiling and sensor-range limit still applied.
The default step is 0.25 g; each early-trigger retry adds that selected step.
`START_THRESHOLD=1 STEP=0.25` selects those values explicitly for a run and
still performs the three-window measurement before movement. Supplying only
`STEP` preserves automatic initial selection. Parameters are validated before
workflow effects, stored in the session, and cannot change mid-run.
Previous models at 1 g and 2 g remain valid. Manual `threshold`, `x_threshold`
and `y_threshold` settings in either section, and both `SET_ACCEL_HOMING` and
`SET_BEACON_ACCEL_HOMING`, accept any finite value greater than 0 g. They have
no fixed 3 g minimum or 15 g maximum. When the configured/detected sensor range
is known, the effective threshold must be at most 80% of full-scale (12.8 g for
a generic ±16 g sensor); a wider sensor range permits values above 15 g.
The active stream range is checked again before arming, including a narrower
Beacon stream already in use. Native Beacon without known clip/range data
retains the existing behavior without an invented upper limit. A derived raw
trigger limit must also remain finite and positive before arming. These manual
settings do not change the separate 0.5–15 g calibration/model bounds.
Explicit existing `calibration_start_threshold` and `calibration_step` settings
are preserved unless a command overrides them for that run. Updating does not
replace any saved model threshold. The independent default 40-attempt cap
includes discovery and ten validation homes. A finer grid can exhaust that
cap before reaching the configured threshold ceiling or completing validation;
this change does not add automatic attempts.
One qualifying discovery is followed by ten additional accepted validation
homes, returning only the tested axis to center between accepted trials.
CALIBRATE and ACCURACY use the same helper for midpoint returns at ordinary
acceleration restored after homing. After the return completes, each waits for
`calibration_settle_time` (default 0.25 s, finite 0-2 s) before the next
acquisition. Zero disables only this extra wait. Session identity is checked
after yielding operations. Homing speed, the acceleration cap during homing,
thresholds and TEST/CHARTS return behavior are unchanged.

Discovery requires an impact after more than 20 mm of commanded travel. The
discovery impact is excluded from the following ten centered checks; their
max-minus-min spread must be at most 2 mm. The running spread is checked after
every accepted impact; >2 mm immediately raises the candidate and restarts
discovery and all ten checks.
For discovery and every validation impact beyond 20 mm, commanded trigger
travel must also lie inside the inclusive band
`expected * (1 - calibration_travel_tolerance)` through
`expected * (1 + calibration_travel_tolerance)`, where `expected` is the absolute
configured midpoint-to-endstop distance and the existing tolerance defaults
to 0.20. Outside this band, the endstop coordinate is untrustworthy: the run
aborts, invalidates only the selected axis's homed state, and requires manual
XY centering before a new command. It does not return, raise the threshold or
write a model. Other-axis/Z state is preserved. This checks commanded-travel
plausibility, not actual position or contact with the intended stop.
An impact at or before 20 mm instead uses recorded signed start-to-halt travel
to return only the selected axis to its estimated starting point, settles,
and retries discovery automatically at the next candidate. It clears the
selected axis's homed state and the validation window. There is no manual
fallback when return checks fail. Sensor faults, missing or uncertain evidence
and homing errors abort; stock homing errors may release all motors.
These bounded early reversals retain their existing homing acceleration cap
and configured settling wait.
See [SIMPLE_CALIBRATION.md](SIMPLE_CALIBRATION.md) for the per-return bounds
and commanded-travel limitations.

TEST/CHARTS use the same manual preparation, with both XY axes unhomed, and
start directly with `ACCEL_HOMING_TEST CENTERED=1` or
`ACCEL_HOMING_CHARTS_GENERATE CENTERED=1`. They retain the prepared XY state
until homing starts; there is no preliminary XY release or separate
confirmation command. All three workflows require `CENTERED=1` before any
workflow movement. Early calibration retries need no additional command.
The workflows emit no UI prompt actions.

Successful calibration defaults to `SAVE_CONFIG=1`, writing the staged model
to `printer.cfg` and restarting Klipper. `SAVE_CONFIG=0` stages only in memory;
it does not write or restart, and the workflow restores the previous active
thresholds. An explicit `SAVE_CONFIG` is still required to persist and reload
the new model; an ordinary `RESTART` discards unsaved staged values. Manual
threshold overrides retain priority after reload.
Existing saved models remain compatible without a new range check on loading.
Their historical validations are not certified by this rule; only a new
successful calibration records the new range qualification.

## Native Beacon with an expanded reference

A native calibration reference, or another reference whose accepted peak-to-peak
variation exceeds 20% of the active impact threshold, carries an observed noise
envelope. Each sensor axis uses `max(abs(low - mean), abs(high - mean))`; a non-calibration reference within the previous
limit keeps the legacy native motion and already-pressed path.

For an expanded reference, motion proof uses a lower velocity bound after
subtracting that envelope from the accumulated acceleration. A bounded
history retains an early lower-speed peak so watchdog/report alignment does
not discard valid ramp evidence. A retained peak must reach at least half the
full effective homing speed, with the usual later commanded-history evidence;
the instantaneous lower-bound comparison remains available. Automatic impact
direction likewise needs a signed velocity interval entirely beyond +30 or
-30 mm/s; uncertain drift keeps detection bidirectional.

Expanded native references also have an additional interval proof anchored
to the first commanded step found in recorded step history. Queued homing
startup can put stationary sensor samples between arming and those steps;
the added proof does not integrate that entire earlier stationary interval.
The step epoch is established on the reactor thread, without querying
steppers from the sensor callback, with a 50 microsecond search precision.
It describes commanded steps, not an encoder measurement of motor movement.

A candidate sensor interval starts within 15 ms before or after that epoch,
never before arming. It ends no later than the epoch plus the effective
acceleration-ramp duration (`speed / acceleration`), 15 ms and one sample,
and never after the original startup deadline. Its coherent X/Y/Z samples
use the same interval and observed noise envelope. The conservative lower
velocity must reach half the full effective speed, and the usual lagged
commanded-history check must still qualify. Sample gaps, sensor faults and
bounded-history overflow cannot qualify this additional proof. The history
holds at most 4096 prefix entries, including the initial entry. It never
silently evicts entries; overflow requests a host stop with `proof_overflow`.
Each watchdog callback shares a budget of 1024 same-window comparisons, even
when it checks proof more than once. Step-history query errors prevent
initial proof, including use of a cached full-arm peak.

The ramp duration uses planned effective speed and acceleration. A broader
physical acceleration response can still leave too little evidence to
qualify; the original deadline then applies without changing thresholds,
speed or timeout. This is not general qualification for every ramp shape.

Already-pressed detection uses the **current upper velocity bound**, rather
than treating a small lower bound as proof that the toolhead is stationary.
If uncertainty prevents a valid decision and no raw impact occurs, a finite
startup deadline requests a host stop and reports `no_motion`. That deadline
is the earlier of the existing travel deadline and the generic startup-time
calculation below with native `stream_timeout`
as the sample-age allowance and the native early-report interval as its
reporting interval (3 ms). At 75 mm/s, 5000 mm/s² and the default 50 ms
`stream_timeout`, the startup calculation is 90 ms. No generic travel-budget
setting is applied to Beacon. Expanded native packets require a finite
positive sample interval and strict sample-clock order. Their oldest retained
post-arm sample must be no older than `stream_timeout`, and their newest
sample no more than 5 ms in the future, before proof or trigger dispatch.
Missing, stale, invalid or discontinuous data cannot establish initial motion
proof on this path. A later gap does not revoke already established motion.

Expanded native results retain a bounded `motion_proof` summary, visible in STATUS
under `last.x` or `last.y` and in a short console line. It records the relevant
sensor/step timing, motion bounds and proof eligibility so a later failure can
be inspected after cleanup. Fields include `first_step_after_arm_ms`,
`first_sample_after_arm_ms`, `lower_speed_mm_s`, `step_epoch_peak_mm_s`,
`command_eligible` and `request_after_arm_ms`, plus gap/overflow flags and
bounded-history counts. `evaluated_sample_after_arm_ms` identifies the
snapshot used for the bounds; `sample_after_arm_ms` can identify a newer
sample. Effective speed and acceleration are planned values, not measurements.
The summary does not retain raw sample arrays or establish that every field
failure has the same cause.

Raw baseline-relative impact samples and the configured threshold remain
unchanged. The first valid impact after arming is eligible, including an
impact before motion proof; there is no blanking interval, minimum travel,
automatic threshold increase or speed change. Workflows retain their separate
travel and motion qualification. The observed envelope is not a guaranteed
bound on later disturbances, and the host deadline is not a physical
stopping-distance guarantee. This path has synthetic validation only.

## Generic startup motion policy

While no impact has completed a generic home, fresh acceleration must prove
relative motion by the startup deadline. If it has not, the generic path
requests a host stop and reports `no_motion` as a homing failure. It never
classifies missing motion as a successful already-pressed home. Ordinary `G28`
can finish immediately on a fresh threshold impact even before motion proof;
the calibration/test/chart workflows separately reject suspect motion/travel.
A separate `motion_seen` proof allows slow workflow homes without changing
Beacon's historical already-pressed cutoff.

During calibration, or if any axis of another generic reference exceeds the
original 20% quiet limit, movement proof subtracts an allowance for observed stationary noise.
For each sensor axis `i`, the accepted window supplies its low, high and mean:

```text
noise_bound_i = max(abs(low_i - mean_i), abs(high_i - mean_i))
proof_sum_i = max(0, abs(sum(sample_i - mean_i)) - N * noise_bound_i)
```

`N` includes the first accumulated sample. The existing physical velocity
conversion and vector norm use these bounded sums for movement proof.
Automatic impact-direction selection also uses the noise bound for these
generic references. The relevant signed velocity interval must lie entirely
at least +30 mm/s or at most -30 mm/s before inferring a direction; uncertain
drift leaves automatic detection bidirectional.

The applied bounds appear in status as
`baseline.motion_proof_noise_bound_g` (an X/Y/Z mapping, or `None` when not
applied) and in the console as `motion-proof noise bound XYZ ... g`.

Generic references with a measured noise envelope also retain the greatest
coherent full-arm lower-speed bound observed at an eligible batch endpoint
no later than their original startup deadline. When the existing lagged
commanded-speed check becomes eligible, this peak can qualify only if it is
at least `0.5 * effective_homing_speed`. The instantaneous comparison is still
available. This closes a report-phase case in which a valid early lower bound
was observed before command eligibility and then lost as accumulated
uncertainty grew. No lower bound is reconstructed from raw speed alone, no
smaller later command reduces the historical threshold, and a late report
endpoint cannot create a qualifying historical peak. Data/gap checks remain
active. This does not add native-style first-step scanning to generic sensors.

Generic attempts retain a constant-size `motion_proof` result through errors
and cleanup. It records raw/lower/upper integrated speed, retained lower-speed
peak, commanded-speed eligibility, effective speed/acceleration, measured XYZ
noise bounds, sample timing and startup deadline. Generic results do not
classify their absent native step-history fields as a missing-step failure.
The `no_motion` exception includes a concise numerical summary, and STATUS
retains the fuller record. Diagnostics do not change the stop decision.

Raw baseline-relative acceleration, impact thresholds, manual direction
overrides, first-impact eligibility and the startup deadline are unchanged.
Native Beacon uses the separate expanded-reference policy above.

The generic adjustment has synthetic validation only. It assumes later
stationary noise stays within the deviations observed in the accepted
window; a larger later disturbance can still resemble motion. Very noisy
or slow cases may fail with `no_motion`. That failure does not automatically
increase speed or impact thresholds.

The startup deadline calculation is unchanged. With effective homing speed `v`
(the lesser of rail speed and toolhead velocity limit), effective acceleration
`a` (current toolhead acceleration limited by nonzero `homing_accel`), and all
times in seconds, the calculation is:

```text
w = max(0.004, min(0.050, 0.050 / v))
T = v / a + 0.015 + max_sample_age + max(0.010, 2 * stream_interval, w)
nominal request travel = v * (T + 0.003) - 0.5 * v * v / a
```

`T` includes the acceleration ramp, 15 ms command/sensor lag allowance,
accepted sample age, and reporting/proof-window margin. The startup watchdog
still requests a stop when motion proof is absent by that deadline. There is
no default report-distance or startup-distance admission cap: a finite positive
configured homing speed is preserved without inserting either option.
Freshness, stream-fault and finite timing checks remain mandatory.

`max_startup_travel` is an optional nominal request-travel limit, disabled when
absent and bounded to finite 0.1..5 mm values when explicitly configured.
`max_report_travel` is independently optional, disabled when absent and bounded
to finite 0.1..2 mm values when configured. An explicit startup limit is checked
before enable/backoff and again before the homing pass; it does not change the
startup deadline or become a physical stopping guarantee.
Resolve the speed/acceleration configuration within the machine's supported
limits before retrying. The installer does not raise acceleration.
The installer preserves the configured `homing_speed` unless an explicit
`--homing-speed N` requests an edit. An incompatible configured or explicitly
requested plan is refused before installation writes; there is no automatic
speed edit or reduction. When a reporting limit is explicitly configured, its
speed ceiling is `max_report_travel / max_sample_age`. For example, an explicit
1 mm limit with 0.020 s sample age imposes 50 mm/s. This is an opt-in limit,
not a default ceiling. Each configured budget applies independently.
Optional installer `--max-report-travel N` and `--max-startup-travel N` flags
select those settings before validation and persist only explicit overrides.
Existing limits and homing speeds are preserved when no matching flag is
given. The flags are refused for native Beacon. The installer changes neither
sample age, reporting interval, current nor acceleration to make a speed pass,
and does not restore a historical speed from older backups.
CALIBRATE/TEST/CHARTS also make a read-only check of their requested generic X/Y
plans before workflow effects, including when both axes are still unhomed.
The owner method `preflight_motion_policy(axes, error=None)` validates selected
module endstops and calls the backend rail validator without moving, enabling
motors, changing settings or consulting a presumed physical start position.
Beacon retains its existing path. Invalid/nonpositive live toolhead limits are
rejected; `homing_accel: 0` leaves the live acceleration in force, and a nonzero
value only caps it. Later preparation checks remain necessary because live
limits can change after this first check.
Ordinary public `G28` is not wrapped by this preflight. With `safe_z_home`, a
configured Z hop can occur before a later X/Y policy rejection. These budget
options do not change the safe Z section or Z settings.

For `v=50 mm/s`, `a=5000 mm/s²`, `max_sample_age=0.020`, and
`stream_interval=0.005`, `T=0.055 s` and nominal request travel is `2.65 mm`.
At 100 mm/s with those timing/acceleration settings, `T=0.065 s` and nominal
request travel is `5.8 mm`. Without an explicit distance cap, neither plan is
rejected for this distance. An explicit `max_startup_travel: 3` would reject
the latter. These are synthetic software calculations, not physical validation
of either speed. Lower live acceleration increases the startup time and travel;
a plugin cap does not raise a lower live value left by a macro or tuning run.
The extra 3 ms covers one nominal watchdog interval. It excludes
unbounded host scheduling stalls, transport and MCU stop delay, and actual
mechanical response. **The deadline and optional nominal travel limits are not
physical stopping guarantees.**
Backend status exposes `startup_motion_request_limit_mm`,
`startup_motion_timeout_s`, and `startup_motion_request_travel_mm` for
inspection; the limit key reflects the explicit `max_startup_travel` or `null`
when disabled. These remain software planning values.

The calibration criterion `max(travel_mm) - min(travel_mm) <= 2 mm` measures
spread across ten accepted validation homes. It does not constrain the travel
of a single home and does not set either optional distance cap. Host latency,
transport and the mechanical response remain relevant at every speed.

Mechanics remain a separate requirement. The sensor must move with the tested
axis, be mounted firmly and have the correct machine `axes_map`. Toolhead-only
sensors do not measure moving-bed Y impacts. Multi-MCU motor arrangements,
unusual kinematics, independent carriages, endstop geometry and every possible
ADXL carrier have not been physically validated. Supervised workflows limit
their supported kinematics to Cartesian/CoreXY with independent Z and verified
stock G28, either directly or beneath one recognized built-in `safe_z_home` or
`homing_override`. Other homing wrappers, nested chains and potentially
Z-coupled paths remain rejected.

Distance qualification uses commanded/firmware step positions, not encoder
measurements. An accelerometer plus step counts cannot distinguish every jam,
skipped step or wrong physical stop from valid motion. A qualifying discovery,
ten validation repeats and a saved model are tuning evidence, not independent
physical metrology or safe stopping certification. Choosing a different start
or step does not prove that skipped steps or inconsistent physical homing are
fixed.
