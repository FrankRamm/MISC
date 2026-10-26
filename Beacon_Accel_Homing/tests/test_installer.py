"""Offline synthetic host/config fixtures. Never contact a printer or a service."""
import base64
import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

DIRECTORY = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("installer")
bootstrap = load("bootstrap")

CONFIG = """# Keep my printer settings
[printer]
kinematics: corexy
max_velocity: 300
max_accel: 5000
[include axes.cfg]
[beacon]
serial: /dev/serial/by-id/fixture-only
[gcode_macro PRINT_START]
gcode:
    M118 Leave this exact macro alone
"""
AXES = """[stepper_x]
endstop_pin: tmc2209_stepper_x:virtual_endstop
position_endstop: 0
position_max: 350
homing_speed: 150
homing_retract_dist: 5
homing_accel: 1000
min_home_dist: 10
[stepper_y]
endstop_pin: !PA0
position_endstop: 350
position_max: 350
homing_speed: 35
homing_retract_dist: 3
[tmc2209 stepper_x]
diag_pin: ^PB0
run_current: 1.2
home_current: 0.6
driver_SGTHRS: 50
[tmc5160 stepper_y]
diag0_pin: PB1
run_current: 1.5
[tmc2209 stepper_z]
diag_pin: PB2
run_current: 0.8
"""
SAVED = """
#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*# [beacon model default]
#*# model_coef = 1.234
#*# [accel_homing_model default]
#*# x_threshold = 6.5
"""
SAFE_Z_HOME = """# Preserve the operator's probe homing setup
[safe_z_home]
home_xy_position: 175, 175
speed: 90
z_hop: 15
z_hop_speed: 8
move_to_previous: True
"""


class HostFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "klipper"
        self.extras = self.repo / "klippy/extras"
        self.extras.mkdir(parents=True)
        (self.repo / "klippy/klippy.py").write_text("# fixture\n")
        (self.extras / "beacon.py").write_text("# fixture driver\n")
        (self.extras / "adxl345.py").write_text("# fixture driver\n")
        (self.repo / ".git/info").mkdir(parents=True)
        (self.repo / ".git/info/exclude").write_text("# existing\n")
        self.config = self.home / "printer_data/config/printer.cfg"
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes((CONFIG + SAVED).encode())
        self.axes = self.config.parent / "axes.cfg"
        self.axes.write_bytes(AXES.encode())
        self.source = self.home / "package"
        self.source.mkdir()
        for name in installer.MODULES:
            (self.source / name).write_text("# fixture source\n")
        self.selected = dict(repo=self.repo.resolve(), config=self.config.resolve(), socket=None,
                             pid=None, service=None, origin="fixture", executable=None)

    def plan(self, *flags):
        return installer.make_plan(self.source, self.selected, installer.arguments(list(flags)))

    def cartographer_safe_z_fixture(self):
        probe_home = self.config.parent / "probe-home.cfg"
        probe_home.write_bytes(SAFE_Z_HOME.replace("\n", "\r\n").encode())
        config = CONFIG.replace("[beacon]", "[scanner]")
        config += ("\n[adxl345 toolhead]\ncs_pin: fixture:None\n"
                   "[include probe-home.cfg]\n")
        saved = SAVED.replace("[beacon model default]", "[scanner model default]")
        self.config.write_bytes((config + saved).encode())
        return probe_home, saved

    def require_symlinks(self):
        link = self.home / "symlink-test"
        try:
            os.symlink(self.source, link, target_is_directory=True)
            link.unlink()
        except OSError:
            self.skipTest("Host does not allow symlink creation")

    def test_common_directory_detection(self):
        chosen = installer.discover_target(installer.arguments([]), self.home, [], [])
        self.assertEqual(chosen["repo"], self.repo)
        self.assertEqual(chosen["config"], self.config)

    def test_running_process_takes_priority_and_executable_retained(self):
        live = dict(self.selected, pid=123, executable="/venv/bin/python", origin="running", service="custom.service")
        chosen = installer.discover_target(installer.arguments([]), self.home, [live], [])
        self.assertEqual(chosen["executable"], "/venv/bin/python")
        self.assertEqual(chosen["pid"], 123)

    def test_multiple_running_instances_require_selection(self):
        one = dict(self.selected, pid=1)
        two = dict(self.selected, pid=2, config=self.home / "other.cfg")
        with self.assertRaisesRegex(installer.InstallError, "Multiple running"):
            installer.discover_target(installer.arguments([]), self.home, [one, two], [])
        selected = installer.discover_target(installer.arguments(["--config", str(self.config)]), self.home, [one, two], [])
        self.assertEqual(selected["pid"], 1)

    def test_multiple_stopped_checkouts_rejected(self):
        other = self.home / "kalico/klippy"
        other.mkdir(parents=True)
        (other / "klippy.py").write_text("# fixture")
        with self.assertRaisesRegex(installer.InstallError, "Cannot choose"):
            installer.discover_target(installer.arguments([]), self.home, [], [])

    def test_explicit_wrong_service_cannot_bypass_live_instance(self):
        live = dict(self.selected, pid=10, service="klipper.service")
        args = installer.arguments(["--klipper-path", str(self.repo), "--config", str(self.config), "--service", "wrong"])
        with self.assertRaises(installer.InstallError):
            installer.discover_target(args, self.home, [live], [])

    def test_command_parser_extracts_config_socket_and_python(self):
        parsed = installer.command_instance(["/venv/bin/python", str(self.repo / "klippy/klippy.py"),
                    str(self.config), "-l", "/tmp/klippy.log", "-a", "/tmp/klippy.sock"], self.home, "fixture")
        self.assertEqual(parsed["repo"], self.repo)
        self.assertEqual(parsed["socket"], Path("/tmp/klippy.sock").resolve())
        self.assertEqual(parsed["executable"], "/venv/bin/python")

    def test_proc_fixture_extracts_service_without_execution(self):
        proc = self.home / "proc/101"
        proc.mkdir(parents=True)
        argv = ["python", str(self.repo / "klippy/klippy.py"), str(self.config), "-a", str(self.home / "uds")]
        (proc / "cmdline").write_bytes(b"\0".join(v.encode() for v in argv) + b"\0")
        (proc / "cgroup").write_text("0::/system.slice/klipper-two.service\n")
        found = installer.proc_instances(proc.parent)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["service"], "klipper-two.service")

    def test_plan_activates_included_endstops_and_preserves_saved_block(self):
        changes, messages, sensor = self.plan()
        self.assertEqual(sensor, "beacon")
        axes = changes[self.axes]["data"].decode()
        self.assertIn("endstop_pin: accel_homing:x", axes)
        self.assertIn("endstop_pin: accel_homing:y", axes)
        self.assertIn("homing_retract_dist: 0", axes)
        self.assertIn("# accel_homing previous: diag_pin: ^PB0", axes)
        self.assertIn("driver_SGTHRS: 50", axes)
        self.assertIn("[tmc2209 stepper_z]\ndiag_pin: PB2", axes)
        self.assertIn("homing_speed: 150", axes)
        main = changes[self.config]["data"].decode()
        self.assertTrue(main.endswith(SAVED))
        self.assertIn("M118 Leave this exact macro alone", main)
        self.assertLess(main.index("[include accel_homing.cfg]"), main.index(installer.SAVE_MARKER))
        created = changes[self.config.parent / installer.MANAGED]["data"].decode()
        self.assertIn("#x_threshold: 8.0\n#y_threshold: 8.0", created)
        self.assertNotRegex(created, r"(?m)^x_threshold:")

    def test_generic_speed_cap_never_raises_slower_axis(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 toolhead]") + SAVED)
        changes, messages, sensor = self.plan()
        graph = changes[self.axes]["data"].decode()
        self.assertIn("homing_speed: 50", graph)
        self.assertIn("homing_speed: 35", graph)
        self.assertIn("adxl345 toolhead", sensor)
        with self.assertRaisesRegex(installer.InstallError, "exceeds"):
            self.plan("--homing-speed", "150")

    def planned_speed(self, changes, axis):
        text = changes[self.axes]["data"].decode().replace("\r\n", "\n")
        match = re.search(r"(?ms)^\[stepper_" + axis + r"\].*?^homing_speed:\s*([\d.eE+-]+)", text)
        return float(match.group(1))

    def test_generic_low_accel_install_lowers_default_before_startup_budget(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 toolhead]").replace("max_accel: 5000", "max_accel: 1000") + SAVED)
        before = self.config.read_bytes()
        changes, messages, _ = self.plan()
        x, y = self.planned_speed(changes, "x"), self.planned_speed(changes, "y")
        policy = installer.load_motion_policy()
        self.assertGreater(x, 43.0)
        self.assertLess(x, 50.0)
        self.assertEqual(y, 35.0)
        self.assertLessEqual(policy.motion_startup_request_travel(x, 1000.0, 0.020, 0.005), 3.0)
        self.assertTrue(any("report/startup" in message for message in messages))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.config.parent / ".accel_homing_backups").exists())

    def test_explicit_generic_speed_above_startup_budget_refuses_without_writes(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 toolhead]").replace("max_accel: 5000", "max_accel: 1000") + SAVED)
        before = self.config.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "startup stop request"):
            self.plan("--homing-speed", "50")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.config.parent / installer.MANAGED).exists())
        changes = self.plan("--homing-speed", "40")[0]
        self.assertEqual(self.planned_speed(changes, "x"), 40.0)

    def test_generic_startup_budget_uses_plugin_acceleration_and_zero_override(self):
        policy = installer.load_motion_policy()
        for homing_accel, effective_accel in ((100.0, 100.0), (0.0, 5000.0)):
            self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 toolhead]") +
                                   "\n[accel_homing]\nhoming_accel: " + str(homing_accel) + "\n" + SAVED)
            changes, _, _ = self.plan()
            x = self.planned_speed(changes, "x")
            self.assertLessEqual(policy.motion_startup_request_travel(x, effective_accel, 0.020, 0.005), 3.0)
            self.assertLessEqual(x, 50.0)
            self.assertIn(("homing_accel: " + str(homing_accel)).encode(), changes[self.config]["data"])

    def test_generic_velocity_limit_participates_without_raising_rail_speed(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 toolhead]").replace("max_velocity: 300", "max_velocity: 20").replace("max_accel: 5000", "max_accel: 1000"))
        changes = self.plan()[0]
        self.assertEqual(self.planned_speed(changes, "x"), 150.0)
        self.assertEqual(self.planned_speed(changes, "y"), 35.0)
        # Runtime sees min(rail speed, max_velocity)=20, not the raw rail setting.
        changes = self.plan("--homing-speed", "50")[0]
        self.assertEqual(self.planned_speed(changes, "x"), 50.0)

    def test_legacy_section_thresholds_and_pin_alias_preserved(self):
        self.config.write_text(CONFIG + "\n[beacon_accel_homing]\nx_threshold: 6.1\ny_threshold: 7.2\n" + SAVED)
        changes, _, _ = self.plan()
        text = changes[self.config]["data"].decode().replace("\r\n", "\n")
        self.assertIn("x_threshold: 6.1\ny_threshold: 7.2", text)
        self.assertNotIn("[include accel_homing.cfg]", text)
        self.assertIn("beacon_accel:x", changes[self.axes]["data"].decode())

    def test_multiple_sensors_requires_full_name(self):
        self.config.write_text(CONFIG + "\n[adxl345 toolhead]\ncs_pin: host:None\n")
        with self.assertRaisesRegex(installer.InstallError, "Expected one configured"):
            self.plan()
        self.assertEqual(self.plan("--accel-chip", "adxl345 toolhead")[2], "adxl345 toolhead")

    def test_cartographer_probe_alone_is_not_a_sensor(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[scanner]"))
        with self.assertRaisesRegex(installer.InstallError, "Cartographer/scanner"):
            self.plan()

    def test_custom_homing_overrides_and_macros_still_require_acknowledgement(self):
        for section in ("homing_override", "gcode_macro G28"):
            self.config.write_text(CONFIG + "\n[" + section + "]\ngcode:\n    G28\n")
            with self.assertRaisesRegex(installer.InstallError, "Custom homing path"):
                self.plan()
            self.plan("--acknowledge-homing-override")

    def test_safe_z_home_with_cartographer_and_accelerometer_needs_no_bypass_flag(self):
        probe_home, saved = self.cartographer_safe_z_fixture()
        original = {path: path.read_bytes() for path in (self.config, self.axes, probe_home)}
        changes, messages, sensor = self.plan()
        self.assertEqual(sensor, "adxl345 toolhead")
        self.assertNotIn(probe_home, changes)
        main = changes[self.config]["data"].decode()
        self.assertIn("[scanner]\nserial: /dev/serial/by-id/fixture-only", main)
        self.assertTrue(main.endswith(saved))
        self.assertIn("[include probe-home.cfg]", main)
        self.assertTrue(any("[safe_z_home]" in message and "z_hop" in message for message in messages))
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.config.parent / ".accel_homing_backups").exists())

    def test_safe_z_home_does_not_exempt_custom_g28_and_duplicate_sections(self):
        for section in ("homing_override", "gcode_macro G28"):
            self.config.write_text(CONFIG + SAFE_Z_HOME + "\n[" + section + "]\ngcode:\n    G28\n")
            with self.assertRaisesRegex(installer.InstallError, "Custom homing path"):
                self.plan()
        self.config.write_text(CONFIG + SAFE_Z_HOME + "\n" + SAFE_Z_HOME)
        with self.assertRaisesRegex(installer.InstallError, "exactly one \\[safe_z_home\\]"):
            self.plan()

    def test_incomplete_package_fails_without_changes(self):
        (self.source / installer.MODULES[-1]).unlink()
        with self.assertRaisesRegex(installer.InstallError, "Incomplete package"):
            self.plan()
        self.assertEqual(self.config.read_bytes(), (CONFIG + SAVED).encode())

    def test_duplicate_sections_options_and_recursive_includes_rejected(self):
        self.axes.write_text(AXES + "\n[stepper_x]\nendstop_pin: PA1\n")
        with self.assertRaisesRegex(installer.InstallError, "exactly one"):
            self.plan()
        self.axes.write_text(AXES.replace("homing_speed: 150", "homing_speed: 150\nhoming_speed: 10"))
        with self.assertRaisesRegex(installer.InstallError, "Duplicate"):
            self.plan()
        self.axes.write_text(AXES + "\n[include printer.cfg]\n")
        with self.assertRaisesRegex(installer.InstallError, "Recursive"):
            self.plan()

    def test_existing_unincluded_config_not_overwritten(self):
        (self.config.parent / installer.MANAGED).write_text("# User file")
        with self.assertRaisesRegex(installer.InstallError, "not included"):
            self.plan()

    def test_conflicting_regular_plugin_file_rejected(self):
        (self.extras / installer.MODULES[0]).write_text("# custom plugin")
        with self.assertRaisesRegex(installer.InstallError, "non-symlink"):
            self.plan()

    def test_transaction_idempotence_and_rollback(self):
        before = {self.config: self.config.read_bytes(), self.axes: self.axes.read_bytes(),
                  self.repo / ".git/info/exclude": (self.repo / ".git/info/exclude").read_bytes()}
        changes = {p: v for p, v in self.plan()[0].items() if v["kind"] != "symlink"}
        backup = installer.apply_plan(changes, self.config)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "installed")
        with mock.patch.object(installer, "MODULES", ()):
            self.assertEqual(self.plan()[0], {})
        installer.rollback(backup, selected=self.selected)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.config.parent / installer.MANAGED).exists())
        self.assertFalse((self.extras / installer.MODULES[0]).is_symlink())

    def test_rollbacks_refuse_later_edits(self):
        changes = {p: v for p, v in self.plan()[0].items() if v["kind"] != "symlink"}
        backup = installer.apply_plan(changes, self.config)
        self.axes.write_text(self.axes.read_text() + "# later user edit\n")
        with self.assertRaisesRegex(installer.InstallError, "changed after"):
            installer.rollback(backup)
        self.assertIn("later user edit", self.axes.read_text())

    def test_failed_write_restores_prior_bytes(self):
        initial = self.config.read_bytes()
        calls = []
        def fail_second(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("synthetic disk failure")
            installer.write_state(path, value)
        with self.assertRaisesRegex(installer.InstallError, "restored"):
            installer.apply_plan({self.config: {"kind": "file", "data": b"changed"},
                                  self.axes: {"kind": "file", "data": b"changed"}}, self.config, fail_second)
        self.assertEqual(self.config.read_bytes(), initial)
        self.assertEqual(self.axes.read_bytes(), AXES.encode())

    def test_edit_after_plan_fails_before_backup_or_writes(self):
        changes = self.plan()[0]
        self.config.write_text(self.config.read_text() + "# concurrent edit\n")
        with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
            installer.apply_plan(changes, self.config)
        self.assertIn("concurrent edit", self.config.read_text())
        self.assertFalse((self.config.parent / ".accel_homing_backups").exists())

    def test_failure_rollback_preserves_concurrent_edit_to_applied_file(self):
        count = []
        def concurrent_failure(path, value):
            count.append(path)
            if len(count) == 2:
                self.config.write_bytes(b"concurrent editor/SAVE_CONFIG data")
                raise OSError("synthetic later write failure")
            installer.write_state(path, value)
        changes = {self.config: {"kind": "file", "data": b"installed A"},
                   self.axes: {"kind": "file", "data": b"installed B"}}
        with self.assertRaisesRegex(installer.InstallError, "rollback needs repair"):
            installer.apply_plan(changes, self.config, concurrent_failure)
        self.assertEqual(self.config.read_bytes(), b"concurrent editor/SAVE_CONFIG data")
        manifest = next((self.config.parent / ".accel_homing_backups").glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text())["status"], "rollback_incomplete")

    def test_explicit_rollback_guards_restore_and_failure_rollforward(self):
        changes = {self.config: {"kind": "file", "data": b"installed A"},
                   self.axes: {"kind": "file", "data": b"installed B"}}
        backup = installer.apply_plan(changes, self.config)
        real_writer = installer.write_state
        def editing_writer(path, value):
            real_writer(path, value)
            if path == self.axes:
                self.axes.write_bytes(b"new edit during rollback rollforward")
                self.config.write_bytes(b"new edit before next rollback restore")
        with mock.patch.object(installer, "write_state", side_effect=editing_writer):
            with self.assertRaisesRegex(installer.InstallError, "concurrent changes were preserved"):
                installer.rollback(backup)
        self.assertEqual(self.axes.read_bytes(), b"new edit during rollback rollforward")
        self.assertEqual(self.config.read_bytes(), b"new edit before next rollback restore")
        self.assertEqual(json.loads((backup / "manifest.json").read_text())["status"], "rollback_incomplete")

    def test_idle_requires_correct_instance_and_not_printing(self):
        selected = dict(self.selected, pid=123, socket=self.home / "fake-socket")
        def query(path, method, params=None):
            if method == "info":
                return dict(state="ready", config_file=str(self.config), klipper_path=str(self.repo))
            if method == "objects/list":
                return dict(objects=["idle_timeout", "print_stats", "pause_resume"])
            return dict(status={"idle_timeout": {"state": "Ready"}, "print_stats": {"state": "standby"}, "pause_resume": {"is_paused": False}})
        self.assertIn("ready/idle", installer.assert_idle(selected, query, lambda _: None))
        def wrong(path, method, params=None):
            return dict(state="ready", config_file=str(self.home / "wrong.cfg")) if method == "info" else query(path, method, params)
        with self.assertRaisesRegex(installer.InstallError, "different/unknown config"):
            installer.assert_idle(selected, wrong, lambda _: None)
        def printing(path, method, params=None):
            response = query(path, method, params)
            if method == "objects/query":
                response["status"]["print_stats"]["state"] = "printing"
            return response
        with self.assertRaisesRegex(installer.InstallError, "Refusing writes"):
            installer.assert_idle(selected, printing, lambda _: None)

    def test_running_unknown_socket_rejected_stopped_host_allowed(self):
        with self.assertRaisesRegex(installer.InstallError, "no known API socket"):
            installer.assert_idle(dict(self.selected, pid=1), service_state=lambda _: None)
        self.assertIn("stopped", installer.assert_idle(self.selected, service_state=lambda _: None))

    def test_mixed_case_sensor_suffix_preserved(self):
        self.config.write_text(CONFIG.replace("[beacon]", "[adxl345 ToolHead]"))
        changes, _, sensor = self.plan("--accel-chip", "adxl345 ToolHead")
        self.assertEqual(sensor, "adxl345 ToolHead")
        self.assertIn(b"accel_chip: adxl345 ToolHead", changes[self.config.parent / installer.MANAGED]["data"])

    def test_wildcard_include_does_not_get_duplicate_explicit_include(self):
        self.config.write_bytes((CONFIG + "\n[include accel_*.cfg]\n" + SAVED).encode())
        (self.config.parent / "accel_existing.cfg").write_text("# Existing include fixture\n")
        changes, _, _ = self.plan()
        self.assertNotIn(self.config, changes)
        file_changes = {p: v for p, v in changes.items() if v["kind"] != "symlink"}
        installer.apply_plan(file_changes, self.config)
        with mock.patch.object(installer, "MODULES", ()):
            self.assertEqual(self.plan()[0], {})

    def test_cli_dry_run_writes_nothing_and_does_not_query_live_status(self):
        before = sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*"))
        with mock.patch.object(installer, "discover_target", return_value=self.selected), \
             mock.patch.object(installer, "__file__", str(self.source / "installer.py")), \
             mock.patch.object(installer.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(installer, "assert_idle", side_effect=AssertionError("must not query")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--dry-run"]), 0)
        after = sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*"))
        self.assertEqual(before, after)

    def test_real_plugin_symlinks_where_supported(self):
        self.require_symlinks()
        changes = {p: v for p, v in self.plan()[0].items() if v["kind"] == "symlink"}
        backup = installer.apply_plan(changes, self.config)
        self.assertEqual((self.extras / installer.MODULES[0]).resolve(), self.source / installer.MODULES[0])
        installer.rollback(backup)
        self.assertFalse((self.extras / installer.MODULES[0]).is_symlink())


class BootstrapFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "cache"
        self.sha = "a" * 40
        self.urls = []
        self.data = {name: b"# synthetic complete offline payload\n" if name.endswith(".py") else b"offline fixture\n"
                     for name in bootstrap.FILES}

    def fetch(self, url):
        self.urls.append(url)
        if "/commits/" in url:
            return json.dumps({"sha": self.sha}).encode()
        if "/contents/" in url:
            suffix = url.split("/Beacon_Accel_Homing", 1)[1].split("?", 1)[0].strip("/")
            listing = []
            for name, data in self.data.items():
                if str(Path(name).parent).replace("\\", "/") != (suffix or "."):
                    continue
                digest = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
                listing.append(dict(name=Path(name).name, type="file", size=len(data), sha=digest))
            return json.dumps(listing).encode()
        return self.data[url.split("/Beacon_Accel_Homing/", 1)[1]]

    def test_branch_resolves_once_and_all_files_use_immutable_sha(self):
        release = bootstrap.prepare_release("example/project", "feature/branch", self.destination, self.fetch)
        self.assertIn("/commits/feature%2Fbranch", self.urls[0])
        raw = [url for url in self.urls if "raw.githubusercontent.com" in url]
        self.assertTrue(all("/" + self.sha + "/" in url for url in raw))
        metadata_directories = {Path(name).parent for name in bootstrap.FILES}
        self.assertEqual(len(self.urls), len(bootstrap.FILES) + 1 + len(metadata_directories))
        self.assertTrue((release / "installer.py").is_file())

    def test_default_bootstrap_downloads_testing_fork_before_installation(self):
        release = self.destination / "verified-release"
        with mock.patch.object(bootstrap.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(bootstrap, "prepare_release", return_value=release) as prepare, \
             mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(["--install-dir", str(self.destination), "--dry-run"]), 0)
        prepare.assert_called_once_with("FrankRamm/MISC", "codex/universal-accel-homing", str(self.destination))
        run.assert_called_once_with([bootstrap.sys.executable, str(release / "installer.py"), "--dry-run"])

    def test_shell_default_pins_testing_fork_and_preserves_installer_arguments(self):
        script = (DIRECTORY / "bootstrap.sh").read_text()
        embedded = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        data = (DIRECTORY / "bootstrap.py").read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()

        def response(url, **kwargs):
            url = getattr(url, "full_url", url)
            self.urls.append(url)
            if "/commits/" in url:
                payload = json.dumps({"sha": self.sha}).encode()
            elif "/contents/" in url:
                payload = json.dumps(dict(type="file", size=len(data), sha=blob)).encode()
            else:
                payload = data
            result = io.BytesIO(payload)
            result.headers = {"Content-Length": str(len(payload))}
            return result

        with mock.patch.object(bootstrap.urllib.request, "urlopen", side_effect=response), \
             mock.patch.object(bootstrap.sys, "argv", ["bootstrap.sh", "--dry-run"]), \
             mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             self.assertRaises(SystemExit) as stopped:
            exec(compile(embedded, "bootstrap.sh embedded Python", "exec"), {})
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.urls[0], "https://api.github.com/repos/FrankRamm/MISC/commits/codex%2Funiversal-accel-homing")
        self.assertTrue(all(self.sha in url for url in self.urls[1:]))
        self.assertEqual(run.call_args.args[0][2:], ["--repo", "FrankRamm/MISC", "--ref", self.sha, "--dry-run"])

    def test_complete_release_retains_runtime_source_license_and_original_credits(self):
        self.assertTrue(set(installer.MODULES).issubset(bootstrap.FILES))
        self.data = {name: (DIRECTORY / name).read_bytes() for name in bootstrap.FILES}
        release = bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch)
        for name in ("LICENSE", "NOTICE"):
            self.assertEqual((release / name).read_bytes(), (DIRECTORY / name).read_bytes())
        notice = (release / "NOTICE").read_text()
        self.assertIn("CloakedWayne, https://github.com/CloakedWayne", notice)
        self.assertIn("f5d97db465bc5499fb6ac4b7cc85698ef7a60cd4", notice)
        self.assertIn("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", notice)
        marker = json.loads((release / ".source.json").read_text())
        self.assertIn("NOTICE", marker["sha256"])

    def test_failed_download_leaves_no_release_or_install(self):
        def broken(url):
            if url.endswith("accel_homing_model.py"):
                raise OSError("synthetic HTTP failure")
            return self.fetch(url)
        with self.assertRaisesRegex(OSError, "HTTP failure"):
            bootstrap.prepare_release("example/project", self.sha, self.destination, broken)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_cache_reuse_checks_content_and_rejects_local_edits(self):
        release = bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch)
        self.urls.clear()
        self.assertEqual(bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch), release)
        self.assertEqual(self.urls, [])
        (release / "accel_homing.py").write_text("# custom edited source\n")
        with self.assertRaisesRegex(ValueError, "Cached source changed"):
            bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch)

    def test_malformed_python_is_never_installed(self):
        self.data["installer.py"] = b"not valid python !\n"
        with self.assertRaises(SyntaxError):
            bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_valid_python_prefix_truncation_fails_git_blob_verification(self):
        self.data["installer.py"] = b"# complete source\nprint('body')\n"
        def truncated(url):
            return b"# complete source\n" if url.endswith("/installer.py") else self.fetch(url)
        with self.assertRaisesRegex(ValueError, "pinned Git blob"):
            bootstrap.prepare_release("example/project", self.sha, self.destination, truncated)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_http_declared_length_mismatch_rejected(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": "100"}
        response.read.return_value = b"# valid Python prefix\n"
        with mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "Incomplete HTTP response"):
                bootstrap.download("https://offline.invalid/fixture")

    def test_partial_shell_invocation_cannot_drop_forwarded_arguments(self):
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("Bash unavailable for syntax-only truncation fixture")
        script = (DIRECTORY / "bootstrap.sh").read_text()
        invocation = script.rindex("\n(\n") + 1
        # Syntax checks only; no code, downloads, status queries or services run.
        full = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(full.returncode, 0, full.stderr)
        for end in (invocation + 1, script.rindex(' "$@"'), script.rindex("\n)")):
            truncated = subprocess.run([bash, "-n"], input=script[:end], text=True, capture_output=True)
            self.assertNotEqual(truncated.returncode, 0)


