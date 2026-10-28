# Accelerometer Homing

Use a configured accelerometer as an X/Y impact endstop in Klipper or Kalico.
This experimental extension deliberately contacts mechanical stops.
Supervise initial homes and calibration with an accessible emergency stop.

## Install

With the printer **idle**, paste this into the Linux host's **shell** as
the existing Klipper service user:

```bash
installer=$(curl -fsSL https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/bootstrap.sh) && bash -c "$installer"
```

The installer backs up changes, preserves speeds, thresholds and currents,
and sets up Moonraker updates automatically. **No API keys, identifiers,
paths or configuration need to be entered.** Detected services restart;
it may ask for your Linux sudo password. Installation does not home the printer.

For later updates, use **accel_homing → Update** in Mainsail/Fluidd while idle.
Older recorded snapshot installations use the same installer to migrate.
[Custom hosts and recovery](docs/INSTALLATION.md).

## Moonraker updates

New installations add this entry directly to `moonraker.conf` automatically.
For manual setup, replace `path` below with the complete **Managed update source**
path printed by the installer, then paste into that instance's `moonraker.conf`:

```ini
[update_manager accel_homing]
type: git_repo
channel: dev
path: <full Managed update source path>
origin: https://github.com/FrankRamm/MISC.git
primary_branch: codex/universal-accel-homing
is_system_service: False
managed_services: klipper
```

Add `[update_manager]` once if your configuration does not already contain it.
Keep only one `accel_homing` updater entry, including entries in an existing
`accel_homing_update.cfg`; those installations keep their current include.
If you edited the configuration manually, restart that Moonraker service while idle.

## Settings

In your existing `[accel_homing]` section (`accel_homing.cfg` for new installs),
`x_threshold` and `y_threshold` set manual values in g. To use calibration,
comment out those settings and any shared `threshold`, then run `RESTART`
while idle.

## Calibrate X, then Y

Use an already working, firmly mounted sensor that moves with the tested axis,
without requiring sensor-to-printer axis mapping for this detector.
Workflows require Cartesian/CoreXY, independent Z
and stock `G28` (standard `safe_z_home` or `homing_override` is supported);
see [compatibility](docs/COMPATIBILITY.md).

Calibration uses **three-axis acceleration magnitude** about a fresh resting
offset, with **no quietness gate or acceleration-derived motion proof**.
It starts at **1.0 g**, with **0.25 g** increments by default.

An **impact after more than 20 mm of travel** selects the candidate. Reaching
20 mm without an impact does not stop or qualify the move. It then centers
**only the calibrated X or Y axis** and performs **ten** endstop checks from
that axis's configured midpoint. The first discovery impact is excluded.
**Max distance minus min distance <= 2.00 mm passes**; a larger ten-check spread
raises the threshold one step, clears the entire window, recenters that axis,
and automatically restarts impact discovery and validation.
An impact at or before 20 mm stops immediately, raises the threshold, and
requires manual recentering and the next command. Data faults/no impact abort.

Returns target the configured axis midpoint after stock homing assigns the
endstop coordinate. The homing acceleration cap and default 0.25 s settling
wait remain. Step counts are not physical-position feedback.
[Complete algorithm and limitations](docs/SIMPLE_CALIBRATION.md).

1. Keep the printer idle and unpaused. **Center XY by hand and leave both
   axes unhomed in Klipper.** Keep Z supported and ensure nozzle/bed clearance:
   workflows skip Z hops and override G-code. Prepare suitable XY currents/enables separately.
2. In the **printer console**, run:

   ```gcode
   ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1 START_THRESHOLD=1 STEP=0.25
   ```

   **`CENTERED=1` authorizes movement without another confirmation.**
3. Each successful calibration saves that axis and restarts Klipper. Leave the saved
   **DO NOT EDIT** block alone.
4. After restart, repeat step 1, then calibrate Y:

   ```gcode
   ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1 START_THRESHOLD=1 STEP=0.25
   ```

If asked to recenter, repeat the preparation and the **same command** to resume.
The explicit start and step above override older configuration values and
do not consume stationary-noise hints. A resumed run keeps its
current candidate; repeating `START_THRESHOLD=1` does not reset it to 1 g.
After physically validating both axes, use the printer's normal homing controls.

## Mainsail / Fluidd console commands

Enter these commands in the **printer console** in Mainsail or Fluidd.

TEST, CHARTS and ACCURACY need the same manual preparation as calibration.

| Printer-console command | Purpose |
| --- | --- |
| `ACCEL_HOMING_STATUS` | Show settings, progress and results. |
| `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1` | Calibrate X; saves X and restarts Klipper. |
| `ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` | Calibrate Y; saves Y and restarts Klipper. |
| `ACCEL_HOMING_CALIBRATE_NOISE` | Optional stationary diagnostic; no movement and no automatic calibration seeding. |
| `ACCEL_HOMING_TEST CENTERED=1` | Test X and Y. |
| `ACCEL_HOMING_CHARTS_GENERATE AXIS=X CENTERED=1` | Chart X; use `AXIS=Y` or omit `AXIS` for both. |
| `ACCEL_HOMING_ACCURACY AXIS=X SAMPLES=10 CENTERED=1` | Measure repeated X commanded-travel variation and export PNG charts. |
| `ACCEL_HOMING_CANCEL` | Cancel pending work; not an emergency stop. |

Noise measurement is diagnostic only; it does not choose the starting threshold for simple calibration.
New saved models preserve XYZ-magnitude detection for ordinary homing after restart.

## Uninstall

Run in the same host shell while idle:

```bash
bash -c 'set -euo pipefail; t=$(mktemp); trap "rm -f \"$t\"" EXIT; curl -fL --retry 3 "https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/Beacon_Accel_Homing/uninstall.sh" -o "$t"; bash "$t" --repo FrankRamm/MISC --ref codex/universal-accel-homing'
```

Review the output, then **manually restart the listed services while idle**.

## For the curious

[Detailed reference](https://github.com/FrankRamm/MISC/blob/codex/universal-accel-homing/Beacon_Accel_Homing/docs/DETAILS.md) ·
[Installation and recovery](docs/INSTALLATION.md) ·
[Configuration example](examples/accel_homing.cfg) · [Offline tests](TESTING.md).

## Credits

**[CloakedWayne](https://github.com/CloakedWayne)** created the original Beacon
Accelerometer Homing plugin and impact-detection algorithm in [Monolith3D/MISC](https://github.com/Monolith3D/MISC/tree/main/Beacon_Accel_Homing).
Thanks to **mc-red** for extensive testing. This [FrankRamm extension](https://github.com/FrankRamm/MISC/tree/codex/universal-accel-homing)
is intended for supervised testing and future upstream contribution after
physical validation. [Full credits](NOTICE) · [GPLv3](LICENSE).
