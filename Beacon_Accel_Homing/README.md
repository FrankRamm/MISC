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
with the correct `axes_map`. Workflows require Cartesian/CoreXY, independent Z
and stock `G28` (standard `safe_z_home` or `homing_override` is supported);
see [compatibility](docs/COMPATIBILITY.md).

By default, calibration starts at 0.5 g and needs ten validation homes after
discovery, with travel spread below 1.5 mm.

1. Keep the printer idle and unpaused. **Center XY by hand and leave both
   axes unhomed in Klipper.** Keep Z supported and ensure nozzle/bed clearance:
   workflows skip Z hops and override G-code. Prepare suitable XY currents/enables separately.
2. In the **printer console**, run:

   ```gcode
   ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1
   ```

   **`CENTERED=1` authorizes movement without another confirmation.**
3. Each successful calibration saves that axis and restarts Klipper. Leave the saved
   **DO NOT EDIT** block alone.
4. After restart, repeat step 1, then calibrate Y:

   ```gcode
   ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1
   ```

If asked to recenter, repeat the preparation and the **same command** to resume.
After physically validating both axes, use the printer's normal homing controls.

## Mainsail / Fluidd console commands

Enter these commands in the **printer console** in Mainsail or Fluidd.

TEST, CHARTS and ACCURACY need the same manual preparation as calibration.

| Printer-console command | Purpose |
| --- | --- |
| `ACCEL_HOMING_STATUS` | Show settings, progress and results. |
| `ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1` | Calibrate X; saves X and restarts Klipper. |
| `ACCEL_HOMING_CALIBRATE AXIS=Y CENTERED=1` | Calibrate Y; saves Y and restarts Klipper. |
| `ACCEL_HOMING_CALIBRATE_NOISE` | Optional stationary measurement after preparation, before calibration; no movement. |
| `ACCEL_HOMING_TEST CENTERED=1` | Test X and Y. |
| `ACCEL_HOMING_CHARTS_GENERATE AXIS=X CENTERED=1` | Chart X; use `AXIS=Y` or omit `AXIS` for both. |
| `ACCEL_HOMING_ACCURACY AXIS=X SAMPLES=10 CENTERED=1` | Measure repeated X commanded-travel variation and export PNG charts. |
| `ACCEL_HOMING_CANCEL` | Cancel pending work; not an emergency stop. |

Noise candidates expire on restart; repeat the measurement before Y if using them.

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
