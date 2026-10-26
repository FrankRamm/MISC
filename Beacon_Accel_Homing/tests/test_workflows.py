"""Offline state-machine tests: simulated G28 results never touch a printer."""
import collections
import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from accel_homing_charts import write_capture
from accel_homing_model import AccelHomingModel
from accel_homing_workflows import AccelHomingWorkflows
from harness import CommandError, ConfigError, FakeReactor


class Config:
    error = ConfigError

    def __init__(self, printer, options=None, name="accel_homing", sections=None):
        self.printer, self.options, self.name = printer, options or {}, name
        self.sections = sections or {"printer": {"kinematics": "corexy"}}

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getfloat(self, name, default=None, minval=None, maxval=None):
        return self._number(float, name, default, minval, maxval)

    def getint(self, name, default=None, minval=None, maxval=None):
        return self._number(int, name, default, minval, maxval)

    def _number(self, kind, name, default, low, high):
        value = self.options.get(name, default)
        if value is None:
            return value
        value = kind(value)
        if (low is not None and value < low) or (high is not None and value > high):
            raise ConfigError(name + " out of range")
        return value

    def has_section(self, name):
        return name in self.sections

    def getsection(self, name):
        return Config(self.printer, self.sections[name], name, self.sections)


class GCmd:
    error = CommandError

    def __init__(self, **params):
        self.params, self.responses = params, []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = int(self.get(name, default))
        if (minval is not None and value < minval) or (maxval is not None and value > maxval):
            raise CommandError("invalid " + name)
        return value

    def respond_info(self, message):
        self.responses.append(message)


class Line:
    def __init__(self, name, disabled):
        self.name, self.disabled = name, disabled
        self.enable = object()
        self.enabled = True

    def motor_disable(self, stamp):
        self.enabled = False
        self.disabled.append(self.name)

    def is_motor_enabled(self):
        return self.enabled


class Stepper:
    def __init__(self, name, axes):
        self.name, self.axes = name, axes

    def get_name(self):
        return self.name

    def is_active_axis(self, axis):
        return axis in self.axes


class Rail:
    def __init__(self, positive=False, bounds=(0.0, 300.0), endstop=None):
        self.bounds = bounds
        self.info = types.SimpleNamespace(
            retract_dist=0.0, min_home_dist=0.0, speed=100.0, positive_dir=positive,
            position_endstop=(bounds[1] if positive else bounds[0]) if endstop is None else endstop)

    def get_homing_info(self):
        return self.info

    def get_range(self):
        return self.bounds


class Kin:
    def __init__(self):
        self.limits = [(1.0, -1.0), (1.0, -1.0), (0.0, 500.0)]
        self.steppers = [Stepper("stepper_x", "xy"), Stepper("stepper_y", "xy"),
                         Stepper("stepper_z", "z"), Stepper("stepper_z1", "z")]
        self.dual_carriage_axis = None

    def get_steppers(self):
        return self.steppers

    def clear_homing_state(self, axes):
        for i, axis in enumerate("xyz"):
            if axis in axes:
                self.limits[i] = (1.0, -1.0)

    def get_status(self, stamp):
        return {"homed_axes": "".join(axis for axis, limits in zip("xyz", self.limits)
                                      if limits[0] <= limits[1])}


class Toolhead:
    def __init__(self, rig):
        self.rig, self.kin = rig, Kin()
        self.position = [150.0, 100.0, 40.0, 6.0]
        self.moves, self.dwells = [], []
        self.busy = (0.0, 10.0, True)

    def get_position(self):
        return list(self.position)

    def get_kinematics(self):
        return self.kin

    def get_status(self, stamp):
        return self.kin.get_status(stamp)

    def check_busy(self, stamp):
        return self.busy

    def wait_moves(self):
        self.busy = (0.0, 10.0, True)

    def flush_step_generation(self):
        pass

    def dwell(self, seconds):
        self.dwells.append(seconds)
        self.rig.idle_state = "Printing"  # stock toolhead:sync_print_time lifecycle

    def get_last_move_time(self):
        return 0.0

    def manual_move(self, coords, speed):
        self.moves.append((list(coords), speed))
        for index, value in enumerate(coords):
            if value is not None:
                self.position[index] = value
        self.rig.idle_state = "Printing"


class Configfile:
    def __init__(self):
        self.saved = {}
        self.fail = False

    def set(self, section, option, value):
        if self.fail:
            raise CommandError("simulated config staging error")
        self.saved[(section, option)] = value


