"""Offline Moonraker registration/source transaction fixtures; never a host install."""
import base64
import copy
import contextlib
import hashlib
import json
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import test_installer as installer_tests

installer = installer_tests.installer
updater = installer_tests.load("accel_homing_updater")


class UpdaterFixture(unittest.TestCase):
    plan = installer_tests.HostFixture.plan

    def setUp(self):
        installer_tests.HostFixture.setUp(self)
        installer.load_updater.return_value = updater
        home = mock.patch.object(updater.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.selected["socket"] = self.home / "klippy.sock"
        self.data = self.home / "printer_data_moon"
        self.moon_config = self.data / "config/moonraker.conf"
        self.moon_config.parent.mkdir(parents=True)
        self.moon_config.write_text("[server]\nklippy_uds_address: %s\n[update_manager]\n" % self.selected["socket"])
        self.commit = "a" * 40
        self.marker()
        self.refresh()
        self.instance_discovery = updater.moonraker_instances
        instances = mock.patch.object(updater, "moonraker_instances", side_effect=lambda api: [self.moon])
        instances.start()
        self.addCleanup(instances.stop)

    def marker(self):
        hashes = {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest() for name in installer.MODULES}
        (self.source / ".source.json").write_text(json.dumps(dict(repo="FrankRamm/MISC", commit=self.commit, sha256=hashes)))

    def refresh(self):
        self.moon = updater._moon_command(
            [str(self.home / "moon-env/bin/python"), str(self.home / "moonraker/moonraker.py"), "-d", str(self.data)],
            self.home, installer.uninstall_api(), pid=77, service="moonraker-test.service", scope="system", start="100")

    def updater_plan(self):
        return updater.make_plan(self.source, self.selected, installer.arguments([]), installer.uninstall_api())

    def install_config(self, plan):
        changes = {path: value for path, value in self.plan()[0].items() if value["kind"] != "symlink"}
        changes.update(plan["changes"])
        backup = installer.apply_plan(changes, self.config, selected=self.selected, updater=plan)
        self.refresh()
        return backup

    def legacy_plan(self):
        """Reproduce the old include-layout journal without migrating it."""
        plan = self.updater_plan()
        include = self.moon_config.parent / updater.INCLUDE
        suffix = updater.main_suffix(False, True)
        context = {key: value for key, value in plan["context"].items() if key != "layout"}
        context.update(include_file=str(include), append=base64.b64encode(suffix).decode())
        receipt = (json.dumps(dict(version=1, instance=updater.instance_key(self.selected),
                                   commit=self.commit, **context), sort_keys=True, indent=2) + "\n").encode()
        plan["context"] = context
        plan["changes"] = {
            include: dict(kind="file", data=updater.stanza(plan["repo"])),
            self.moon_config: dict(kind="file", data=self.moon_config.read_bytes() + suffix),
            plan["receipt"]: dict(kind="file", data=receipt),
        }
        for path, change in plan["changes"].items():
            change["expected"] = installer.snapshot(path)
        return plan

    def test_live_moonraker_paths_follow_argv_environment_and_verified_owner_home(self):
        owner = self.home / "moonraker-owner"
        default_data = owner / "printer_data"
        default_config = default_data / "config/moonraker.conf"
        default_config.parent.mkdir(parents=True)
        default_config.write_bytes(self.moon_config.read_bytes())
        alternate = self.home / "alternate-data"
        alternate.mkdir()
        custom = self.home / "custom-moonraker.conf"
        custom.write_bytes(self.moon_config.read_bytes())
        argv = [str(self.home / "moon-env/bin/python"), "-m", "moonraker"]
        cases = (
            ([], {"HOME": str(owner), "MOONRAKER_DATA_PATH": str(self.data)}, self.data, self.moon_config),
            ([], {"HOME": str(owner)}, default_data, default_config),
            ([], {}, default_data, default_config),
            ([], {"HOME": str(owner), "MOONRAKER_DATA_PATH": "~/printer_data"}, default_data, default_config),
            (["-d", str(alternate), "--datapath", str(self.data)],
             {"HOME": str(owner), "MOONRAKER_DATA_PATH": str(alternate)}, self.data, self.moon_config),
            (["--datapath="], {"HOME": str(owner), "MOONRAKER_DATA_PATH": str(self.data)}, default_data, default_config),
            (["-d", str(self.data)], {"HOME": str(owner), "MOONRAKER_CONFIG_PATH": str(custom)}, self.data, custom),
            (["-d", str(self.data), "--configfile", str(self.moon_config)],
             {"HOME": str(owner), "MOONRAKER_CONFIG_PATH": str(custom)}, self.data, self.moon_config),
        )
        for flags, environment, data, config in cases:
            with self.subTest(flags=flags, environment_keys=sorted(environment)):
                found = updater._moon_command(argv + flags, self.home, installer.uninstall_api(), pid=77,
                                              path_environment=environment, owner_home=str(owner))
                self.assertIsNotNone(found)
                self.assertEqual(found["data"], data)
                self.assertEqual(found["config"], config)
        # Moonraker treats an empty config as a supplied path, not its default.
        self.assertIsNone(updater._moon_command(argv, self.home, installer.uninstall_api(), pid=77,
                                               path_environment={"HOME": str(owner), "MOONRAKER_CONFIG_PATH": ""},
                                               owner_home=str(owner)))
        # No live environment proof must not create an assumed default instance.
        self.assertIsNone(updater._moon_command(argv, self.home, installer.uninstall_api(), pid=77))

    def test_moonraker_environment_reader_retains_only_bounded_path_inputs(self):
        process = self.home / "synthetic-process"
        process.mkdir()
        path = process / "environ"
        path.write_bytes(b"HOME=/verified/home\0MOONRAKER_DATA_PATH=/verified/data\0"
                         b"MOONRAKER_CONFIG_PATH=/verified/moonraker.conf\0"
                         b"SECRET_TOKEN=must-not-be-retained\0MOONRAKER_LOG_PATH=/ignored/log\0")
        self.assertEqual(updater._path_environment(process), {
            "HOME": "/verified/home", "MOONRAKER_DATA_PATH": "/verified/data",
            "MOONRAKER_CONFIG_PATH": "/verified/moonraker.conf"})
        path.write_bytes(b"HOME=/one\0HOME=/two\0")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            updater._path_environment(process)
        path.write_bytes(b"IGNORED=" + b"x" * 65536)
        with self.assertRaisesRegex(ValueError, "bound"):
            updater._path_environment(process)

    def test_omitted_klippy_socket_uses_moonraker_tmp_default_without_alternate_probing(self):
        self.moon_config.write_text("[server]\n[update_manager]\n")
        self.refresh()
        expected = installer.canonical("/tmp/klippy_uds", self.home)
        self.assertEqual(self.moon["socket"], expected)
        selected = {**self.selected, "socket": expected}
        self.assertIs(updater.discover_moonraker(selected, installer.uninstall_api(), instances=[self.moon]), self.moon)
        explicit = self.data / "comms/custom-klippy.sock"
        self.moon_config.write_text("[server]\nklippy_uds_address: %s\n" % explicit)
        self.refresh()
        self.assertEqual(self.moon["socket"], explicit)
        with self.assertRaisesRegex(installer.InstallError, "socket mismatch"):
            updater.discover_moonraker(selected, installer.uninstall_api(), instances=[self.moon])

    def test_configured_socket_paths_resolve_cwd_and_literal_data_path_without_defaulting_empty(self):
        cwd = self.home / "moonraker-working-directory"
        cwd.mkdir()
        argv = [str(self.home / "moon-env/bin/python"), "-m", "moonraker", "-d", str(self.data)]
        cases = (("comms/klippy.sock", cwd / "comms/klippy.sock"),
                 ("{data_path}/comms/klippy.sock", self.data / "comms/klippy.sock"),
                 ("{ data_path }/comms/klippy.sock", self.data / "comms/klippy.sock"),
                 ("", cwd))
        for configured, expected in cases:
            with self.subTest(configured=configured):
                self.moon_config.write_text("[server]\nklippy_uds_address: %s\n" % configured)
                found = updater._moon_command(argv, cwd, installer.uninstall_api(), pid=77,
                                              path_environment={"HOME": str(self.home)}, owner_home=str(self.home))
                self.assertIsNotNone(found)
                self.assertEqual(found["socket"], expected)

    def test_no_match_diagnostics_identify_parser_template_and_socket_stages_without_payloads(self):
        argv = [str(self.home / "moon-env/bin/python"), "-m", "moonraker", "-d", str(self.data)]
        for body, reason in (("[server]\nklippy_uds_address: {secrets.private_socket}\n", "socket expression is unsupported"),
                             ("[server]\n[include missing.cfg]\n", "include pattern has no matches")):
            with self.subTest(reason=reason):
                self.moon_config.write_text(body)
                notes = []
                self.assertIsNone(updater._moon_command(argv, self.home, installer.uninstall_api(), pid=77,
                                                       diagnostics=notes))
                self.assertIn(reason, "\n".join(notes))
                self.assertNotIn("secrets.private_socket", "\n".join(notes))
        self.moon_config.write_text("[server]\nklippy_uds_address: %s\n" % self.selected["socket"])
        self.refresh()
        selected = {**self.selected, "socket": self.home / "wanted\nsocket"}
        with self.assertRaises(installer.InstallError) as stopped:
            updater.discover_moonraker(selected, installer.uninstall_api(), instances=[self.moon])
        message = str(stopped.exception)
        self.assertIn("Selected Klipper config=", message)
        self.assertIn("PID=77", message)
        self.assertIn("socket mismatch", message)
        self.assertIn("wanted\\nsocket", message)
        self.assertNotIn("wanted\nsocket", message)

    def test_proc_rejections_report_unreadable_environment_or_wrong_uid_without_secret_errors(self):
        proc = self.home / "synthetic-proc"
        process = proc / "77"
        (process / "cwd").mkdir(parents=True)
        units = self.home / "empty-system-units"
        units.mkdir()
        (process / "cmdline").write_bytes(b"python\0-m\0moonraker\0-d\0" + str(self.data).encode() + b"\0")
        uid = process.stat().st_uid
        with mock.patch.object(updater.os, "getuid", return_value=uid, create=True), \
                mock.patch.object(updater, "_path_environment", side_effect=PermissionError("SECRET_VALUE must not escape")):
            found = self.instance_discovery(installer.uninstall_api(), proc, [units])
        with self.assertRaises(installer.InstallError) as stopped:
            updater.discover_moonraker(self.selected, installer.uninstall_api(), instances=found)
        self.assertIn("bounded path environment unavailable (PermissionError)", str(stopped.exception))
        self.assertNotIn("SECRET_VALUE", str(stopped.exception))
        with mock.patch.object(updater.os, "getuid", return_value=uid + 1, create=True), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("foreign process arguments must not be read")), \
                mock.patch.object(updater, "_path_environment") as environment:
            found = self.instance_discovery(installer.uninstall_api(), proc, [units])
            environment.assert_not_called()
        with self.assertRaises(installer.InstallError) as stopped:
            updater.discover_moonraker(self.selected, installer.uninstall_api(), instances=found)
        self.assertIn("skipped 1 processes owned by other UIDs", str(stopped.exception))

    def test_environment_based_instances_still_require_matching_socket_unique_pid_and_stable_inputs(self):
        argv = [str(self.home / "moon-env/bin/python"), "-m", "moonraker"]
        environment = {"HOME": str(self.home), "MOONRAKER_DATA_PATH": str(self.data)}
        found = updater._moon_command(argv, self.home, installer.uninstall_api(), pid=77,
                                      service="moonraker-test.service", scope="system", start="100",
                                      path_environment=environment, owner_home=str(self.home))
        other = {**found, "pid": 78, "socket": self.home / "other-klippy.sock"}
        self.assertIs(updater.discover_moonraker(self.selected, installer.uninstall_api(), instances=[found, other]), found)
        with self.assertRaisesRegex(installer.InstallError, "multiple live"):
            updater.discover_moonraker(self.selected, installer.uninstall_api(), instances=[found, {**other, "socket": found["socket"]}])
        with self.assertRaises(installer.InstallError):
            updater.discover_moonraker(self.selected, installer.uninstall_api(), self.home / "unrelated.conf", [found])
        changes = ({**found, "path_environment": {**environment, "HOME": str(self.home / "changed-home")}},
                   {**found, "cwd": str(self.home / "changed-working-directory")})
        for changed in changes:
            with mock.patch.object(updater, "discover_moonraker", return_value=changed):
                with self.assertRaisesRegex(installer.InstallError, "changed"):
                    updater.assert_identity(found, self.selected, installer.uninstall_api())

    def test_literal_loaded_service_reuses_verified_live_environment_path_context(self):
        argv = [str(self.home / "moon-env/bin/python"), "-m", "moonraker"]
        found = updater._moon_command(argv, self.home, installer.uninstall_api(), pid=77,
                                      service="moonraker-test.service", scope="system", start="100",
                                      path_environment={"HOME": str(self.home), "MOONRAKER_DATA_PATH": str(self.data)},
                                      owner_home=str(self.home))
        properties = dict(ActiveState="active", SubState="running", MainPID="77", User="fixture",
                          WorkingDirectory=str(self.home),
                          ExecStart="{ path=%s ; argv[]=%s ; ignore_errors=no ; }" % (
                              argv[0], shlex.join(argv)))
        with mock.patch.object(updater, "discover_moonraker", return_value=found), \
                mock.patch.object(updater, "_current_users", return_value={"fixture"}), \
                mock.patch.object(installer, "read_service_unit", return_value=properties):
            identity = updater.service_identity(found, self.selected, installer.uninstall_api(), "systemctl")
        self.assertEqual(identity["instance"]["config"], str(self.moon_config))
        self.assertEqual(identity["instance"]["path_environment"], found["path_environment"])

    def test_exact_git_transport_is_journalled_directly_without_include_env_or_hooks(self):
        plan = self.updater_plan()
        text = plan["changes"][self.moon_config]["data"].decode()
        self.assertIn("type: git_repo\nchannel: dev", text)
        self.assertIn("primary_branch: codex/universal-accel-homing", text)
        self.assertIn("managed_services: klipper", text)
        self.assertNotIn("install_script", text)
        self.assertNotIn("requirements", text)
        self.assertNotIn("virtualenv", text)
        self.assertEqual(plan["context"]["layout"], "direct")
        self.assertNotIn("include_file", plan["context"])
        self.assertNotIn(self.moon_config.parent / updater.INCLUDE, plan["changes"])
        self.assertTrue(all("expected" in value for value in plan["changes"].values()))
        self.assertNotEqual(updater.managed_path(self.selected), updater.managed_path(
            {**self.selected, "config": self.home / "other.cfg"}))
        backup = self.install_config(plan)
        manifest = json.loads((backup / "manifest.json").read_text())
        allowed = updater.validate_record(manifest, self.selected, installer.uninstall_api())
        self.assertEqual(set(allowed), {self.moon_config, self.config.parent / updater.RECEIPT})

    def test_new_direct_registration_adds_one_missing_base_and_preserves_original_bytes(self):
        original = self.moon_config.read_bytes().replace(b"[update_manager]\n", b"")
        original += b"[unrelated]\nkeep: yes\n# operator comment\n"
        self.moon_config.write_bytes(original)
        self.refresh()
        plan = self.updater_plan()
        rendered = plan["changes"][self.moon_config]["data"]
        self.assertEqual(rendered, original + updater.direct_suffix(plan["repo"], True, True))
        self.assertEqual(rendered.count(b"[update_manager]\n"), 1)
        self.assertEqual(rendered.count(b"[update_manager accel_homing]\n"), 1)

    def test_skipped_or_failed_activation_retries_even_when_configuration_unchanged(self):
        first = self.updater_plan()
        backup = self.install_config(first)
        for status in ("skipped", "pending", "failed"):
            with self.subTest(status=status):
                updater.record_activation(first, self.selected, installer.uninstall_api(), backup, status)
                repeated = self.updater_plan()
                self.assertEqual(repeated["changes"], {})
                self.assertTrue(repeated["restart_needed"])
        updater.record_activation(first, self.selected, installer.uninstall_api(), backup, "restarted")
        repeated = self.updater_plan()
        self.assertFalse(repeated["restart_needed"])
        self.assertEqual(self.moon_config.read_text().count("[update_manager accel_homing]"), 1)
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())

    def test_conflicting_manual_stanzas_and_duplicate_registration_or_base_are_rejected(self):
        original = self.moon_config.read_text()
        manual = updater.stanza(updater.managed_path(self.selected)).decode()
        cases = ("[update_manager accel_homing]\npath: /foreign\n",
                 manual + "install_script: install.sh\n",
                 manual + "malformed continuation\n",
                 manual.replace("managed_services: klipper", " managed_services: klipper"),
                 manual.replace("[update_manager accel_homing]", "[UPDATE_MANAGER ACCEL_HOMING]"),
                 manual.replace("[update_manager accel_homing]", "[ update_manager accel_homing ]"),
                 manual + "managed_services: klipper\n",
                 manual + manual,
                 "[update_manager]\n",
                 "enable_auto_refresh: True\nenable_auto_refresh: False\n")
        for suffix in cases:
            with self.subTest(suffix=suffix):
                self.moon_config.write_text(original + suffix)
                self.refresh()
                with self.assertRaises(installer.InstallError):
                    self.updater_plan()

    def test_new_direct_registration_does_not_create_a_file_matched_by_existing_wildcards(self):
        self.moon_config.write_text(self.moon_config.read_text() + "[include *update.cfg]\n[include accel_*.cfg]\n")
        (self.moon_config.parent / "otherupdate.cfg").write_text("[existing]\n")
        (self.moon_config.parent / "accel_other.cfg").write_text("[another]\n")
        self.refresh()
        plan = self.updater_plan()
        self.assertIn(self.moon_config, plan["changes"])
        self.assertNotIn(self.moon_config.parent / updater.INCLUDE, plan["changes"])
        self.assertNotIn(b"[include accel_homing_update.cfg]", plan["changes"][self.moon_config]["data"])

    def test_concurrent_moonraker_main_edit_is_not_adopted_or_overwritten(self):
        plan = self.updater_plan()
        current = self.moon_config.read_bytes() + b"[operator]\nkeep: yes\n"
        self.moon_config.write_bytes(current)
        with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
            installer.apply_plan(plan["changes"], self.config, selected=self.selected, updater=plan)
        self.assertEqual(self.moon_config.read_bytes(), current)
        self.assertFalse((self.config.parent / ".accel_homing_backups").exists())

    def test_matching_manual_main_entry_is_preserved_through_rerun_and_uninstall(self):
        manual = self.moon_config.read_bytes() + b"\n# operator updater\n" + updater.stanza(
            updater.managed_path(self.selected)).split(b"\n", 1)[1]
        self.moon_config.write_bytes(manual)
        self.refresh()
        plan = self.updater_plan()
        self.assertNotIn(self.moon_config, plan["changes"])
        self.assertEqual(plan["context"]["append"], "")
        self.install_config(plan)
        self.assertEqual(self.updater_plan()["changes"], {})
        current = manual + b"\n[unrelated]\nkeep: yes\n"
        self.moon_config.write_bytes(current)
        self.refresh()
        with mock.patch.object(installer, "proc_instances", return_value=[]), \
                mock.patch.object(installer, "service_instances", return_value=[]):
            restore = installer.make_uninstall_plan(self.selected)
            installer.apply_uninstall_plan(restore, self.selected)
        self.assertEqual(self.moon_config.read_bytes(), current)
        self.assertFalse((self.config.parent / updater.RECEIPT).exists())

    def test_owned_direct_append_edit_cannot_be_adopted_as_a_manual_entry(self):
        self.install_config(self.updater_plan())
        changed = self.moon_config.read_bytes().replace(
            b"# Managed by the accelerometer homing installer.", b"# operator changed this owned comment")
        self.moon_config.write_bytes(changed)
        self.refresh()
        with self.assertRaisesRegex(installer.InstallError, "owned Moonraker.*changed/missing"):
            self.updater_plan()
        self.assertEqual(self.moon_config.read_bytes(), changed)

    def test_existing_legacy_include_journal_upgrades_and_uninstalls_without_migration(self):
        original = self.moon_config.read_bytes()
        legacy = self.legacy_plan()
        backup = self.install_config(legacy)
        manifest = json.loads((backup / "manifest.json").read_text())
        allowed = updater.validate_record(manifest, self.selected, installer.uninstall_api())
        self.assertEqual(set(allowed), {self.moon_config, self.moon_config.parent / updater.INCLUDE,
                                       self.config.parent / updater.RECEIPT})
        installed = self.moon_config.read_bytes()
        self.assertEqual(self.updater_plan()["changes"], {})
        self.commit = "b" * 40
        self.marker()
        upgrade = self.updater_plan()
        self.assertNotIn("layout", upgrade["context"])
        self.assertEqual(set(upgrade["changes"]), {self.config.parent / updater.RECEIPT})
        self.install_config(upgrade)
        self.assertEqual(self.moon_config.read_bytes(), installed)
        self.assertEqual((self.moon_config.parent / updater.INCLUDE).read_bytes(), updater.stanza(legacy["repo"]))
        unrelated = b"\n[unrelated]\nkeep: yes\n"
        self.moon_config.write_bytes(installed + unrelated)
        self.refresh()
        with mock.patch.object(installer, "proc_instances", return_value=[]), \
                mock.patch.object(installer, "service_instances", return_value=[]):
            restore = installer.make_uninstall_plan(self.selected)
            installer.apply_uninstall_plan(restore, self.selected)
        self.assertEqual(self.moon_config.read_bytes(), original + unrelated)
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())

    def test_no_moonraker_ambiguous_and_wrong_explicit_instance_do_not_guess(self):
        for instances, explicit in (([], None), ([self.moon, {**self.moon, "pid": 78}], None),
                                     ([self.moon], self.home / "different.conf")):
            with self.subTest(instances=len(instances), explicit=explicit):
                with self.assertRaises(installer.InstallError):
                    updater.discover_moonraker(self.selected, installer.uninstall_api(), explicit, instances)

    def test_combined_transaction_failure_restores_printer_and_moonraker_files(self):
        plan = self.updater_plan()
        printer, moon = self.config.read_bytes(), self.moon_config.read_bytes()
        def fail(path, state):
            if path == self.config.parent / updater.RECEIPT and state["kind"] == "file":
                raise OSError("synthetic updater receipt write failure")
            installer.write_state(path, state)
        changes = {path: value for path, value in self.plan()[0].items() if value["kind"] != "symlink"}
        changes.update(plan["changes"])
        with self.assertRaisesRegex(installer.InstallError, "restored"):
            installer.apply_plan(changes, self.config, writer=fail, selected=self.selected, updater=plan)
        self.assertEqual(self.config.read_bytes(), printer)
        self.assertEqual(self.moon_config.read_bytes(), moon)
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())

    def test_uninstall_restores_owned_registration_preserves_unrelated_edits_and_rejects_arbitrary_paths(self):
        plan = self.updater_plan()
        original = self.moon_config.read_bytes()
        backup = self.install_config(plan)
        self.moon_config.write_bytes(self.moon_config.read_bytes() + b"\n[unrelated]\nkeep: yes\n")
        self.refresh()
        with mock.patch.object(installer, "proc_instances", return_value=[]), \
                mock.patch.object(installer, "service_instances", return_value=[]):
            restore = installer.make_uninstall_plan(self.selected)
            installer.apply_uninstall_plan(restore, self.selected)
        self.assertEqual(self.moon_config.read_bytes(), original + b"\n[unrelated]\nkeep: yes\n")
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())
        self.assertFalse((self.config.parent / updater.RECEIPT).exists())
        manifest = json.loads((backup / "manifest.json").read_text())
        malicious = copy.deepcopy(manifest)
        malicious["updater"]["managed_repo"] = str(self.home / "unowned-repo")
        with self.assertRaisesRegex(installer.InstallError, "ownership"):
            updater.validate_record(malicious, self.selected, installer.uninstall_api())

    def test_loaded_wrong_instance_and_wrong_user_service_are_rejected(self):
        properties = dict(ActiveState="inactive", SubState="dead", MainPID="0", User="fixture",
                          ExecStart="{ argv[]=%s %s -d %s ; ignore_errors=no ; }" % (
                              self.moon["executable"], self.home / "moonraker/moonraker.py", self.home / "other-data"))
        stopped = {**self.moon, "pid": None, "start_time": None}
        with mock.patch.object(updater, "discover_moonraker", return_value=stopped), \
                mock.patch.object(updater, "_current_users", return_value={"fixture"}), \
                mock.patch.object(installer, "read_service_unit", return_value=properties):
            with self.assertRaisesRegex(installer.InstallError, "effective"):
                updater.service_identity(stopped, self.selected, installer.uninstall_api(), "systemctl")
        with mock.patch.object(updater, "_current_users", return_value={"actual-user"}), \
                mock.patch.object(installer, "read_service_unit", return_value=properties):
            with self.assertRaisesRegex(installer.InstallError, "not owned"):
                updater.service_identity(stopped, self.selected, installer.uninstall_api(), "systemctl")

    @contextlib.contextmanager
    def main_fixture(self, activation_error=False, transaction_error=False):
        shutil.copyfile(installer_tests.DIRECTORY / "uninstaller.py", self.source / "uninstaller.py")
        events, links = [], {}
        real_apply, real_snapshot, real_write = installer.apply_plan, installer.snapshot, installer.write_state
        def module(path):
            return Path(path).parent == self.extras and Path(path).name in installer.MODULES
        def snapshot(path):
            return dict(links.get(Path(path), {"kind": "absent"})) if module(path) else real_snapshot(path)
        def write(path, state):
            if module(path):
                links[Path(path)] = dict(state)
            else:
                real_write(path, state)
        def apply(*args, **kwargs):
            events.append("transaction")
            if transaction_error:
                raise installer.InstallError("synthetic combined transaction failure")
            kwargs["writer"] = write
            return real_apply(*args, **kwargs)
        def source(plan, api, guard):
            guard()
            events.append("candidate")
            plan["candidate"] = self.source
            return self.source
        def publish(plan, api, guard):
            guard()
            events.append("publish")
        def activate(*args):
            events.append("moonraker")
            if activation_error:
                raise installer.InstallError("files installed; Moonraker activation pending")
        with mock.patch.object(installer, "__file__", str(self.source / "installer.py")), \
                mock.patch.object(installer.os, "geteuid", return_value=1000, create=True), \
                mock.patch.object(installer, "discover_target", return_value=dict(self.selected)), \
                mock.patch.object(installer, "proc_instances", return_value=[]), \
                mock.patch.object(installer, "service_instances", return_value=[]), \
                mock.patch.object(installer, "prepare_restart", return_value={"klippy": "guarded"}), \
                mock.patch.object(installer, "assert_restart_safe"), \
                mock.patch.object(installer, "prepare_plotting", side_effect=lambda *a, **k: events.append("plotting")), \
                mock.patch.object(installer, "activate_install", side_effect=lambda *a: events.append("klipper")), \
                mock.patch.object(installer, "apply_plan", side_effect=apply), \
                mock.patch.object(installer, "snapshot", side_effect=snapshot), \
                mock.patch.object(updater, "prepare_restart", return_value={"moon": "guarded"}), \
                mock.patch.object(updater, "assert_restart_safe"), \
                mock.patch.object(updater, "prepare_source", side_effect=source), \
                mock.patch.object(updater, "publish_source", side_effect=publish), \
                mock.patch.object(updater, "restore_publication", side_effect=lambda *a: events.append("restore-source")), \
                mock.patch.object(updater, "activate", side_effect=activate), \
                contextlib.redirect_stdout(io.StringIO()):
            yield events, links

    def test_main_validates_candidate_then_publishes_and_activates_exactly_moonraker_before_klipper(self):
        with self.main_fixture() as (events, links):
            self.assertEqual(installer.main([]), 0)
        self.assertEqual(events, ["candidate", "plotting", "publish", "transaction", "moonraker", "klipper"])
        for name in installer.MODULES:
            self.assertEqual(links[self.extras / name]["target"], str(updater.managed_path(self.selected) / "Beacon_Accel_Homing" / name))
        self.assertIn("[update_manager accel_homing]", self.moon_config.read_text())
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())

    def test_main_moonraker_activation_failure_keeps_installed_files_and_never_activates_klipper(self):
        with self.main_fixture(activation_error=True) as (events, links):
            with self.assertRaisesRegex(installer.InstallError, "Moonraker activation pending"):
                installer.main([])
        self.assertEqual(events, ["candidate", "plotting", "publish", "transaction", "moonraker"])
        self.assertEqual(len(links), len(installer.MODULES))
        self.assertIn("[update_manager accel_homing]", self.moon_config.read_text())
        self.assertFalse((self.moon_config.parent / updater.INCLUDE).exists())

    def test_main_transaction_failure_restores_source_before_any_activation(self):
        with self.main_fixture(transaction_error=True) as (events, links):
            with self.assertRaisesRegex(installer.InstallError, "combined transaction failure"):
                installer.main([])
        self.assertEqual(events, ["candidate", "plotting", "publish", "transaction", "restore-source"])
        self.assertEqual(links, {})

    def test_managed_to_snapshot_opt_out_cannot_leave_a_misleading_updater_entry(self):
        plan = self.updater_plan()
        self.install_config(plan)
        with mock.patch.object(installer, "discover_target", return_value=self.selected), \
                mock.patch.object(installer.os, "geteuid", return_value=1000, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(installer.InstallError, "Uninstall its managed registration"):
                installer.main(["--no-update-manager", "--replace-existing-links"])

    def test_actual_activation_receipt_and_exact_service_command_and_manual_advice(self):
        plan = self.updater_plan()
        backup = self.install_config(plan)
        old = updater._fingerprint(plan["moon"])
        restart = dict(sudo="sudo", systemctl="systemctl", identity={"instance": old},
                       service_command=["systemctl", "--system", "--no-ask-password", "restart", "--", "moonraker-test.service"])
        new = {"instance": {**old, "pid": 88, "start_time": "200"}, "unit": {}}
        with mock.patch.object(updater, "assert_restart_safe"), \
                mock.patch.object(updater, "service_identity", return_value=new), \
                mock.patch.object(installer, "service_command", return_value=types.SimpleNamespace(returncode=0)) as command, \
                contextlib.redirect_stdout(io.StringIO()):
            updater.activate(plan, self.selected, restart, installer.uninstall_api(), backup)
        self.assertEqual(command.call_args.args[0], ["sudo", "-n", "--"] + restart["service_command"])
        self.assertEqual(json.loads((backup / "moonraker-activation.json").read_text())["status"], "restarted")
        self.assertFalse(self.updater_plan()["restart_needed"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            updater.print_activation_advice(plan["context"], self.selected, installer.uninstall_api())
        self.assertIn("sudo systemctl restart -- moonraker-test.service", output.getvalue())

    def test_journal_owned_snapshot_links_migrate_without_authorizing_foreign_links(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            arguments = installer.arguments([])
            self.assertFalse(arguments.replace_existing_links)
            migrated = updater.migration_args(self.selected, arguments, installer.uninstall_api())
            self.assertTrue(migrated.replace_existing_links)
            path = self.extras / installer.MODULES[0]
            foreign = self.home / "foreign" / path.name
            links[path] = {"kind": "symlink", "target": str(foreign)}
            with self.assertRaisesRegex(installer.InstallError, "Managed link changed/missing"):
                updater.migration_args(self.selected, arguments, installer.uninstall_api())
            with self.assertRaisesRegex(installer.InstallError, "Review it"):
                installer.make_plan(self.source, self.selected, arguments)

    @contextlib.contextmanager
    def migration_links(self):
        """Keep migration fixtures independent of native symlink privileges."""
        links = {}
        real_snapshot, real_write = installer.snapshot, installer.write_state
        real_exists, real_symlink, real_readlink = Path.exists, Path.is_symlink, os.readlink
        def module(path):
            return Path(path).parent == self.extras and Path(path).name in installer.MODULES
        def snapshot(path):
            return dict(links.get(Path(path), {"kind": "absent"})) if module(path) else real_snapshot(path)
        def write(path, state):
            if module(path):
                links[Path(path)] = dict(state)
            else:
                real_write(path, state)
        def exists(path):
            return links.get(Path(path), {}).get("kind", "absent") != "absent" if module(path) else real_exists(path)
        def is_symlink(path):
            return links.get(Path(path), {}).get("kind") == "symlink" if module(path) else real_symlink(path)
        def readlink(path, *args, **kwargs):
            if module(path) and links.get(Path(path), {}).get("kind") == "symlink":
                return links[Path(path)]["target"]
            return real_readlink(path, *args, **kwargs)
        with mock.patch.object(installer, "snapshot", side_effect=snapshot), \
                mock.patch.object(Path, "exists", new=exists), \
                mock.patch.object(Path, "is_symlink", new=is_symlink), \
                mock.patch.object(os, "readlink", side_effect=readlink), \
                mock.patch.object(installer, "proc_instances", return_value=[]), \
                mock.patch.object(installer, "service_instances", return_value=[]):
            yield links, write

    def migration_snapshot(self, links):
        paths = (self.config, self.axes, self.config.parent / installer.MANAGED,
                 self.moon_config, self.moon_config.parent / updater.INCLUDE,
                 self.config.parent / updater.RECEIPT, self.repo / ".git/info/exclude")
        backups = self.config.parent / ".accel_homing_backups"
        history = {str(path): path.read_bytes() for path in backups.rglob("*") if path.is_file()}
        return ({str(path): installer.snapshot(path) for path in paths}, copy.deepcopy(links), history)

    def assert_migration_refused(self, links, selected=None):
        selected = self.selected if selected is None else selected
        before = self.migration_snapshot(links)
        with self.assertRaises(installer.InstallError):
            arguments = updater.migration_args(selected, installer.arguments([]), installer.uninstall_api())
            installer.make_plan(self.source, selected, arguments)
        self.assertEqual(self.migration_snapshot(links), before)

    def migration_upgrade(self, write):
        previous = self.source
        self.source = self.home / "next-snapshot"
        shutil.copytree(previous, self.source)
        return installer.apply_plan(self.plan("--replace-existing-links")[0], self.config,
                                    writer=write, selected=self.selected)

    def test_recorded_link_migration_preserves_reordered_config_manual_thresholds_and_saved_models(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            text = self.config.read_text()
            macro = "[gcode_macro PRINT_START]\ngcode:\n    M118 Leave this exact macro alone\n"
            self.assertIn(macro, text)
            text = text.replace(macro, "").replace("[printer]", macro + "\n[printer]")
            text = text.replace("#*# x_threshold = 6.5", "#*# x_threshold = 9.2")
            text += "#*# [accel_homing_model later]\n#*# y_threshold = 7.1\n"
            self.config.write_text(text)
            managed = self.config.parent / installer.MANAGED
            managed.write_text(managed.read_text().replace("#x_threshold: 8.0", "x_threshold: 0.5")
                               .replace("#y_threshold: 8.0", "y_threshold: 0.001"))
            before = self.migration_snapshot(links)
            arguments = updater.migration_args(self.selected, installer.arguments([]), installer.uninstall_api())
            self.assertTrue(arguments.replace_existing_links)
            changes = installer.make_plan(self.source, self.selected, arguments)[0]
            self.assertNotIn(self.config, changes)
            self.assertNotIn(self.axes, changes)
            self.assertNotIn(managed, changes)
            # The unchanged include's leading blank now follows [beacon], not
            # the relocated macro. Full inverse restoration stays strict about
            # that section context; code-link migration must not invoke it.
            with self.assertRaisesRegex(installer.InstallError, "Managed section changed"):
                installer.make_uninstall_plan(self.selected)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_missing_original_source_does_not_block_code_migration_or_relax_uninstall(self):
        with self.migration_links() as (links, write):
            retired = self.home / "original-source"
            retired.mkdir()
            for name in installer.MODULES:
                (retired / name).write_text("# original source\n")
                links[self.extras / name] = {"kind": "symlink", "target": str(retired / name)}
            installer.apply_plan(self.plan("--replace-existing-links")[0], self.config,
                                 writer=write, selected=self.selected)
            shutil.rmtree(retired)
            before = self.migration_snapshot(links)
            arguments = updater.migration_args(self.selected, installer.arguments([]), installer.uninstall_api())
            self.assertTrue(arguments.replace_existing_links)
            installer.make_plan(self.source, self.selected, arguments)
            with self.assertRaisesRegex(installer.InstallError, "Original plugin source is missing"):
                installer.make_uninstall_plan(self.selected)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_migration_ownership_does_not_make_modified_managed_config_uninstallable(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            self.axes.write_text(self.axes.read_text().replace("endstop_pin: accel_homing:x", "endstop_pin: !PA4"))
            before = self.migration_snapshot(links)
            arguments = updater.migration_args(self.selected, installer.arguments([]), installer.uninstall_api())
            self.assertTrue(arguments.replace_existing_links)
            with self.assertRaisesRegex(installer.InstallError, "Managed lines|Managed section"):
                installer.make_uninstall_plan(self.selected)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_migration_rejects_changed_missing_or_regular_file_module_states(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            path = self.extras / installer.MODULES[0]
            original = dict(links[path])
            states = ({"kind": "symlink", "target": str(self.home / "foreign" / path.name)},
                      {"kind": "absent"},
                      {"kind": "file", "data": base64.b64encode(b"operator module\n").decode(), "mode": 0o600})
            for state in states:
                with self.subTest(kind=state["kind"]):
                    links[path] = state
                    self.assert_migration_refused(links)
            links[path] = original

    def test_migration_rejects_ambiguous_unowned_and_semantically_tampered_journal_entries(self):
        with self.migration_links() as (links, write):
            backup = installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            path = backup / "manifest.json"
            original = path.read_bytes()
            for damage in ("duplicate", "outside_path", "config_edit"):
                with self.subTest(damage=damage):
                    manifest = json.loads(original)
                    if damage == "duplicate":
                        manifest["changes"].append(copy.deepcopy(manifest["changes"][0]))
                    elif damage == "outside_path":
                        entry = next(item for item in manifest["changes"] if Path(item["path"]).parent == self.extras)
                        entry["path"] = str(self.home / "unowned.py")
                    else:
                        entry = next(item for item in manifest["changes"] if item["path"] == str(self.axes))
                        data = base64.b64decode(entry["after"]["data"])
                        entry["after"]["data"] = base64.b64encode(data.replace(b"run_current: 1.2", b"run_current: 9.9")).decode()
                    path.write_text(json.dumps(manifest))
                    self.assert_migration_refused(links)
            path.write_bytes(original)

    def test_migration_rejects_gapped_or_hash_changed_history(self):
        with self.migration_links() as (links, write):
            first = installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            self.migration_upgrade(write)
            path = first / "manifest.json"
            original = path.read_bytes()
            manifest = json.loads(original)
            manifest["created_utc"] = "changed after the following transaction recorded its parent"
            path.write_text(json.dumps(manifest))
            self.assert_migration_refused(links)
            path.write_bytes(original)
            withheld = self.home / "withheld-history"
            first.rename(withheld)
            try:
                self.assert_migration_refused(links)
            finally:
                withheld.rename(first)

    def test_migration_does_not_borrow_other_checkout_or_instance_ownership(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            self.assert_migration_refused(links, {**self.selected, "repo": self.home / "other-klipper"})
            other = self.config.parent / "other-printer.cfg"
            other.write_bytes(self.config.read_bytes())
            other_before = other.read_bytes()
            before = self.migration_snapshot(links)
            arguments = updater.migration_args({**self.selected, "config": other}, installer.arguments([]), installer.uninstall_api())
            self.assertFalse(arguments.replace_existing_links)
            self.assertEqual(other.read_bytes(), other_before)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_migration_rejects_a_discontinuous_recorded_module_link_chain(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            second = self.migration_upgrade(write)
            path = second / "manifest.json"
            manifest = json.loads(path.read_bytes())
            entry = next(item for item in manifest["changes"] if Path(item["path"]).parent == self.extras)
            entry["before"] = {"kind": "symlink", "target": str(self.home / "never-owned" / Path(entry["path"]).name)}
            path.write_text(json.dumps(manifest))
            self.assert_migration_refused(links)

    def test_migration_rejects_owned_updater_include_or_receipt_drift(self):
        with self.migration_links() as (links, write):
            plan = self.legacy_plan()
            changes = self.plan()[0]
            changes.update(plan["changes"])
            installer.apply_plan(changes, self.config, writer=write, selected=self.selected, updater=plan)
            self.refresh()
            include = self.moon_config.parent / updater.INCLUDE
            receipt = self.config.parent / updater.RECEIPT
            for path in (include, receipt):
                with self.subTest(path=path.name):
                    original = path.read_bytes()
                    if path == include:
                        path.write_bytes(original + b"# later operator edit\n")
                    else:
                        value = json.loads(original)
                        value["commit"] = "b" * 40
                        path.write_text(json.dumps(value))
                    self.refresh()
                    self.assert_migration_refused(links)
                    path.write_bytes(original)
                    self.refresh()

    def test_recorded_link_changed_after_migration_proof_is_not_adopted_during_planning(self):
        with self.migration_links() as (links, write):
            installer.apply_plan(self.plan()[0], self.config, writer=write, selected=self.selected)
            arguments = updater.migration_args(self.selected, installer.arguments([]), installer.uninstall_api())
            self.assertTrue(arguments.replace_existing_links)
            path = self.extras / installer.MODULES[0]
            links[path] = {"kind": "symlink", "target": str(self.home / "appeared-later" / path.name)}
            before = self.migration_snapshot(links)
            with self.assertRaisesRegex(installer.InstallError, "changed after migration ownership validation"):
                installer.make_plan(self.source, self.selected, arguments)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_unrecorded_absent_module_cannot_gain_foreign_link_authority_after_migration_proof(self):
        with self.migration_links() as (links, write):
            path = self.extras / installer.MODULES[-1]
            changes = self.plan()[0]
            changes.pop(path)
            installer.apply_plan(changes, self.config, writer=write, selected=self.selected)
            arguments = updater.migration_args(self.selected, installer.arguments([]), installer.uninstall_api())
            self.assertTrue(arguments.replace_existing_links)
            planned = installer.make_plan(self.source, self.selected, arguments)[0]
            self.assertEqual(planned[path]["expected"], {"kind": "absent"})
            links[path] = {"kind": "symlink", "target": str(self.home / "unowned-later" / path.name)}
            before = self.migration_snapshot(links)
            with self.assertRaisesRegex(installer.InstallError, "changed after migration ownership validation"):
                installer.make_plan(self.source, self.selected, arguments)
            self.assertEqual(self.migration_snapshot(links), before)

    def test_stopped_identity_is_verified_even_with_manual_restart_opt_out(self):
        stopped = {**self.moon, "pid": None, "start_time": None}
        with mock.patch.object(updater, "discover_moonraker", return_value=stopped), \
                mock.patch.object(updater.shutil, "which", return_value="systemctl"), \
                mock.patch.object(updater, "service_identity", side_effect=installer.InstallError("effective loaded mismatch")) as check:
            with self.assertRaisesRegex(installer.InstallError, "effective loaded mismatch"):
                updater.assert_identity(stopped, self.selected, installer.uninstall_api())
            check.assert_called_once()


class IsolatedGitSourceTests(unittest.TestCase):
    """Real git writes only inside disposable synthetic repositories."""
    def run_git(self, path, *args):
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def test_sparse_tracked_source_publication_preserves_old_repo_cache_and_rolls_back_failure(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            remote = home / "remote"
            remote.mkdir()
            self.run_git(remote, "init", "-b", updater.BRANCH)
            self.run_git(remote, "config", "user.name", "Synthetic Test")
            self.run_git(remote, "config", "user.email", "fixture@example.invalid")
            plugin = remote / "Beacon_Accel_Homing"
            plugin.mkdir()
            (plugin / ".gitignore").write_text(".plot-deps/\n")
            for name in installer.MODULES:
                (plugin / name).write_text("# version one\n")
            self.run_git(remote, "add", ".")
            self.run_git(remote, "commit", "-m", "synthetic one")
            first = self.run_git(remote, "rev-parse", "HEAD")
            source = home / "immutable"
            shutil.copytree(plugin, source)
            def marker(commit):
                (source / ".source.json").write_text(json.dumps(dict(repo="FrankRamm/MISC", commit=commit,
                    sha256={name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in installer.MODULES})))
            marker(first)
            selected = dict(repo=home / "klipper", config=home / "printer.cfg")
            api = installer.uninstall_api()
            with mock.patch.object(updater.Path, "home", return_value=home), \
                    mock.patch.object(updater, "ORIGIN", remote.as_uri()):
                repo = updater.managed_path(selected)
                plan = dict(repo=repo, source=repo / "Beacon_Accel_Homing", commit=first,
                            source_input=source, published=False)
                updater.prepare_source(plan, api, lambda: None)
                updater.publish_source(plan, api, lambda: None)
                self.assertEqual(updater.repo_identity(repo, api)["commit"], first)
                (repo / "Beacon_Accel_Homing/.plot-deps").mkdir()
                (repo / "Beacon_Accel_Homing/.plot-deps/cache.txt").write_text("retained private wheels")
                (plugin / installer.MODULES[0]).write_text("# version two\n")
                self.run_git(remote, "add", ".")
                self.run_git(remote, "commit", "-m", "synthetic two")
                second = self.run_git(remote, "rev-parse", "HEAD")
                (source / installer.MODULES[0]).write_text("# version two\n")
                marker(second)
                replacement = dict(repo=repo, source=repo / "Beacon_Accel_Homing", commit=second,
                                   source_input=source, published=False)
                updater.prepare_source(replacement, api, lambda: None)
                self.assertEqual(updater.repo_identity(repo, api)["commit"], first)
                updater.publish_source(replacement, api, lambda: None)
                self.assertEqual(updater.repo_identity(repo, api)["commit"], second)
                self.assertEqual((repo / "Beacon_Accel_Homing/.plot-deps/cache.txt").read_text(), "retained private wheels")
                self.assertTrue(replacement["source_backup"].is_dir())
                updater.restore_publication(replacement, api)
                self.assertEqual(updater.repo_identity(repo, api)["commit"], first)
                self.assertTrue((repo / "Beacon_Accel_Homing/.plot-deps/cache.txt").is_file())
                self.run_git(repo, "config", "remote.origin.url", "https://example.invalid/foreign.git")
                with self.assertRaisesRegex(installer.InstallError, "origin/branch"):
                    updater.repo_identity(repo, api)


if __name__ == "__main__":
    unittest.main()