class UninstallFixture(unittest.TestCase):
    """Real temp config files, with managed symlinks emulated on Windows too."""
    plan = HostFixture.plan

    def setUp(self):
        HostFixture.setUp(self)
        self.links = {}
        self.real_snapshot, self.real_write = installer.snapshot, installer.write_state
        def snapshot(path):
            path = Path(path)
            return dict(self.links.get(path, {"kind": "absent"})) if self.is_module(path) else self.real_snapshot(path)
        self.snapshot = snapshot
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installer, "snapshot", side_effect=snapshot).start()
        mock.patch.object(installer, "write_state", side_effect=self.write).start()

    def is_module(self, path):
        return path.parent == self.extras and path.name in installer.MODULES

    def write(self, path, state):
        path = Path(path)
        if self.is_module(path):
            self.links[path] = dict(state)
        else:
            self.real_write(path, state)

    def install(self, release="package", *flags):
        self.source = self.home / release
        self.source.mkdir(exist_ok=True)
        for name in installer.MODULES:
            (self.source / name).write_text("# offline source " + release + "\n")
        changes = self.plan(*flags)[0]
        return installer.apply_plan(changes, self.config, self.write, self.selected)

    def uninstall(self):
        plan = installer.make_uninstall_plan(self.selected)
        backup = installer.apply_uninstall_plan(plan, self.selected, self.write)
        return plan, backup

    def capture(self):
        return {str(path): self.snapshot(path) for path in
                (self.config, self.axes, self.config.parent / installer.MANAGED,
                 self.repo / ".git/info/exclude", *(self.extras / name for name in installer.MODULES))}

    def manifest(self, backup):
        return json.loads((backup / "manifest.json").read_text())

    def test_install_and_uninstall_preserve_safe_z_probe_and_saved_config_bytes(self):
        probe_home, _saved = HostFixture.cartographer_safe_z_fixture(self)
        original = self.capture()
        probe_original = probe_home.read_bytes()
        self.install()
        self.assertEqual(probe_home.read_bytes(), probe_original)
        self.assertIn(b"[scanner]", self.config.read_bytes())
        self.uninstall()
        self.assertEqual(self.capture(), original)
        self.assertEqual(probe_home.read_bytes(), probe_original)

    def test_uninstall_preserves_later_safe_z_settings_and_probe_model_edits(self):
        probe_home, _saved = HostFixture.cartographer_safe_z_fixture(self)
        self.install()
        edited_probe = probe_home.read_bytes().replace(b"z_hop: 15", b"z_hop: 20")
        probe_home.write_bytes(edited_probe)
        self.config.write_bytes(self.config.read_bytes().replace(b"model_coef = 1.234", b"model_coef = 9.876"))
        self.uninstall()
        self.assertEqual(probe_home.read_bytes(), edited_probe)
        self.assertIn(b"[scanner model default]", self.config.read_bytes())
        self.assertIn(b"model_coef = 9.876", self.config.read_bytes())
        self.assertNotIn(b"[include accel_homing.cfg]", self.config.read_bytes())

    def test_full_history_two_upgrades_preserves_unrelated_edits_and_is_idempotent(self):
        original = self.capture()
        first = self.install()
        self.axes.write_text(self.axes.read_text().replace("run_current: 1.2", "run_current: 1.25"))
        second = self.install("upgrade-one")
        self.config.write_text(self.config.read_text().replace("Keep my printer settings", "Keep edited printer settings"))
        third = self.install("upgrade-two")
        plan, receipt = self.uninstall()
        self.assertEqual(len(plan["history"]), 3)
        self.assertIn("run_current: 1.25", self.axes.read_text())
        self.assertIn("Keep edited printer settings", self.config.read_text())
        self.assertIn("endstop_pin: tmc2209_stepper_x:virtual_endstop", self.axes.read_text())
        self.assertIn("diag_pin: ^PB0", self.axes.read_text())
        self.assertIn("home_current: 0.6", self.axes.read_text())
        self.assertFalse((self.config.parent / installer.MANAGED).exists())
        self.assertEqual(self.capture()[str(self.repo / ".git/info/exclude")], original[str(self.repo / ".git/info/exclude")])
        self.assertTrue(all(value["kind"] == "absent" for value in self.links.values()))
        self.assertEqual(self.manifest(receipt)["operation"], "uninstall")
        self.assertTrue(all(self.manifest(item)["status"] == "installed" for item in (first, second, third)))
        again, no_backup = self.uninstall()
        self.assertEqual(again["entries"], [])
        self.assertIsNone(no_backup)

    def test_routine_calibration_restores_original_plugin_model_and_preserves_other_saved_data(self):
        self.install()
        text = self.config.read_text().replace("#*# x_threshold = 6.5", "#*# x_threshold = 9.2")
        text += ("#*# [accel_homing_model new_run]\n#*# y_threshold = 7.1\n"
                 "#*# [bed_mesh default]\n#*# points = 0.1, 0.2\n")
        text = text.replace("model_coef = 1.234", "model_coef = 9.876")
        self.config.write_text(text)
        self.uninstall()
        restored = self.config.read_text()
        self.assertIn("#*# x_threshold = 6.5", restored)
        self.assertNotIn("accel_homing_model new_run", restored)
        self.assertIn("model_coef = 9.876", restored)
        self.assertIn("#*# [bed_mesh default]\n#*# points = 0.1, 0.2\n", restored)

    def test_composed_config_upgrades_restore_both_axes_and_nested_speed_edits(self):
        self.install("package", "--axes", "x", "--homing-speed", "80")
        self.axes.write_bytes(self.axes.read_text().replace("run_current: 1.2", "run_current: 1.25").encode())
        self.install("upgrade-one", "--axes", "y", "--homing-speed", "60")
        self.install("upgrade-two", "--axes", "x", "--homing-speed", "90")
        plan, _ = self.uninstall()
        self.assertEqual(len(plan["history"]), 3)
        expected = AXES.replace("run_current: 1.2", "run_current: 1.25")
        self.assertEqual(self.axes.read_text(), expected)
        self.assertEqual(self.config.read_bytes(), (CONFIG + SAVED).encode())

    def test_calibration_when_originally_no_saved_block_removes_only_plugin_models(self):
        self.config.write_text(CONFIG)
        self.install()
        self.config.write_text(self.config.read_text() + SAVED.replace("#*# [beacon model default]\n#*# model_coef = 1.234\n", ""))
        self.uninstall()
        self.assertEqual(self.config.read_text(), CONFIG)

    def test_generated_config_tuning_is_owned_but_other_sections_fail_closed(self):
        self.install()
        managed = self.config.parent / installer.MANAGED
        managed.write_text(managed.read_text().replace("#x_threshold: 8.0", "x_threshold: 7.2"))
        before = self.capture()
        managed.write_text(managed.read_text() + "\n[gcode_macro MY_MACRO]\ngcode: M118 mine\n")
        with self.assertRaisesRegex(installer.InstallError, "unowned sections"):
            installer.make_uninstall_plan(self.selected)
        self.assertIn("MY_MACRO", managed.read_text())
        managed.write_bytes(base64.b64decode(before[str(managed)]["data"]))
        self.uninstall()
        self.assertFalse(managed.exists())

    def test_preexisting_legacy_config_links_models_and_commented_active_models_restored(self):
        active_model = "\n[accel_homing_model old]\nx_threshold: 6.1\nx_successes: 10\n"
        self.config.write_text(CONFIG + "\n[beacon_accel_homing]\nx_threshold: 6.2\ny_threshold: 6.3\n" + active_model + SAVED)
        old_config = self.config.read_bytes()
        original_source = self.home / "original-legacy-source"
        original_source.mkdir()
        for name in installer.MODULES:
            (original_source / name).write_text("# Original legacy module\n")
            self.links[self.extras / name] = {"kind": "symlink", "target": str(original_source / name)}
        original_links = {path: dict(state) for path, state in self.links.items()}
        self.install()
        self.config.write_text(self.config.read_text().replace("x_threshold: 6.1", "#x_threshold: 6.1").replace("x_successes: 10", "#x_successes: 10") +
                               "#*# [accel_homing_model old]\n#*# x_threshold = 8.5\n#*# x_successes = 10\n")
        self.uninstall()
        self.assertEqual(self.config.read_bytes(), old_config)
        self.assertEqual(self.links, original_links)

    def test_managed_line_edit_or_changed_section_refuses_before_writes(self):
        self.install()
        for old, new in (("endstop_pin: accel_homing:x", "endstop_pin: !PA4"),
                         ("[stepper_x]", "[stepper_edited]")):
            original = self.axes.read_text()
            self.axes.write_text(original.replace(old, new))
            current = self.capture()
            with self.assertRaisesRegex(installer.InstallError, "Managed lines|Managed section"):
                installer.make_uninstall_plan(self.selected)
            self.assertEqual(self.capture(), current)
            self.axes.write_text(original)

    def test_real_save_config_normalizes_crlf_and_separator_blank_lines(self):
        original = CONFIG + "\n[beacon_accel_homing]\nx_threshold: 6.2\n" + (
            "\n[accel_homing_model old]\nx_threshold: 6.1\nx_successes: 10\n") + SAVED
        self.config.write_bytes(original.replace("\n", "\r\n").encode())
        self.install()
        active = self.config.read_text().split(installer.SAVE_MARKER)[0].rstrip()
        active = active.replace("x_threshold: 6.1", "#x_threshold: 6.1").replace("x_successes: 10", "#x_successes: 10")
        saved = SAVED.lstrip("\n") + "#*# [accel_homing_model old]\n#*# x_threshold = 8.5\n#*# x_successes = 10\n"
        self.config.write_bytes((active + "\n" + saved).encode())
        self.uninstall()
        text = self.config.read_bytes().decode().replace("\r\n", "\n")
        self.assertIn("[accel_homing_model old]\nx_threshold: 6.1\nx_successes: 10\n", text)
        self.assertNotIn("#*# [accel_homing_model old]", text)
        self.assertNotIn("accel_chip:", text)

    def test_new_active_model_or_plugin_section_and_broken_restored_include_refuse(self):
        self.install()
        current = self.config.read_bytes()
        for added in ("\n[accel_homing_model manual]\nx_threshold: 6.1\n",
                      "\n[beacon_accel_homing]\nx_threshold: 6.2\n",
                      "\n[include accel_*.cfg]\n"):
            text = current.decode().replace(installer.SAVE_MARKER, added + installer.SAVE_MARKER)
            self.config.write_bytes(text.encode())
            before = self.capture()
            with self.assertRaisesRegex(installer.InstallError, "New/unowned active|Missing include"):
                installer.make_uninstall_plan(self.selected)
            self.assertEqual(self.capture(), before)
        self.config.write_bytes(current)

    def test_missing_original_link_source_refuses_but_deleted_intermediate_release_does_not(self):
        for name in installer.MODULES:
            self.links[self.extras / name] = {"kind": "symlink", "target": "../../../legacy/" + name}
        self.install()
        current = self.capture()
        with self.assertRaisesRegex(installer.InstallError, "Original plugin source is missing"):
            installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), current)
        # Recover the original source and use two later releases; intermediate
        # source deletion does not matter when its link is absent in the result.
        legacy = self.repo / "klippy/extras/../../../legacy"
        legacy.mkdir(parents=True, exist_ok=True)
        for name in installer.MODULES:
            (legacy / name).write_text("# recovered original\n")
        self.install("upgrade")
        shutil.rmtree(self.home / "package")
        self.uninstall()
        self.assertTrue(all(state.get("target", "").startswith("../../../legacy/") for state in self.links.values()))

    def test_unchanged_included_context_is_rechecked_before_uninstall_mutation(self):
        untouched = self.config.parent / "unchanged.cfg"
        untouched.write_text("[gcode_macro USER_MACRO]\ngcode: M118 mine\n")
        self.config.write_bytes((CONFIG + "\n[include unchanged.cfg]\n" + SAVED).encode())
        self.install()
        plan = installer.make_uninstall_plan(self.selected)
        self.assertNotIn(str(untouched), {entry["path"] for entry in plan["entries"]})
        untouched.write_text("[accel_homing_model new_unreviewed]\nx_threshold: 7.1\n")
        with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
            installer.apply_uninstall_plan(plan, self.selected, self.write)

    def test_changed_link_refuses_and_missing_history_fails_before_writes(self):
        first = self.install()
        second = self.install("upgrade")
        self.links[self.extras / installer.MODULES[0]]["target"] = "/somewhere/manual.py"
        current = self.capture()
        with self.assertRaisesRegex(installer.InstallError, "Managed link"):
            installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), current)
        shutil.rmtree(first)
        with self.assertRaisesRegex(installer.InstallError, "gapped"):
            installer.make_uninstall_plan(self.selected)
        self.assertTrue(second.is_dir())

    def test_uninstall_wrong_checkout_unknown_path_and_edited_manifest_refuse(self):
        first = self.install()
        with self.assertRaisesRegex(installer.InstallError, "different Klipper checkout"):
            installer.make_uninstall_plan(dict(self.selected, repo=self.home / "other-klipper"))
        manifest = self.manifest(first)
        manifest["changes"][0]["path"] = str(self.home / "unrelated.cfg")
        (first / "manifest.json").write_text(json.dumps(manifest))
        current = self.capture()
        with self.assertRaisesRegex(installer.InstallError, "outside selected instance"):
            installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), current)

    def test_v2_unchanged_main_baseline_handles_wildcard_calibration_and_v1_refuses_unknown_models(self):
        self.config.write_text(CONFIG + "\n[include accel_*.cfg]\n" + SAVED)
        (self.config.parent / "accel_existing.cfg").write_text("# Original include\n")
        first = self.install()
        self.assertNotIn(str(self.config), {entry["path"] for entry in self.manifest(first)["changes"]})
        self.config.write_text(self.config.read_text() + "#*# [accel_homing_model later]\n#*# x_threshold = 7.1\n")
        plan = installer.make_uninstall_plan(self.selected)
        self.assertTrue(plan["entries"])
        manifest = self.manifest(first)
        for key in ("context", "repo", "parent", "previous_uninstall", "operation"):
            manifest.pop(key, None)
        manifest["version"] = 1
        (first / "manifest.json").write_text(json.dumps(manifest))
        current = self.capture()
        with self.assertRaisesRegex(installer.InstallError, "no original main-config/model baseline"):
            installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), current)

    def test_v1_complete_install_and_upgrade_restores_original_state(self):
        before = self.capture()
        for name in ("package", "upgrade"):
            directory = self.install(name)
            manifest = self.manifest(directory)
            for key in ("context", "repo", "parent", "previous_uninstall", "operation"):
                manifest.pop(key, None)
            manifest["version"] = 1
            (directory / "manifest.json").write_text(json.dumps(manifest))
        self.uninstall()
        self.assertEqual(self.capture(), before)

    def test_uninstall_failure_recovers_entire_active_state_and_retry_works(self):
        self.install()
        self.install("upgrade")
        before = self.capture()
        plan = installer.make_uninstall_plan(self.selected)
        calls = []
        def fail_second(path, state):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("offline restore failure")
            self.write(path, state)
        with self.assertRaisesRegex(installer.InstallError, "Uninstall failed; applied changes restored"):
            installer.apply_uninstall_plan(plan, self.selected, fail_second)
        self.assertEqual(self.capture(), before)
        failed = sorted((self.config.parent / ".accel_homing_backups").iterdir())[-1]
        self.assertEqual(self.manifest(failed)["status"], "rolled_back_after_failure")
        self.uninstall()

    def test_partial_failure_and_concurrent_edit_report_recovery_without_overwrite(self):
        self.install()
        plan = installer.make_uninstall_plan(self.selected)
        first_path = Path(plan["entries"][0]["path"])
        calls = []
        def fail(path, state):
            calls.append(path)
            if len(calls) == 2:
                if self.is_module(first_path):
                    self.links[first_path] = {"kind": "symlink", "target": "/concurrent/manual.py"}
                else:
                    first_path.write_text("# concurrent edit during failed uninstall\n")
                raise OSError("offline restore failure")
            self.write(path, state)
        with self.assertRaisesRegex(installer.InstallError, "rollback needs repair"):
            installer.apply_uninstall_plan(plan, self.selected, fail)
        failed = sorted((self.config.parent / ".accel_homing_backups").iterdir())[-1]
        self.assertEqual(self.manifest(failed)["status"], "rollback_incomplete")
        with self.assertRaisesRegex(installer.InstallError, "Incomplete/unknown transaction"):
            installer.make_uninstall_plan(self.selected)

    def test_successful_uninstall_can_be_precisely_rolled_back_then_uninstalled_again(self):
        self.install()
        self.install("upgrade")
        installed = self.capture()
        _, backup = self.uninstall()
        installer.rollback(backup, selected=self.selected)
        self.assertEqual(self.capture(), installed)
        self.assertEqual(len(installer.make_uninstall_plan(self.selected)["history"]), 2)
        self.uninstall()

    def test_new_install_after_uninstall_has_new_baseline(self):
        self.install()
        self.uninstall()
        self.axes.write_text(self.axes.read_text().replace("run_current: 1.2", "run_current: 1.3"))
        self.install("new-installation")
        plan, _ = self.uninstall()
        self.assertEqual(len(plan["history"]), 1)
        self.assertIn("run_current: 1.3", self.axes.read_text())

    def test_uninstall_plan_checks_concurrent_config_history_and_instance_before_writes(self):
        self.install()
        plan = installer.make_uninstall_plan(self.selected)
        with self.assertRaisesRegex(installer.InstallError, "different instance"):
            installer.apply_uninstall_plan(plan, dict(self.selected, config=self.home / "wrong.cfg"), self.write)
        self.axes.write_text(self.axes.read_text() + "# concurrent edit\n")
        with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
            installer.apply_uninstall_plan(plan, self.selected, self.write)

    def test_cli_uninstall_dry_run_and_idle_failure_do_not_write_or_install(self):
        self.install()
        before = self.capture()
        with mock.patch.object(installer, "discover_target", return_value=self.selected), \
             mock.patch.object(installer.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(installer, "assert_idle", side_effect=installer.InstallError("printing")) as idle, \
             mock.patch.object(installer, "make_plan", side_effect=AssertionError("must never install")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--uninstall", "--dry-run"]), 0)
            idle.assert_not_called()
            with self.assertRaisesRegex(installer.InstallError, "printing"):
                installer.main(["--uninstall"])
        self.assertEqual(self.capture(), before)

    def test_other_shared_checkout_plugin_consumer_refuses_before_writes(self):
        self.install()
        peer_config = self.home / "printer_b/config/printer.cfg"
        peer_config.parent.mkdir(parents=True)
        peer_config.write_text("[printer]\nkinematics: corexy\n[accel_homing]\naccel_chip: beacon\n")
        peer = dict(self.selected, config=peer_config, pid=456, service="klipper-b.service", origin="offline peer")
        before = self.capture()
        with mock.patch.object(installer, "proc_instances", return_value=[peer]), \
             mock.patch.object(installer, "service_instances", return_value=[]):
            with self.assertRaisesRegex(installer.InstallError, "shared checkout module links.*printer_b"):
                installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), before)
        peer_config.write_text("[include missing-config.cfg]\n")
        with mock.patch.object(installer, "proc_instances", return_value=[]), \
             mock.patch.object(installer, "service_instances", return_value=[dict(peer, pid=None)]):
            with self.assertRaisesRegex(installer.InstallError, "cannot resolve this shared checkout consumer"):
                installer.make_uninstall_plan(self.selected)
        self.assertEqual(self.capture(), before)

    def test_peer_without_plugin_is_safe_but_new_consumer_after_planning_refuses(self):
        self.install()
        peer_config = self.home / "plain-printer.cfg"
        peer_config.write_text("[printer]\nkinematics: corexy\n")
        peer = dict(self.selected, config=peer_config, pid=None, service="klipper-b.service", origin="offline service")
        with mock.patch.object(installer, "proc_instances", return_value=[]), \
             mock.patch.object(installer, "service_instances", return_value=[peer]):
            plan = installer.make_uninstall_plan(self.selected)
            peer_config.write_text("[accel_homing_model old]\nx_threshold: 6.1\n")
            before = self.capture()
            with self.assertRaisesRegex(installer.InstallError, "shared checkout module links"):
                installer.apply_uninstall_plan(plan, self.selected, self.write)
            self.assertEqual(self.capture(), before)

    def test_pristine_host_without_any_history_is_already_uninstalled(self):
        plan, backup = self.uninstall()
        self.assertEqual(plan["entries"], [])
        self.assertIsNone(backup)
        self.assertFalse((self.config.parent / ".accel_homing_backups").exists())