class Rig:
    config_error = ConfigError
    command_error = CommandError

    def __init__(self, options=None, sections=None):
        self.reactor = FakeReactor()
        self.handlers = collections.defaultdict(list)
        self.output, self.scripts, self.disabled, self.home_calls = [], [], [], []
        self.end_sessions = 0
        self.idle_state, self.print_state = "Ready", "standby"
        self.paused, self.sd_active, self.shutdown = False, False, False
        self.save_error = False
        self.home_results = collections.deque()
        self.on_home = None
        self.capture_active, self.capture_stops = False, 0
        self.bad_capture = None
        self.toolhead = Toolhead(self)
        self.lines = {s.name: Line(s.name, self.disabled) for s in self.toolhead.kin.steppers}
        self.configfile = Configfile()
        self.gcode = types.SimpleNamespace(
            ready_gcode_handlers={}, register_command=self.register_command,
            respond_info=self.output.append, run_script_from_command=self.run_script)
        homing = types.SimpleNamespace(cmd_G28=self.stock_home)
        self.gcode.ready_gcode_handlers["G28"] = homing.cmd_G28
        self.objects = {
            "gcode": self.gcode, "toolhead": self.toolhead,
            "homing": homing, "configfile": self.configfile,
            "stepper_enable": types.SimpleNamespace(lookup_enable=self.lines.__getitem__),
            "idle_timeout": types.SimpleNamespace(get_status=lambda t: {"state": self.idle_state}),
            "print_stats": types.SimpleNamespace(get_status=lambda t: {"state": self.print_state}),
            "pause_resume": types.SimpleNamespace(get_status=lambda t: {"is_paused": self.paused}),
            "virtual_sdcard": types.SimpleNamespace(is_active=lambda: self.sd_active)}
        self.config = Config(self, options, sections=sections)
        self.owner = types.SimpleNamespace(
            printer=self, reactor=self.reactor, toolhead=self.toolhead, name="accel_homing",
            threshold_g={"x": 8.0, "y": 9.0}, impact_sign={"x": 0, "y": 0},
            homing_accel=5000.0, back_off_dist=5.0, full_scale_g=16.0,
            homing_active=False, attempt=None, last={}, endstops={"x": object(), "y": object()},
            rails_by_axis={"x": Rail(), "y": Rail(bounds=(0.0, 200.0))},
            sensor_name="simulated", start_capture=self.start_capture, stop_capture=self.stop_capture,
            _end_session=self.end_session, cmd_STATUS=lambda g: g.respond_info("core status"))
        self.workflow = AccelHomingWorkflows(self.owner, self.config)
        self.owner.workflows = self.workflow

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def load_object(self, config, name):
        if name not in self.objects and name.startswith("accel_homing_model "):
            self.objects[name] = AccelHomingModel(config.getsection(name))
        return self.objects[name]

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, event, callback):
        self.handlers[event].append(callback)

    def is_shutdown(self):
        return self.shutdown

    def register_command(self, name, callback, desc=None):
        if name in self.gcode.ready_gcode_handlers:
            raise ConfigError("duplicate command: " + name)
        self.gcode.ready_gcode_handlers[name] = callback

    def end_session(self):
        self.end_sessions += 1
        self.owner.homing_active = False
        self.owner.attempt = None

    def start_capture(self):
        if self.capture_active:
            raise CommandError("capture already active")
        self.capture_active = True

    def stop_capture(self):
        self.capture_active = False
        self.capture_stops += 1
        if self.bad_capture is not None:
            return self.bad_capture
        return {"samples": [(0.0, 0.0, 0.0, 1.0), (0.001, 0.1, 0.0, 1.0),
                            (0.002, -4.0, -5.0, 1.0), (0.003, 0.0, 0.0, 1.0)],
                "dropped": 0, "baseline_g": (0.0, 0.0, 1.0)}

    def stock_home(self, gcmd):
        raise AssertionError("the simulated run_script handles G28 explicitly")

    def run_script(self, script):
        self.scripts.append(script)
        if script == "SAVE_CONFIG":
            if self.save_error:
                raise CommandError("simulated SAVE_CONFIG include conflict")
            return
        if not script.startswith("G28 ") or script not in ("G28 X", "G28 Y"):
            raise AssertionError("unexpected movement command " + script)
        axis = script[-1].lower()
        self.workflow.check_home_axes({axis: self.owner.rails_by_axis[axis]})
        self.home_calls.append({"axis": axis, "threshold": self.owner.threshold_g[axis],
                                "homed_axes": self.toolhead.kin.get_status(0)["homed_axes"],
                                "position": list(self.toolhead.position),
                                "attempt": self.workflow.session["attempts"],
                                "candidate_qualified": self.workflow.session["candidate_qualified"],
                                "successes": self.workflow.session["successes"]})
        if self.on_home:
            self.on_home(axis)
        result = self.home_results.popleft() if self.home_results else {}
        if isinstance(result, Exception):
            raise result
        _center, expected = self.workflow._geometry(axis)
        record = {"axis": axis, "result": "impact", "travel_mm": expected,
                  "onset_g": 4.0, "impact_peak_g": 7.0, "moving": True,
                  "threshold_g": self.owner.threshold_g[axis], "arm_time_s": 0.001,
                  "onset_time_s": 0.002, "direction": -1}
        record.update(result)
        self.owner.last[axis] = record
        index = "xy".index(axis)
        self.toolhead.kin.limits[index] = self.owner.rails_by_axis[axis].get_range()
        self.toolhead.position[index] = self.owner.rails_by_axis[axis].info.position_endstop
        self.workflow.check_home_result(record)
        self.idle_state = "Printing"

    def calibrate(self, axis="X", save=None, **params):
        arguments = {"AXIS": axis, "CENTERED": 1}
        if save is not None:
            arguments["SAVE_CONFIG"] = save
        arguments.update(params)
        self.workflow.cmd_CALIBRATE(GCmd(**arguments))

    def confirm(self, **params):
        arguments = {"CENTERED": 1}
        arguments.update(params)
        self.workflow.cmd_CONFIRM(GCmd(**arguments))

    def effects(self):
        return copy.deepcopy({
            "scripts": self.scripts, "moves": self.toolhead.moves,
            "dwells": self.toolhead.dwells, "disabled": self.disabled,
            "enabled": {name: line.enabled for name, line in self.lines.items()},
            "position": self.toolhead.position, "limits": self.toolhead.kin.limits,
            "thresholds": self.owner.threshold_g, "back_off": self.owner.back_off_dist,
            "session": self.workflow.session, "result": self.workflow.last_result,
            "saved": self.configfile.saved, "output": self.output,
            "end_sessions": self.end_sessions, "capture_stops": self.capture_stops,
            "capture_active": self.capture_active, "timers": list(self.reactor.timers.values())})


