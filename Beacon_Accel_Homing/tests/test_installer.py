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

    def test_homing_overrides_and_safe_z_home_fail_closed(self):
        for section in ("homing_override", "safe_z_home", "gcode_macro G28"):
            self.config.write_text(CONFIG + "\n[" + section + "]\ngcode:\n    G28\n")
            with self.assertRaisesRegex(installer.InstallError, "Custom homing path"):
                self.plan()
            self.plan("--acknowledge-homing-override")

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


if __name__ == "__main__":
    unittest.main()