class UninstallBootstrapFixture(unittest.TestCase):
    setUp = BootstrapFixture.setUp
    fetch = BootstrapFixture.fetch
    def embedded(self):
        return (DIRECTORY / "uninstall.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    def response(self, url, **kwargs):
        url = getattr(url, "full_url", url)
        self.urls.append(url)
        data = (DIRECTORY / "bootstrap.py").read_bytes()
        if "/commits/" in url:
            payload = json.dumps({"sha": self.sha}).encode()
        elif "/contents/" in url:
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            payload = json.dumps(dict(type="file", size=len(data), sha=blob)).encode()
        else:
            payload = data
        result = io.BytesIO(payload)
        result.headers = {"Content-Length": str(len(payload))}
        return result

    def run_entry(self, source, *args, response=None):
        with mock.patch.object(bootstrap.urllib.request, "urlopen", side_effect=response or self.response), \
             mock.patch.object(bootstrap.sys, "argv", ["uninstall.sh", str(source)] + list(args)), \
             mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            exec(compile(self.embedded(), "uninstall.sh embedded Python", "exec"), {})
        return stopped.exception.code, run

    def test_standalone_with_arbitrary_sibling_installer_always_verifies_and_forces_uninstall(self):
        self.destination.mkdir()
        (self.destination / "installer.py").write_text("raise AssertionError('untrusted sibling')\n")
        code, run = self.run_entry(self.destination, "--config", "/offline/printer.cfg", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(self.urls)
        command = run.call_args.args[0]
        self.assertIn("--uninstall", command)
        self.assertEqual(command[2:], ["--repo", "FrankRamm/MISC", "--ref", self.sha,
                                      "--uninstall", "--config", "/offline/printer.cfg", "--dry-run"])
        self.assertNotEqual(command[1], str(self.destination / "installer.py"))

    def test_complete_verified_local_package_uses_exact_sibling_uninstall_offline(self):
        self.data = {name: (DIRECTORY / name).read_bytes() for name in bootstrap.FILES}
        source = bootstrap.prepare_release("example/project", self.sha, self.destination, self.fetch)
        self.urls.clear()
        code, run = self.run_entry(source, "--dry-run", response=AssertionError("must stay offline"))
        self.assertEqual(code, 0)
        self.assertEqual(self.urls, [])
        run.assert_called_once_with([bootstrap.sys.executable, str(source / "installer.py"), "--uninstall", "--dry-run"])

    def test_uninstall_conflicting_rollback_aborts_before_download_or_dispatch(self):
        code, run = self.run_entry(self.destination, "--rollback", "/offline/backup")
        self.assertEqual(code, 1)
        self.assertEqual(self.urls, [])
        run.assert_not_called()

    def test_uninstall_download_failure_or_valid_prefix_truncation_never_dispatches(self):
        for failure in (OSError("offline HTTP failure"), "truncate"):
            self.urls.clear()
            def response(url, **kwargs):
                address = getattr(url, "full_url", url)
                if "raw.githubusercontent.com" in address:
                    if isinstance(failure, Exception):
                        raise failure
                    result = io.BytesIO(b"# valid prefix\n")
                    result.headers = {}
                    return result
                return self.response(url, **kwargs)
            code, run = self.run_entry(self.destination, response=response)
            self.assertEqual(code, 1)
            run.assert_not_called()

    def test_uninstall_bootstrap_preserves_forced_mode_after_release_download(self):
        release = self.destination / "verified-release"
        with mock.patch.object(bootstrap.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(bootstrap, "prepare_release", return_value=release), \
             mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(["--uninstall", "--dry-run"]), 0)
        run.assert_called_once_with([bootstrap.sys.executable, str(release / "installer.py"), "--uninstall", "--dry-run"])

    def test_uninstall_shell_truncation_cannot_drop_forced_mode(self):
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("Bash unavailable for syntax-only fixture")
        script = (DIRECTORY / "uninstall.sh").read_text()
        invocation = script.rindex("\n(\n") + 1
        full = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(full.returncode, 0, full.stderr)
        for end in (invocation + 1, script.rindex(' "$@"'), script.rindex("\n)")):
            truncated = subprocess.run([bash, "-n"], input=script[:end], text=True, capture_output=True)
            self.assertNotEqual(truncated.returncode, 0)


if __name__ == "__main__":
    unittest.main()