class CalibrationTests(unittest.TestCase):
    def test_first_home_is_the_real_candidate_trial_from_unhomed_xy_without_seed(self):
        rig = Rig()
        # Unhomed reported coordinates do not establish the physical center.
        rig.toolhead.position[:2] = [12.0, 19.0]
        rig.calibrate(save=0)
        first = rig.home_calls[0]
        self.assertEqual(first["threshold"], 3.0)
        self.assertEqual(first["homed_axes"], "z")
        self.assertEqual(first["position"], [12.0, 19.0, 40.0, 6.0])
        self.assertEqual(first["attempt"], 1)
        self.assertFalse(first["candidate_qualified"])
        self.assertTrue(rig.home_calls[1]["candidate_qualified"])
        self.assertEqual(rig.home_calls[1]["attempt"], 2)
        self.assertEqual(rig.home_calls[1]["successes"], 0)
        self.assertEqual(rig.scripts, ["G28 X"] * 11)
        self.assertEqual(rig.disabled, [])
        self.assertEqual(rig.toolhead.dwells, [])
        self.assertEqual(rig.owner.threshold_g, {"x": 8.0, "y": 9.0})
        self.assertFalse(any("action:prompt_begin" in line for line in rig.output))
        self.assertFalse(any("TOKEN" in line for line in rig.output))

    def test_ten_independent_axis_homes_are_saved_and_original_settings_restored(self):
        for axis in "XY":
            rig = Rig()
            rig.calibrate(axis)
            self.assertEqual(rig.scripts, ["G28 " + axis] * 11 + ["SAVE_CONFIG"])
            self.assertEqual(len(rig.toolhead.moves), 10)
            index = "XY".index(axis)
            for coords, speed in rig.toolhead.moves:
                self.assertEqual(coords[2], None)
                self.assertEqual(coords[1 - index], None)
                self.assertEqual(coords[index], (150.0, 100.0)[index])
                self.assertEqual(speed, 50.0)
            values = rig.configfile.saved
            self.assertEqual(values[("accel_homing_model default", axis.lower() + "_successes")], "10")
            self.assertNotIn(("accel_homing_model default", "XY"[1 - index].lower() + "_threshold"), values)
            self.assertNotIn(("accel_homing", "x_threshold"), values)
            self.assertEqual(rig.owner.threshold_g, {"x": 8.0, "y": 9.0})
            self.assertEqual(rig.owner.back_off_dist, 5.0)
            self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])
            self.assertFalse(rig.capture_active)
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.workflow.last_result["successes"], 10)
            metadata = json.loads(values[("accel_homing_model default", axis.lower() + "_metadata")])
            self.assertEqual(len(metadata["onset_g"]), 10)
            self.assertFalse(metadata["physical_position_verified"])

    def test_short_false_home_waits_without_reposition_then_same_command_preserves_candidate(self):
        for short in (0.0, 10.0, 40.0, 110.0):
            rig = Rig()
            rig.home_results.append({"travel_mm": short})
            rig.calibrate()
            session = rig.workflow.session
            run = session["run_id"]
            self.assertEqual(rig.toolhead.moves, [])
            self.assertEqual(rig.scripts, ["G28 X"])
            self.assertEqual(session["candidate"], 3.5)
            self.assertEqual(session["successes"], 0)
            self.assertEqual(session["attempts"], 1)
            self.assertEqual(session["state"], "waiting")
            self.assertNotIn("token", session)
            self.assertEqual(set(rig.disabled), {"stepper_x", "stepper_y"})
            self.assertEqual(rig.toolhead.kin.get_status(0)["homed_axes"], "z")
            self.assertTrue(rig.lines["stepper_z"].enabled)
            self.assertTrue(rig.lines["stepper_z1"].enabled)
            self.assertEqual(rig.owner.threshold_g, {"x": 8.0, "y": 9.0})
            self.assertTrue(any("ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1" in text
                                for text in rig.output))
            self.assertFalse(any("ACCEL_HOMING_CONFIRM" in text or "TOKEN" in text
                                 for text in rig.output))
            rig.calibrate()
            self.assertEqual(rig.home_calls[1]["threshold"], 3.5)
            self.assertEqual(rig.home_calls[1]["attempt"], 2)
            self.assertEqual(rig.home_calls[1]["homed_axes"], "z")
            self.assertEqual(rig.scripts, ["G28 X"] * 12 + ["SAVE_CONFIG"])
            self.assertEqual(rig.workflow.last_result["run_id"], run)
            self.assertEqual(rig.workflow.last_result["threshold_g"], 3.5)

    def test_pressed_result_is_never_a_success_even_with_apparent_step_travel(self):
        rig = Rig()
        rig.home_results.append({"result": "pressed", "travel_mm": 150.0, "moving": False})
        rig.calibrate()
        self.assertEqual(rig.workflow.session["successes"], 0)
        self.assertEqual(rig.toolhead.moves, [])
        self.assertEqual(rig.workflow.session["candidate"], 3.5)

    def test_success_count_resets_after_threshold_changes(self):
        rig = Rig()
        rig.home_results.extend([{}, {}, {"travel_mm": 8.0}])
        rig.calibrate()
        self.assertEqual(len(rig.toolhead.moves), 2)
        self.assertEqual(rig.workflow.session["successes"], 0)
        self.assertFalse(rig.workflow.session["candidate_qualified"])
        self.assertEqual(rig.workflow.session["attempts"], 3)
        rig.calibrate()
        self.assertEqual(rig.workflow.last_result["successes"], 10)
        self.assertEqual(rig.workflow.last_result["threshold_g"], 3.5)
        self.assertEqual(len(rig.scripts), 15)

    def test_suspicious_long_travel_or_no_motion_abort_without_threshold_escalation(self):
        for bad in ({"travel_mm": 200.0}, {"moving": False}, {"travel_mm": None},
                    {"result": "no_data"}):
            rig = Rig()
            rig.home_results.append(bad)
            with self.assertRaises(CommandError):
                rig.calibrate()
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.toolhead.moves, [])
            self.assertEqual(rig.scripts, ["G28 X"])
            self.assertEqual(rig.owner.threshold_g["x"], 8.0)
            self.assertEqual(rig.toolhead.kin.get_status(0)["homed_axes"], "z")

    def test_threshold_ceiling_and_attempt_ceiling_abort(self):
        for ceiling in ("threshold", "sensor_scale", "attempts"):
            rig = Rig({"calibration_max_threshold": 3.0} if ceiling == "threshold" else {})
            if ceiling == "sensor_scale":
                rig.owner.full_scale_g = 4.0
            if ceiling == "attempts":
                rig.home_results.append({"travel_mm": 2.0})
                rig.calibrate()
                rig.workflow.session["attempts"] = rig.workflow.max_attempts
            else:
                rig.home_results.append({"travel_mm": 2.0})
            with self.assertRaisesRegex(CommandError, "ceiling|maximum"):
                rig.calibrate()
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.toolhead.moves, [])

    def test_no_data_and_incomplete_capture_abort_and_close_capture(self):
        for result in (CommandError("no accelerometer data"), None):
            rig = Rig()
            if result:
                rig.home_results.append(result)
            else:
                rig.bad_capture = {"samples": [], "dropped": 0}
            with self.assertRaises(CommandError):
                rig.calibrate()
            self.assertFalse(rig.capture_active)
            self.assertEqual(rig.owner.back_off_dist, 5.0)
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.toolhead.moves, [])

    def test_unhomed_positive_and_negative_axes_use_actual_geometry_without_touching_z(self):
        for axis in "xy":
            for positive in (False, True):
                rig = Rig()
                rig.owner.rails_by_axis[axis] = Rail(positive=positive, bounds=(-40.0, 220.0))
                rig.calibrate(axis.upper())
                coords = [None, None, None]
                coords["xy".index(axis)] = 90.0
                self.assertEqual(rig.toolhead.moves[0][0], coords)
                self.assertEqual(rig.home_calls[0]["homed_axes"], "z")
                self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])
                self.assertEqual(rig.workflow.last_result["result"], "calibrated")

    def test_configured_start_remains_editable_above_minimum_and_is_used_first(self):
        rig = Rig({"calibration_start_threshold": 4.0})
        rig.calibrate(save=0)
        self.assertEqual([call["threshold"] for call in rig.home_calls], [4.0] * 11)
        with self.assertRaises(ConfigError):
            Rig({"calibration_start_threshold": 2.5})

    def test_successful_calibration_keeps_unhomed_z_unhomed(self):
        rig = Rig()
        rig.toolhead.kin.limits[2] = (1.0, -1.0)
        rig.calibrate(save=0)
        self.assertEqual(rig.toolhead.kin.get_status(0)["homed_axes"], "x")
        self.assertEqual(rig.toolhead.kin.limits[2], (1.0, -1.0))
        self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])
        self.assertTrue(rig.lines["stepper_z"].enabled)
        self.assertTrue(rig.lines["stepper_z1"].enabled)

    def test_save_config_opt_out_stages_but_never_restarts(self):
        rig = Rig()
        rig.calibrate(save=0)
        self.assertNotIn("SAVE_CONFIG", rig.scripts)
        self.assertEqual(rig.workflow.last_result["result"], "calibrated")
        self.assertTrue(any("staged only" in text for text in rig.output))

    def test_save_config_error_restores_settings_and_clears_active_state(self):
        rig = Rig()
        rig.save_error = True
        with self.assertRaisesRegex(CommandError, "SAVE_CONFIG"):
            rig.calibrate()
        self.assertIsNone(rig.workflow.session)
        self.assertEqual(rig.owner.threshold_g["x"], 8.0)
        self.assertEqual(rig.owner.back_off_dist, 5.0)
        self.assertFalse(rig.capture_active)
        self.assertEqual(rig.workflow.last_result["result"], "save_failed")

    def test_config_staging_error_restores_everything(self):
        rig = Rig()
        rig.configfile.fail = True
        with self.assertRaisesRegex(CommandError, "staging"):
            rig.calibrate()
        self.assertIsNone(rig.workflow.session)
        self.assertEqual(rig.owner.threshold_g["x"], 8.0)
        self.assertFalse(rig.capture_active)


class GuardTests(unittest.TestCase):
    def test_missing_zero_or_malformed_centered_has_no_start_or_resume_side_effects(self):
        for waiting in (False, True):
            for params in ({}, {"CENTERED": 0}, {"CENTERED": "invalid"},
                           {"CENTERED": "1.0"}, {"CENTERED": 2}, {"CENTERED": -1}):
                with self.subTest(waiting=waiting, params=params):
                    rig = Rig()
                    if waiting:
                        rig.home_results.append({"travel_mm": 2.0})
                        rig.calibrate()
                    before = rig.effects()
                    with self.assertRaisesRegex(CommandError, "CENTERED=1"):
                        rig.workflow.cmd_CALIBRATE(GCmd(AXIS="X", **params))
                    self.assertEqual(rig.effects(), before)

    def test_homed_x_or_y_is_rejected_without_motion_release_or_setting_changes(self):
        for homed in ("x", "y", "xy", "xz", "yz", "xyz"):
            for axis in "XY":
                rig = Rig()
                rig.toolhead.kin.limits = [(0.0, 300.0) if a in homed else (1.0, -1.0)
                                           for a in "xyz"]
                before = rig.effects()
                with self.assertRaisesRegex(CommandError, "both X and Y unhomed"):
                    rig.calibrate(axis)
                self.assertEqual(rig.effects(), before)

    def test_release_clears_only_xy_across_integer_and_letter_clear_state_apis(self):
        class IntegerKin(Kin):
            def clear_homing_state(self, axes):
                for index in range(3):
                    if index in axes:
                        self.limits[index] = (1.0, -1.0)

        for kind in (Kin, IntegerKin):
            for z_homed in (False, True):
                rig = Rig()
                kin = kind()
                kin.limits[2] = (20.0, 480.0) if z_homed else (1.0, -1.0)
                kin.clear_homing_state = mock.Mock(wraps=kin.clear_homing_state)
                rig.toolhead.kin = kin
                z_limit = kin.limits[2]
                rig.home_results.append({"travel_mm": 2.0})
                rig.calibrate()
                self.assertEqual(kin.get_status(0)["homed_axes"], "z" if z_homed else "")
                self.assertEqual(kin.limits[2], z_limit)
                self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])
                self.assertTrue(rig.lines["stepper_z"].enabled)
                self.assertTrue(rig.lines["stepper_z1"].enabled)
                kin.clear_homing_state.assert_not_called()

    def test_unknown_limits_layout_is_rejected_before_side_effects(self):
        for limits in (None, ((1.0, -1.0),) * 3, [(1.0, -1.0)] * 2):
            rig = Rig()
            rig.toolhead.kin.limits = limits
            before = rig.effects()
            with self.assertRaisesRegex(CommandError, "invalidate XY"):
                rig.calibrate()
            self.assertEqual(rig.effects(), before)

    def test_nonfinite_workflow_configuration_is_rejected(self):
        for name in ("calibration_start_threshold", "calibration_step", "calibration_max_threshold",
                     "calibration_confirm_timeout", "calibration_travel_tolerance",
                     "workflow_reposition_speed"):
            with self.assertRaises(ConfigError):
                Rig({name: float("nan")})

    def test_charts_default_tracks_custom_main_configuration_directory(self):
        with mock.patch.object(Rig, "get_start_args", create=True,
                               return_value={"config_file": "C:/custom_instance/config/printer-main.cfg"}):
            rig = Rig()
            expected = Path("C:/custom_instance/config/accel_homing_charts").absolute()
            self.assertEqual(Path(rig.workflow.charts_directory), expected)

    def test_busy_print_pause_and_pending_queue_reject_before_motion(self):
        for setup in (lambda r: setattr(r, "print_state", "printing"),
                      lambda r: setattr(r, "print_state", "paused"),
                      lambda r: setattr(r, "paused", True),
                      lambda r: setattr(r, "sd_active", True),
                      lambda r: setattr(r, "idle_state", "Printing"),
                      lambda r: setattr(r.toolhead, "busy", (11.0, 10.0, True)),
                      lambda r: setattr(r.toolhead, "busy", (0.0, 10.0, False))):
            rig = Rig()
            setup(rig)
            with self.assertRaises(CommandError):
                rig.calibrate()
            self.assertEqual(rig.scripts, [])
            self.assertEqual(rig.disabled, [])

    def test_current_command_does_not_mark_idle_printing_and_own_prompt_motion_can_continue(self):
        rig = Rig()
        rig.home_results.append({"travel_mm": 2.0})
        rig.calibrate()
        self.assertEqual(rig.idle_state, "Printing")
        rig.calibrate()
        self.assertEqual(rig.workflow.last_result["result"], "calibrated")

    def test_overrides_macros_and_unrelated_g28_wrappers_rejected(self):
        for key in ("homing_override", "gcode_macro G28", "wrapped_handler"):
            rig = Rig()
            if key == "wrapped_handler":
                rig.gcode.ready_gcode_handlers["G28"] = lambda g: None
            else:
                rig.objects[key] = object()
            with self.assertRaisesRegex(CommandError, "G28"):
                rig.calibrate()
            self.assertEqual(rig.scripts, [])

    def test_retract_or_minimum_second_home_are_rejected_before_initial_g28(self):
        for field in ("retract_dist", "min_home_dist"):
            rig = Rig()
            setattr(rig.owner.rails_by_axis["x"].info, field, 5.0)
            with self.assertRaisesRegex(CommandError, "homing_retract_dist"):
                rig.calibrate()
            self.assertEqual(rig.scripts, [])

    def test_unsupported_z_coupled_kinematics_and_shared_enable_lines_rejected(self):
        for kind in ("corexz", "shared_line", "coupled_stepper", "dual_carriage"):
            rig = Rig()
            if kind == "corexz":
                rig.workflow.kinematics_name = "corexz"
            elif kind == "shared_line":
                rig.lines["stepper_x"].enable = rig.lines["stepper_z"].enable
            elif kind == "coupled_stepper":
                rig.toolhead.kin.steppers[0].axes = "xz"
            else:
                rig.toolhead.kin.dual_carriage_axis = 0
            with self.assertRaises(CommandError):
                rig.calibrate()
            self.assertEqual(rig.scripts, [])
            self.assertEqual(rig.disabled, [])

    def test_new_workflow_external_home_and_setting_change_are_blocked_while_waiting(self):
        rig = Rig()
        rig.home_results.append({"travel_mm": 2.0})
        rig.calibrate()
        before = rig.effects()
        with self.assertRaisesRegex(CommandError, "already active"):
            rig.calibrate("Y")
        with self.assertRaisesRegex(CommandError, "already active"):
            rig.workflow.cmd_TEST(GCmd())
        with self.assertRaisesRegex(CommandError, "already active"):
            rig.workflow.cmd_CHARTS(GCmd())
        with self.assertRaisesRegex(CommandError, "workflow"):
            rig.workflow.check_home_axes({"x": rig.owner.rails_by_axis["x"]})
        with self.assertRaisesRegex(CommandError, "workflow"):
            rig.workflow.check_settings_change()
        with self.assertRaisesRegex(CommandError, "ACCEL_HOMING_CALIBRATE AXIS=X CENTERED=1"):
            rig.confirm()
        self.assertEqual(rig.effects(), before)

    def test_calibration_cannot_replace_another_workflow_or_reenter_a_running_one(self):
        for mode in ("test", "charts", "calibration_running"):
            rig = Rig()
            if mode == "test":
                rig.workflow.cmd_TEST(GCmd())
            elif mode == "charts":
                rig.workflow.cmd_CHARTS(GCmd())
            else:
                rig.home_results.append({"travel_mm": 2.0})
                rig.calibrate()
                rig.workflow.session["state"] = "running"
            before = rig.effects()
            with self.assertRaisesRegex(CommandError, "already active"):
                rig.calibrate()
            self.assertEqual(rig.effects(), before)

    def test_calibration_resume_keeps_save_config_choice_and_rejects_conflicting_override(self):
        rig = Rig()
        rig.home_results.append({"travel_mm": 2.0})
        rig.calibrate(save=0)
        before = rig.effects()
        with self.assertRaisesRegex(CommandError, "SAVE_CONFIG cannot change"):
            rig.calibrate(save=1)
        self.assertEqual(rig.effects(), before)
        rig.calibrate()
        self.assertNotIn("SAVE_CONFIG", rig.scripts)
        self.assertFalse(rig.workflow.last_result["save_config"])

    def test_stale_position_settings_homed_state_or_print_state_abort_calibration_resume(self):
        for setup in (lambda r: r.toolhead.position.__setitem__(0, 10.0),
                      lambda r: r.owner.impact_sign.__setitem__("x", -1),
                      lambda r: r.owner.threshold_g.__setitem__("x", 7.0),
                      lambda r: setattr(r.owner, "homing_accel", 4000.0),
                      lambda r: setattr(r.owner, "back_off_dist", 3.0),
                      lambda r: r.toolhead.kin.limits.__setitem__(0, (0.0, 300.0)),
                      lambda r: setattr(r, "print_state", "printing"),
                      lambda r: setattr(r, "paused", True),
                      lambda r: setattr(r.toolhead, "busy", (20.0, 10.0, True))):
            rig = Rig()
            rig.home_results.append({"travel_mm": 2.0})
            rig.calibrate()
            setup(rig)
            with self.assertRaises(CommandError):
                rig.calibrate()
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.scripts, ["G28 X"])
            self.assertEqual(rig.toolhead.moves, [])

    def test_late_resume_is_rejected_even_if_timeout_callback_has_not_run(self):
        rig = Rig()
        rig.home_results.append({"travel_mm": 2.0})
        rig.calibrate()
        rig.reactor.now = rig.workflow.session["expires"]
        with self.assertRaisesRegex(CommandError, "expired"):
            rig.calibrate()
        self.assertIsNone(rig.workflow.session)
        self.assertEqual(rig.scripts, ["G28 X"])
        self.assertEqual(rig.toolhead.moves, [])

    def test_cancel_and_timeout_restore_without_more_motion(self):
        for method in ("cancel", "timeout"):
            rig = Rig()
            rig.home_results.append({"travel_mm": 2.0})
            rig.calibrate()
            if method == "cancel":
                rig.workflow.cmd_CANCEL(GCmd())
            else:
                when = rig.workflow.session["expires"]
                self.assertEqual(rig.workflow._timeout(when), rig.reactor.NEVER)
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.owner.threshold_g["x"], 8.0)
            self.assertEqual(rig.scripts, ["G28 X"])
            self.assertEqual(rig.toolhead.moves, [])


class ModelTests(unittest.TestCase):
    def test_selected_model_rejects_changed_sensor_geometry_speed_or_mapping(self):
        calibrated = Rig()
        calibrated.calibrate(save=0)
        options = {key: value for (section, key), value in calibrated.configfile.saved.items()}
        sections = {"printer": {"kinematics": "corexy"},
                    "accel_homing_model default": options}
        rig = Rig(sections=sections)
        rig.workflow._validate_model_connection()
        for mutate in (lambda r: setattr(r.owner, "sensor_name", "other_sensor"),
                       lambda r: setattr(r.owner.rails_by_axis["x"].info, "position_endstop", 2.0),
                       lambda r: setattr(r.owner.rails_by_axis["x"], "bounds", (-10.0, 300.0)),
                       lambda r: setattr(r.owner.rails_by_axis["x"].info, "speed", 80.0),
                       lambda r: setattr(r.owner, "helper", types.SimpleNamespace(
                           config=types.SimpleNamespace(axes_map=[(1, 1), (0, 1), (2, 1)])))):
            rig = Rig(sections=sections)
            mutate(rig)
            with self.assertRaisesRegex(ConfigError, "does not match"):
                rig.workflow._validate_model_connection()
            override = Rig({"x_threshold": 8.0}, sections=sections)
            mutate(override)
            override.workflow._validate_model_connection()

    def test_saved_models_supply_defaults_but_manual_controls_win(self):
        sections = {"printer": {"kinematics": "corexy"},
                    "accel_homing_model default": {"x_threshold": 4.0, "x_successes": 10,
                                                   "y_threshold": 5.0, "y_successes": 10}}
        rig = Rig(sections=sections)
        self.assertEqual(rig.owner.threshold_g, {"x": 4.0, "y": 5.0})
        rig = Rig({"x_threshold": 8.0}, sections=sections)
        self.assertEqual(rig.owner.threshold_g, {"x": 8.0, "y": 5.0})
        rig = Rig({"threshold": 8.0}, sections=sections)
        self.assertEqual(rig.owner.threshold_g, {"x": 8.0, "y": 9.0})

    def test_model_rejects_unqualified_threshold_or_bad_metadata(self):
        for options in ({"x_threshold": 4.0, "x_successes": 9},
                        {"x_metadata": "[]"}, {"x_metadata": "invalid"},
                        {"x_threshold": float("nan"), "x_successes": 10},
                        {"x_metadata": '{"invalid": NaN}'},
                        {"x_metadata": '{"nested": [1e999]}'}):
            with self.assertRaises(ConfigError):
                AccelHomingModel(Config(None, options, "accel_homing_model test"))

    def test_saving_one_axis_keeps_other_axis_model(self):
        rig = Rig()
        rig.configfile.saved[("accel_homing_model default", "y_threshold")] = "4.5"
        rig.calibrate()
        self.assertEqual(rig.configfile.saved[("accel_homing_model default", "y_threshold")], "4.5")


class TestAndChartWorkflowTests(unittest.TestCase):
    def test_no_argument_start_prompts_for_token_free_explicit_confirmation(self):
        for callback in ("cmd_TEST", "cmd_CHARTS"):
            rig = Rig()
            getattr(rig.workflow, callback)(GCmd())
            self.assertEqual(rig.scripts, [])
            self.assertEqual(rig.workflow.session["state"], "waiting")
            self.assertNotIn("token", rig.workflow.session)
            self.assertTrue(any("ACCEL_HOMING_CONFIRM CENTERED=1" in text for text in rig.output))
            self.assertFalse(any("TOKEN" in text for text in rig.output))
            for params in ({}, {"CENTERED": 0}, {"CENTERED": "invalid"}, {"CENTERED": 2}):
                before = rig.effects()
                with self.assertRaisesRegex(CommandError, "CENTERED=1"):
                    rig.workflow.cmd_CONFIRM(GCmd(**params))
                self.assertEqual(rig.effects(), before)

    def test_old_extra_token_is_ignored_for_compatibility(self):
        rig = Rig()
        rig.workflow.cmd_TEST(GCmd())
        rig.confirm(TOKEN="obsolete-token")
        self.assertEqual(rig.scripts, ["G28 X", "G28 Y"])
        self.assertEqual(rig.workflow.last_result["result"], "complete")

    def test_shared_confirmation_rejects_changed_state_and_expiry_before_any_home(self):
        for change in ("position", "threshold", "homed_state", "printing", "expired"):
            rig = Rig()
            rig.workflow.cmd_TEST(GCmd())
            if change == "position":
                rig.toolhead.position[0] += 5.0
            elif change == "threshold":
                rig.owner.threshold_g["y"] = 4.0
            elif change == "homed_state":
                rig.toolhead.kin.limits[1] = (0.0, 200.0)
            elif change == "printing":
                rig.print_state = "printing"
            else:
                rig.reactor.now = rig.workflow.session["expires"]
            with self.assertRaises(CommandError):
                rig.confirm()
            self.assertIsNone(rig.workflow.session)
            self.assertEqual(rig.scripts, [])
            self.assertEqual(rig.toolhead.moves, [])

    def test_test_homes_x_then_y_only_after_confirmation(self):
        rig = Rig()
        rig.workflow.cmd_TEST(GCmd())
        self.assertEqual(rig.scripts, [])
        rig.confirm()
        self.assertEqual(rig.scripts, ["G28 X", "G28 Y"])
        self.assertEqual(rig.toolhead.moves, [([150.0, None, None], 50.0)])
        self.assertEqual(rig.workflow.last_result["result"], "complete")
        self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])

    def test_chart_exports_current_run_samples_and_explicit_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = Rig({"charts_directory": temp})
            rig.owner.last["x"] = {"axis": "x", "result": "impact", "travel_mm": 1234}
            rig.workflow.cmd_CHARTS(GCmd())
            run = rig.workflow.session["run_id"]
            rig.confirm()
            self.assertEqual(rig.scripts, ["G28 X", "G28 Y"])
            result = rig.workflow.last_result
            self.assertEqual(len(result["paths"]), 2)
            for item in result["paths"]:
                for extension in ("csv", "svg", "html", "json"):
                    path = Path(item[extension])
                    self.assertTrue(path.is_absolute())
                    self.assertTrue(path.is_file())
                    self.assertIn(run, path.name)
                metadata = json.loads(Path(item["json"]).read_text(encoding="utf-8"))
                self.assertEqual(metadata["record"]["run_id"], run)
                self.assertNotEqual(metadata["record"]["travel_mm"], 1234)
            self.assertTrue(any("CSV: " + temp in message for message in rig.output))

    def test_bad_test_result_never_returns_from_false_home_or_homes_next_axis(self):
        rig = Rig()
        rig.workflow.cmd_TEST(GCmd())
        rig.home_results.append({"travel_mm": 15.0})
        with self.assertRaisesRegex(CommandError, "no automatic move"):
            rig.confirm()
        self.assertEqual(rig.scripts, ["G28 X"])
        self.assertEqual(rig.toolhead.moves, [])
        self.assertEqual(rig.workflow.last_result["result"], "rejected")


class PortableChartTests(unittest.TestCase):
    def test_failed_chart_write_publishes_nothing_and_previous_runs_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = {"samples": [(0.0, 0.0, 0.0, 1.0)],
                       "baseline_g": (0.0, 0.0, 1.0), "dropped": 0}
            with self.assertRaises(TypeError):
                write_capture(temp, "failed", "x", capture, {"unserializable": object()})
            self.assertEqual(list(Path(temp).iterdir()), [])
            original = write_capture(temp, "completed", "x", capture, {})
            before = Path(original["csv"]).read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_capture(temp, "completed", "x", capture, {})
            self.assertEqual(Path(original["csv"]).read_bytes(), before)

    def test_csv_has_raw_and_baseline_subtracted_samples_and_svg_is_valid_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            record = {"onset_g": 4.0, "impact_peak_g": 6.0, "threshold_g": 3.0,
                      "direction": 0, "onset_time_s": 1.002}
            capture = {"samples": [(1.0, 1.0, 2.0, 3.0), (1.001, 1.0, 2.0, 3.0),
                                   (1.002, -5.0, 2.0, 3.0)],
                       "baseline_g": (1.0, 2.0, 3.0), "dropped": 0}
            paths = write_capture(temp, "synthetic_test", "x", capture, record, simulated=True)
            root = ET.parse(paths["svg"]).getroot()
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            with open(paths["csv"], newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(float(rows[-1]["raw_x_g"]), -5.0)
            self.assertEqual(float(rows[-1]["delta_x_g"]), -6.0)
            svg = Path(paths["svg"]).read_text(encoding="utf-8")
            self.assertIn("SIMULATED / NOT PRINTER MEASUREMENT", svg)
            self.assertIn("+3.00 g threshold", svg)
            self.assertIn("-3.00 g threshold", svg)
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertNotIn("https://", page)
            self.assertIn(Path(paths["csv"]).name, page)

    def test_chart_rejects_empty_stale_overflow_or_invalid_data_and_path_injection(self):
        for capture in ({"samples": []}, {"samples": [(1.0, 0, 0, 0)], "dropped": 1},
                        {"samples": [(1.0, 0, 0, 0), (1.0, 0, 0, 0)]},
                        {"samples": [(1.0, float("nan"), 0, 0)]}):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    write_capture(temp, "run", "x", capture, {})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                write_capture(temp, "../outside", "x", {}, {})
            with self.assertRaises(ValueError):
                write_capture(temp, "run", "../../outside", {}, {})


if __name__ == "__main__":
    unittest.main()
