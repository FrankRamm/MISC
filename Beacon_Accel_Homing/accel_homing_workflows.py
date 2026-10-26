# Supervised calibration and measured charts for accelerometer homing. GPLv3.
"""Bounded X/Y workflows. No workflow command ever requests a Z move.

An accelerometer and motor step count cannot establish physical position.
The operator confirms the initial center. Later automatic returns rely on a
qualified center-to-stop home and remain an experimental physical heuristic.
"""
import datetime
from decimal import Decimal
import json
import logging
import math
import os
import re
import types
import uuid

try:
    from .accel_homing_charts import write_capture
except ImportError:
    from accel_homing_charts import write_capture

try:
    from .homing import PrinterHoming as _PrinterHoming
    from .safe_z_home import SafeZHoming as _SafeZHoming
except ImportError:
    # Standalone analysis can import this module without Klipper. Workflows
    # fail closed until the installed stock classes can be established.
    _PrinterHoming = _SafeZHoming = None

_PRINTER_G28 = _PrinterHoming.cmd_G28 if _PrinterHoming is not None else None
_SAFE_Z_G28 = _SafeZHoming.cmd_G28 if _SafeZHoming is not None else None

AXES = {"x": 0, "y": 1}
REQUIRED_SUCCESSES = 10
REPEATABILITY_LIMIT_MM = 1.5
MIN_TRAVEL = 10.0


class AccelHomingWorkflows:
    def __init__(self, owner, config):
        self.owner, self.config = owner, config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.model_name = config.get("model", "default")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.model_name):
            raise config.error("model must contain only letters, numbers, _ or -")
        self.model_section = "accel_homing_model " + self.model_name
        self.manual_axes = {a for a in AXES
                            if config.get(a + "_threshold", None) is not None}
        if config.get("threshold", None) is not None:
            self.manual_axes.update(AXES)
        self.model = None
        if config.has_section(self.model_section):
            self.model = self.printer.load_object(config, self.model_section)
            for axis, threshold in self.model.thresholds.items():
                if axis not in self.manual_axes:
                    owner.threshold_g[axis] = threshold
        self.start_threshold = config.getfloat("calibration_start_threshold", 0.5,
                                               minval=0.5, maxval=15.0)
        self.threshold_step = config.getfloat("calibration_step", 0.5,
                                              minval=0.1, maxval=3.0)
        self.max_threshold = config.getfloat("calibration_max_threshold", 12.0,
                                             minval=self.start_threshold, maxval=15.0)
        self.max_attempts = config.getint("calibration_max_attempts", 40,
                                          minval=12, maxval=100)
        self.confirm_timeout = config.getfloat("calibration_confirm_timeout", 300.0,
                                               minval=30.0, maxval=1800.0)
        self.travel_tolerance = config.getfloat("calibration_travel_tolerance", 0.20,
                                                minval=0.05, maxval=0.40)
        self.reposition_speed = config.getfloat("workflow_reposition_speed", 50.0,
                                                minval=1.0, maxval=150.0)
        if not all(math.isfinite(value) for value in (
                self.start_threshold, self.threshold_step, self.max_threshold,
                self.confirm_timeout, self.travel_tolerance, self.reposition_speed)):
            raise config.error("Accelerometer homing workflow settings must be finite")
        start_args = self.printer.get_start_args() if hasattr(self.printer, "get_start_args") else {}
        main_config = start_args.get("config_file")
        default_charts = (os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(main_config))),
                                      "accel_homing_charts") if main_config else
                          "~/printer_data/config/accel_homing_charts")
        self.charts_directory = os.path.abspath(os.path.expanduser(config.get(
            "charts_directory", default_charts)))
        self.kinematics_name = None
        if hasattr(config, "getsection"):
            self.kinematics_name = config.getsection("printer").get("kinematics")
        self.session = None
        self.last_result = None
        self.in_home = False
        self.home_axis = None
        self.timer = self.reactor.register_timer(self._timeout)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("klippy:connect", self._validate_model_connection)
        for name, callback, description in (
                ("ACCEL_HOMING_CALIBRATE", self.cmd_CALIBRATE,
                 "Calibrate one X or Y homing threshold with ten qualified homes"),
                ("ACCEL_HOMING_CANCEL", self.cmd_CANCEL,
                 "Cancel the current accelerometer homing workflow"),
                ("ACCEL_HOMING_TEST", self.cmd_TEST,
                 "Test X then Y from an explicitly confirmed center"),
                ("ACCEL_HOMING_CHARTS_GENERATE", self.cmd_CHARTS,
                 "Home X then Y and export measured acceleration CSV/SVG/HTML"),
                ("ACCEL_HOMING_STATUS", self.cmd_STATUS,
                 "Show calibration progress, model and chart output paths")):
            self.gcode.register_command(name, callback, desc=description)

    def _error(self, message):
        return self.printer.command_error("accel_homing: " + message)

    def _info(self, message):
        self.gcode.respond_info("accel_homing: " + message)

    def _status(self, name):
        obj = self.printer.lookup_object(name, None)
        if obj is not None and hasattr(obj, "get_status"):
            return obj.get_status(self.reactor.monotonic())
        return {}

    def _printing(self):
        if self._status("print_stats").get("state") in ("printing", "paused"):
            return True
        if self._status("pause_resume").get("is_paused", False):
            return True
        virtual_sd = self.printer.lookup_object("virtual_sdcard", None)
        return (virtual_sd is not None and hasattr(virtual_sd, "is_active")
                and virtual_sd.is_active())

    @staticmethod
    def _bound_handler(handler, owner, function):
        return (type(handler) is types.MethodType and handler.__self__ is owner
                and handler.__func__ is function)

    def _stock_g28(self):
        """Resolve only stock G28 or one verified built-in safe_z_home wrapper."""
        if self.printer.lookup_object("homing_override", None) is not None:
            raise self._error("workflows require stock G28; remove homing_override first")
        if self.printer.lookup_object("gcode_macro G28", None) is not None:
            raise self._error("workflows require stock G28; a G28 macro is configured")
        normal = self.printer.lookup_object("homing", None)
        stock = getattr(normal, "cmd_G28", None)
        if (_PrinterHoming is None or type(normal) is not _PrinterHoming
                or getattr(normal, "printer", None) is not self.printer
                or not self._bound_handler(stock, normal, _PRINTER_G28)):
            raise self._error("cannot verify the stock G28 homing handler")
        registries = [getattr(self.gcode, name, None)
                      for name in ("ready_gcode_handlers", "gcode_handlers")]
        if any(not isinstance(handlers, dict) or "G28" not in handlers
               for handlers in registries):
            raise self._error("cannot verify the active and ready G28 handlers")
        safe = self.printer.lookup_object("safe_z_home", None)
        if safe is None:
            if not all(self._bound_handler(handlers["G28"], normal, _PRINTER_G28)
                       for handlers in registries):
                raise self._error("workflows require the unmodified stock G28 handler")
        elif (_SafeZHoming is None or type(safe) is not _SafeZHoming
              or getattr(safe, "printer", None) is not self.printer
              or getattr(safe, "gcode", None) is not self.gcode
              or not self._bound_handler(getattr(safe, "cmd_G28", None), safe, _SAFE_Z_G28)
              or not all(self._bound_handler(handlers["G28"], safe, _SAFE_Z_G28)
                         for handlers in registries)
              or not self._bound_handler(getattr(safe, "prev_G28", None), normal, _PRINTER_G28)):
            raise self._error("workflows require built-in safe_z_home with an unmodified stock "
                              "G28 predecessor; custom or nested wrappers are unsupported")
        if not callable(getattr(self.gcode, "create_gcode_command", None)):
            raise self._error("cannot create a verified X/Y G28 command")
        return stock

    def _preflight(self, axes, continuation=False):
        if hasattr(self.printer, "is_shutdown") and self.printer.is_shutdown():
            raise self._error("printer is shut down")
        if self._printing():
            raise self._error("cannot run while printing or paused")
        if not continuation and self._status("idle_timeout").get("state") == "Printing":
            raise self._error("printer is busy; wait until it is idle")
        if self.owner.homing_active or self.owner.attempt is not None:
            raise self._error("another homing operation is active")
        self._stock_g28()
        toolhead = self.owner.toolhead
        if toolhead is None:
            raise self._error("printer is not connected")
        self.owner.preflight_motion_policy(axes, error=self.printer.command_error)
        kin = toolhead.get_kinematics()
        name = self.kinematics_name
        if name is None:
            name = kin.__class__.__module__.rsplit(".", 1)[-1]
        if name not in ("cartesian", "corexy"):
            raise self._error("supervised workflows currently require cartesian or corexy "
                              "kinematics with Z independent of XY")
        if getattr(kin, "dual_carriage_axis", None) is not None:
            raise self._error("dual-carriage workflows are not supported")
        if not (isinstance(getattr(kin, "limits", None), list) and len(kin.limits) == 3):
            raise self._error("this kinematics cannot invalidate XY while retaining Z")
        self._xy_enable_lines()
        for axis in axes:
            rail = self.owner.rails_by_axis.get(axis)
            if rail is None or axis not in self.owner.endstops:
                raise self._error("axis %s must use this module's accelerometer endstop" % axis.upper())
            info = rail.get_homing_info()
            if info.retract_dist or getattr(info, "min_home_dist", 0.0):
                raise self._error("workflow axes require homing_retract_dist: 0 and min_home_dist: 0")
            low, high = rail.get_range()
            center = (low + high) * 0.5
            distance = (info.position_endstop - center) * (1.0 if info.positive_dir else -1.0)
            if not all(math.isfinite(v) for v in (low, high, info.position_endstop)) \
                    or high <= low or distance <= MIN_TRAVEL \
                    or not low <= info.position_endstop <= high:
                raise self._error("axis %s has invalid endstop, direction or insufficient center travel"
                                  % axis.upper())
            if not math.isfinite(info.speed) or info.speed <= 0.0:
                raise self._error("axis homing speed must be positive and finite")
        if hasattr(toolhead, "check_busy"):
            queued, estimated, empty = toolhead.check_busy(self.reactor.monotonic())
            if not empty or queued > estimated + 0.1:
                raise self._error("queued motion is still active")

    def _xy_enable_lines(self):
        toolhead = self.owner.toolhead
        manager = self.printer.lookup_object("stepper_enable", None)
        if toolhead is None or manager is None:
            raise self._error("stepper enable controls are unavailable")
        lines, z_lines = [], []
        for stepper in toolhead.get_kinematics().get_steppers():
            if not hasattr(stepper, "is_active_axis"):
                raise self._error("cannot establish which steppers affect Z")
            xy = any(stepper.is_active_axis(a) for a in "xy")
            z = stepper.is_active_axis("z")
            line = manager.lookup_enable(stepper.get_name())
            if xy and z:
                raise self._error("an XY stepper also drives Z; selective release is unsafe")
            if xy:
                lines.append(line)
            if z:
                z_lines.append(line)
        if not lines:
            raise self._error("no XY stepper enable lines were found")
        if any(line is other or getattr(line, "enable", object()) is getattr(other, "enable", None)
               for line in lines for other in z_lines):
            raise self._error("XY and Z share an enable line; cannot release XY independently")
        if any(hasattr(line, "has_dedicated_enable") and not line.has_dedicated_enable()
               and getattr(getattr(line, "enable", None), "mcu_enable", True) is None
               for line in lines):
            raise self._error("an XY motor has no controllable enable pin")
        return list(dict.fromkeys(lines))

    def _disable_xy(self):
        toolhead = self.owner.toolhead
        kin = toolhead.get_kinematics()
        limits = getattr(kin, "limits", None)
        if not isinstance(limits, list) or len(limits) != 3:
            raise self._error("this kinematics cannot invalidate XY while retaining Z")
        lines = self._xy_enable_lines()
        z_limit = limits[2]
        z_homed = "z" in kin.get_status(self.reactor.monotonic()).get("homed_axes", "")
        toolhead.wait_moves()
        toolhead.flush_step_generation()
        toolhead.dwell(0.1)
        print_time = toolhead.get_last_move_time()
        for line in lines:
            line.motor_disable(print_time)
        toolhead.dwell(0.1)
        # Stock cartesian/corexy use these ranges for homed state. The public
        # clear_homing_state API differs between Klipper (letters) and Kalico
        # (integer indices), so invalidate only the two verified XY ranges.
        limits[0] = limits[1] = (1.0, -1.0)
        homed = kin.get_status(self.reactor.monotonic()).get("homed_axes", "")
        if any(axis in homed for axis in AXES) or ("z" in homed) != z_homed \
                or kin.limits[2] != z_limit:
            raise self._error("could not invalidate XY while retaining Z state")
        toolhead.wait_moves()

    def _geometry(self, axis):
        rail = self.owner.rails_by_axis[axis]
        low, high = rail.get_range()
        center = (low + high) * 0.5
        return center, abs(center - rail.get_homing_info().position_endstop)

    def _model_context(self, axis):
        rail = self.owner.rails_by_axis[axis]
        low, high = rail.get_range()
        info = rail.get_homing_info()
        backend = getattr(self.owner, "backend", None)
        sensor = getattr(backend, "sensor", None)
        helper_config = getattr(getattr(self.owner, "helper", None), "config", None)
        mapping = getattr(sensor, "axes_map", None) if backend is not None else \
            getattr(helper_config, "axes_map", None)
        # JSON normalizes tuples from drivers so saved and live mapping compare.
        mapping = json.loads(json.dumps(mapping, allow_nan=False))
        return {"sensor": getattr(self.owner, "sensor_name", None),
                "backend": "beacon_raw" if backend is None else backend.kind,
                "sensor_axes_map": mapping,
                "geometry": {"minimum": low, "maximum": high,
                             "endstop": info.position_endstop,
                             "positive_dir": bool(info.positive_dir), "center": (low + high) * 0.5},
                "homing_speed_mm_s": info.speed,
                "homing_accel_mm_s2": self.owner.homing_accel,
                "impact_sign": self.owner.impact_sign[axis]}

    def _validate_model_connection(self):
        if self.model is None:
            return
        for axis in self.model.thresholds:
            if axis in self.manual_axes:
                continue
            if axis not in self.owner.rails_by_axis:
                raise self.printer.config_error("Saved %s model has no configured accelerometer endstop"
                                                % axis.upper())
            saved = self.model.metadata[axis]
            current = self._model_context(axis)
            for key, value in current.items():
                if key not in saved or saved[key] != value:
                    raise self.printer.config_error(
                        "Saved %s homing model does not match current %s; recalibrate "
                        "or set an explicit %s_threshold override" % (axis.upper(), key, axis))

    def _snapshot(self):
        return (tuple(self.owner.toolhead.get_position()),
                self._status("toolhead").get("homed_axes", ""),
                tuple(sorted(self.owner.threshold_g.items())),
                tuple(sorted(self.owner.impact_sign.items())),
                self.owner.homing_accel, self.owner.back_off_dist)

    def _restore(self):
        session = self.session
        if session is None:
            return
        try:
            self.owner._end_session()
        finally:
            self.owner.threshold_g.update(session["saved_thresholds"])
            self.owner.back_off_dist = session["saved_back_off"]
            self.in_home, self.home_axis = False, None

    def _finish(self, result, release=False):
        try:
            self._restore()
            if release:
                try:
                    self._disable_xy()
                except Exception:
                    logging.exception("accel_homing: unable to release/invalidate XY during cleanup")
        finally:
            self.last_result = result
            self.session = None
            self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _shutdown(self, *args):
        if self.session is not None:
            self._finish({"result": "aborted", "reason": "printer shutdown"})

    def _timeout(self, eventtime):
        if self.session is None or self.session.get("state") != "waiting":
            return self.reactor.NEVER
        if eventtime >= self.session["expires"]:
            self._finish({"result": "cancelled", "reason": "manual-centering wait expired"})
            self._info("manual-centering wait expired; workflow cancelled")
            return self.reactor.NEVER
        return self.session["expires"]

    def check_home_axes(self, axes):
        """Called by core before an accelerometer homing session can move."""
        if self.session is not None and (not self.in_home or set(axes) != {self.home_axis}):
            raise self._error("a supervised workflow is active; resume calibration with its "
                              "command or use CANCEL")

    def check_home_result(self, record):
        # Single-touch geometry is enforced before any workflow motion. Record
        # qualification therefore happens immediately after stock G28 returns;
        # it does not force stock G28's all-motor fault cleanup for an early hit.
        if self.in_home and record.get("axis", "").lower() != self.home_axis:
            raise self._error("unexpected homing result axis")

    def check_settings_change(self):
        if self.session is not None:
            raise self._error("cancel the active workflow before changing homing settings")

    def _require_centered(self, gcmd):
        if str(gcmd.get("CENTERED", "")).strip() != "1":
            raise gcmd.error("Manually center XY first, then run this command with CENTERED=1")

    def _require_unhomed_xy(self):
        homed = self.owner.toolhead.get_kinematics().get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if any(axis in homed for axis in AXES):
            raise self._error("workflow requires both X and Y unhomed; release XY and "
                              "manually center the toolhead before running with CENTERED=1")

    def _begin(self, gcmd, mode, axis=None):
        if self.session is not None:
            raise gcmd.error("A workflow is already active; use ACCEL_HOMING_CANCEL first")
        axes = [axis] if axis else ["x", "y"]
        self._preflight(axes)
        self._require_unhomed_xy()
        self.session = {"mode": mode, "axis": axis, "state": "starting",
                        "run_id": uuid.uuid4().hex, "successes": 0, "attempts": 0,
                        "candidate_qualified": False, "candidate_attempt": None,
                        "validation_records": [], "repeatability_spread_mm": None,
                        "records": [], "paths": [], "candidate": self.start_threshold,
                        "saved_thresholds": dict(self.owner.threshold_g),
                        "saved_back_off": self.owner.back_off_dist,
                        "save_config": bool(gcmd.get_int("SAVE_CONFIG", 1, minval=0, maxval=1))}

    def _wait_center(self, reason):
        self._restore()
        self._disable_xy()
        session = self.session
        session["state"] = "waiting"
        session["expires"] = self.reactor.monotonic() + self.confirm_timeout
        session["snapshot"] = self._snapshot()
        self.reactor.update_timer(self.timer, session["expires"])
        centers = {session["axis"]: self._geometry(session["axis"])[0]}
        instruction = "Move XY manually to the center of their travel, then repeat the command. "
        instruction += "Expected centers: " + ", ".join("%s=%.2f mm" % (a.upper(), v)
                                                       for a, v in centers.items())
        command = "ACCEL_HOMING_CALIBRATE AXIS=%s CENTERED=1" % session["axis"].upper()
        self._info("%s. %s Command: %s (expires in %.0f s)" % (
            reason, instruction, command, self.confirm_timeout))

    def _threshold_limit(self):
        scale = getattr(self.owner, "full_scale_g", None)
        if scale is None and getattr(self.owner, "helper", None) is not None:
            scale = self.owner._full_scale_g(self.owner.scale)
        return min(self.max_threshold, 0.8 * scale) if scale else self.max_threshold

    def _home(self, axis, threshold=None):
        if axis not in AXES:
            raise self._error("workflow homing requires exactly one X or Y axis")
        session = self.session
        if session["attempts"] >= self.max_attempts:
            raise self._error("maximum homing attempts reached")
        if self._printing():
            raise self._error("printing or pause detected during workflow")
        session["attempts"] += 1
        if threshold is not None:
            if threshold > self._threshold_limit():
                raise self._error("candidate exceeds the safe threshold ceiling")
            self.owner.threshold_g[axis] = threshold
        self.owner.back_off_dist = 0.0
        old_record = self.owner.last.get(axis)
        self.in_home, self.home_axis = True, axis
        capture_started = False
        try:
            self.owner.start_capture()
            capture_started = True
            letter = axis.upper()
            gcmd = self.gcode.create_gcode_command("G28", "G28 " + letter, {letter: ""})
            # safe_z_home can hop Z even for G28 X/Y. Invoke only its verified
            # stock predecessor for this trial, retaining the public wrapper
            # and all normal homing events. Recheck after capture setup yields.
            self._stock_g28()(gcmd)
            self.owner.toolhead.wait_moves()
        finally:
            self.in_home, self.home_axis = False, None
            try:
                self.owner._end_session()
            finally:
                capture = self.owner.stop_capture() if capture_started else {"samples": [], "dropped": 0}
        record = self.owner.last.get(axis)
        if record is None or record is old_record:
            raise self._error("G28 returned without a new accelerometer homing result")
        record = dict(record)
        record["run_id"] = session["run_id"]
        record["workflow_attempt"] = session["attempts"]
        record["workflow_threshold_g"] = (threshold if threshold is not None
                                          else self.owner.threshold_g[axis])
        record["workflow_phase"] = ("validation" if session["candidate_qualified"]
                                    else "discovery") if session["mode"] == "calibration" \
            else session["mode"]
        if len(capture.get("samples", ())) < 2 or capture.get("dropped", 0):
            raise self._error("homing capture is empty or incomplete; sensor data is not usable")
        previous = None
        for sample in capture["samples"]:
            if len(sample) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                           for v in sample) \
                    or (previous is not None and sample[0] <= previous):
                raise self._error("homing capture has invalid or stale sensor samples")
            previous = sample[0]
        session["records"].append(record)
        self._info("%s home: result %s; onset %s g; peak %s g; commanded travel %s mm" % (
            axis.upper(), record.get("result"), record.get("onset_g"),
            record.get("impact_peak_g"), record.get("travel_mm")))
        return record, capture

    def _quality(self, axis, record):
        result = record.get("result")
        travel = record.get("travel_mm")
        if result not in ("impact", "pressed"):
            return "fatal", "sensor/homing failure (%s)" % result
        if not isinstance(travel, (int, float)) or not math.isfinite(travel):
            return "fatal", "missing commanded travel measurement"
        if result == "pressed" or travel <= MIN_TRAVEL:
            return "early", "pressed start or early trigger at %.2f mm (must exceed 10 mm)" % travel
        if not (record.get("moving") or record.get("motion_seen")):
            return "fatal", "accelerometer did not establish motion before impact"
        _center, expected = self._geometry(axis)
        if travel < expected * (1.0 - self.travel_tolerance):
            return "early", "trigger at %.2f mm is materially short of expected center travel %.2f mm" % (
                travel, expected)
        if travel > expected * (1.0 + self.travel_tolerance):
            return "fatal", "travel %.2f mm exceeds expected center travel %.2f mm" % (travel, expected)
        return "good", "qualified center-to-stop home"

    @staticmethod
    def _repeatability_spread(records):
        travels = [record["travel_mm"] for record in records]
        # Reported millimeter values have decimal precision. Subtract their
        # decimal endpoints so an exact 1.5 mm range cannot round below it.
        return float(Decimal(str(max(travels))) - Decimal(str(min(travels))))

    def _recenter(self, axis):
        # Called only after _quality accepted this exact axis's current home.
        if axis not in self.owner.toolhead.get_kinematics().get_status(
                self.reactor.monotonic()).get("homed_axes", ""):
            raise self._error("axis lost homing before return to center")
        center, _expected = self._geometry(axis)
        coordinates = [None, None, None]
        coordinates[AXES[axis]] = center
        info = self.owner.rails_by_axis[axis].get_homing_info()
        self.owner.toolhead.manual_move(coordinates, min(self.reposition_speed, info.speed))
        self.owner.toolhead.wait_moves()

    def _calibrate_trials(self):
        session = self.session
        axis = session["axis"]
        while session["successes"] < REQUIRED_SUCCESSES:
            record, _capture = self._home(axis, session["candidate"])
            quality, reason = self._quality(axis, record)
            if quality == "fatal":
                raise self._error(reason + "; calibration aborted without raising the threshold")
            if quality == "early":
                session["successes"] = 0
                session["candidate_qualified"] = False
                session["candidate_attempt"] = None
                session["validation_records"] = []
                session["repeatability_spread_mm"] = None
                candidate = round(session["candidate"] + self.threshold_step, 6)
                if candidate > self._threshold_limit() or session["attempts"] >= self.max_attempts:
                    raise self._error(reason + "; calibration threshold/attempt ceiling reached")
                session["candidate"] = candidate
                self._wait_center(reason + "; retry at %.2f g" % candidate)
                return
            if not session["candidate_qualified"]:
                session["candidate_qualified"] = True
                session["candidate_attempt"] = record["workflow_attempt"]
                self._info("%s %.2f g candidate qualified; starting ten repeat validation homes "
                           "with commanded travel spread (max-min) strictly < %.2f mm" % (
                               axis.upper(), session["candidate"], REPEATABILITY_LIMIT_MM))
                self._recenter(axis)
                continue
            session["validation_records"].append(record)
            session["successes"] = len(session["validation_records"])
            spread = session["repeatability_spread_mm"] = self._repeatability_spread(
                session["validation_records"])
            if spread >= REPEATABILITY_LIMIT_MM:
                raise self._error(
                    "%s calibration repeatability spread %s mm across %d/10 validation homes "
                    "must be strictly < %.2f mm; calibration aborted without raising the threshold; "
                    "no automatic move follows this result; manually recenter" % (
                        axis.upper(), spread, session["successes"], REPEATABILITY_LIMIT_MM))
            self._info("%s calibration %.2f g: %d/10 qualified homes; commanded travel spread "
                       "%s mm (must be strictly < %.2f mm)" % (
                           axis.upper(), session["candidate"], session["successes"],
                           spread, REPEATABILITY_LIMIT_MM))
            if session["successes"] < REQUIRED_SUCCESSES:
                self._recenter(axis)
        self._save_model()

    def _qualified_validation_records(self):
        """Recheck the entire current-candidate window before any config writes."""
        session = self.session
        message = ("cannot save calibration without a qualified discovery and ten distinct "
                   "current-candidate validation homes with commanded travel spread "
                   "strictly < %.2f mm" % REPEATABILITY_LIMIT_MM)
        if not isinstance(session, dict):
            raise self._error(message)
        axis, threshold = session.get("axis"), session.get("candidate")
        candidate_attempt = session.get("candidate_attempt")
        records, qualified = session.get("records"), session.get("validation_records")
        if session.get("mode") != "calibration" or session.get("state") != "running" \
                or not isinstance(axis, str) or axis not in AXES \
                or session.get("candidate_qualified") is not True \
                or session.get("successes") != REQUIRED_SUCCESSES \
                or not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
                or not math.isfinite(threshold) or not 0.5 <= threshold <= self._threshold_limit() \
                or type(candidate_attempt) is not int or candidate_attempt < 1 \
                or type(session.get("attempts")) is not int \
                or session.get("attempts") != candidate_attempt + REQUIRED_SUCCESSES \
                or not isinstance(records, list) or len(records) != session["attempts"] \
                or not isinstance(qualified, list) or len(qualified) != REQUIRED_SUCCESSES \
                or not isinstance(session.get("run_id"), str) or not session["run_id"] \
                or type(session.get("save_config")) is not bool:
            raise self._error(message)
        discovery = records[candidate_attempt - 1]
        window = [discovery] + qualified
        if len({id(record) for record in window}) != REQUIRED_SUCCESSES + 1 \
                or any(actual is not expected for actual, expected in
                       zip(qualified, records[-REQUIRED_SUCCESSES:])):
            raise self._error(message)
        for index, record in enumerate(window):
            if not isinstance(record, dict) or record.get("axis") != axis \
                    or record.get("run_id") != session["run_id"] \
                    or type(record.get("workflow_attempt")) is not int \
                    or record.get("workflow_attempt") != candidate_attempt + index \
                    or record.get("workflow_threshold_g") != threshold \
                    or record.get("workflow_phase") != ("discovery" if index == 0 else "validation") \
                    or self._quality(axis, record)[0] != "good":
                raise self._error(message)
        if self._repeatability_spread(qualified) >= REPEATABILITY_LIMIT_MM:
            raise self._error(message)
        return qualified

    def _save_model(self):
        qualified = self._qualified_validation_records()
        session = self.session
        axis, threshold = session["axis"], session["candidate"]
        minimum = min(r["travel_mm"] for r in qualified)
        maximum = max(r["travel_mm"] for r in qualified)
        spread = self._repeatability_spread(qualified)
        metadata = {"run_id": session["run_id"], **self._model_context(axis),
                    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "expected_center_travel_mm": self._geometry(axis)[1],
                    "travel_tolerance_fraction": self.travel_tolerance,
                    "min_commanded_travel_mm": minimum,
                    "max_commanded_travel_mm": maximum,
                    "repeatability_spread_mm": spread,
                    "repeatability_limit_mm": REPEATABILITY_LIMIT_MM,
                    "onset_g": [r.get("onset_g") for r in qualified],
                    "impact_peak_g": [r.get("impact_peak_g") for r in qualified],
                    "physical_position_verified": False}
        encoded_metadata = json.dumps(metadata, separators=(",", ":"), allow_nan=False)
        configfile = self.printer.lookup_object("configfile")
        configfile.set(self.model_section, "version", "1")
        configfile.set(self.model_section, axis + "_threshold", "%.6f" % threshold)
        configfile.set(self.model_section, axis + "_successes", str(REQUIRED_SUCCESSES))
        configfile.set(self.model_section, axis + "_metadata", encoded_metadata)
        result = {"result": "calibrated", "axis": axis, "threshold_g": threshold,
                  "successes": REQUIRED_SUCCESSES, "model": self.model_name,
                  "run_id": session["run_id"], "save_config": session["save_config"],
                  "repeatability_spread_mm": spread,
                  "repeatability_limit_mm": REPEATABILITY_LIMIT_MM}
        save = session["save_config"]
        self._finish(result)
        self._info("%s model %s calibrated at %.2f g after ten qualified homes with commanded "
                   "travel spread %s mm < %.2f mm. %s" % (
            axis.upper(), self.model_name, threshold, spread, REPEATABILITY_LIMIT_MM,
            "Explicit manual threshold overrides remain active." if axis in self.manual_axes
            else "The saved model supplies this axis threshold after restart."))
        if save:
            self._info("Saving model to printer.cfg SAVE_CONFIG block; Klipper will restart")
            try:
                self.gcode.run_script_from_command("SAVE_CONFIG")
            except Exception:
                self.last_result["result"] = "save_failed"
                self.last_result["save_config"] = False
                raise
        else:
            self._info("Model staged only. Run SAVE_CONFIG to write printer.cfg and restart")

    def cmd_CALIBRATE(self, gcmd):
        axis = gcmd.get("AXIS", "").lower()
        if axis not in AXES:
            raise gcmd.error("AXIS must be X or Y")
        self._require_centered(gcmd)
        session = self.session
        if session is not None:
            if session["mode"] != "calibration" or session["axis"] != axis \
                    or session.get("state") != "waiting":
                raise gcmd.error("A workflow is already active; resume calibration on its "
                                 "same axis or use ACCEL_HOMING_CANCEL first")
            if gcmd.get("SAVE_CONFIG", None) is not None and bool(gcmd.get_int(
                    "SAVE_CONFIG", minval=0, maxval=1)) != session["save_config"]:
                raise gcmd.error("SAVE_CONFIG cannot change while resuming calibration")
            self._continue_calibration()
            return
        self._begin(gcmd, "calibration", axis)
        try:
            self.session["state"] = "running"
            self._calibrate_trials()
        except Exception as exc:
            if self.session is not None:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    def _continue_calibration(self):
        session = self.session
        try:
            if self.reactor.monotonic() >= session["expires"]:
                raise self._error("manual-centering wait expired")
            self._preflight([session["axis"]], continuation=True)
            if self._snapshot() != session["snapshot"]:
                raise self._error("printer state changed while awaiting manual recentering; restart workflow")
            self._require_unhomed_xy()
            session["state"] = "running"
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
            self._calibrate_trials()
        except Exception as exc:
            if self.session is not None:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    def cmd_CANCEL(self, gcmd):
        if self.session is None:
            gcmd.respond_info("No accelerometer homing workflow is active")
            return
        self._finish({"result": "cancelled", "reason": "operator cancelled"})
        gcmd.respond_info("Accelerometer homing workflow cancelled; settings restored")

    def _start_xy(self, gcmd, mode):
        self._require_centered(gcmd)
        self._begin(gcmd, mode)
        try:
            self.session["state"] = "running"
            self._test_xy()
        except Exception as exc:
            if self.session is not None:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    def cmd_TEST(self, gcmd):
        self._start_xy(gcmd, "test")

    def cmd_CHARTS(self, gcmd):
        self._start_xy(gcmd, "charts")

    def _test_xy(self):
        session = self.session
        for axis in "xy":
            record, capture = self._home(axis)
            quality, reason = self._quality(axis, record)
            if session["mode"] == "charts":
                paths = write_capture(self.charts_directory, session["run_id"], axis, capture, record)
                session["paths"].append({"axis": axis, **paths})
                self._info("%s measured acceleration files:\n%s" % (
                    axis.upper(), "\n".join("%s: %s" % (kind.upper(), path)
                                           for kind, path in paths.items())))
            if quality != "good":
                paths = list(session["paths"])
                self._finish({"result": "rejected", "reason": reason, "paths": paths}, release=True)
                raise self._error(reason + "; no automatic move follows this result; manually recenter")
            if axis == "x":
                # Return only the trusted X axis to its center before Y homes;
                # Y retains the physical center the operator confirmed.
                self._recenter("x")
        self._finish({"result": "complete", "mode": session["mode"],
                      "run_id": session["run_id"], "records": list(session["records"]),
                      "paths": list(session["paths"])})
        self._info("X and Y %s complete; these measurements do not certify physical stop position"
                   % session["mode"])

    def get_status(self, eventtime):
        session = self.session
        active = None if session is None else {
            key: session.get(key) for key in ("mode", "state", "axis", "candidate", "successes",
                                             "candidate_qualified", "attempts", "run_id", "expires",
                                             "repeatability_spread_mm")}
        if active is not None:
            active["repeatability_limit_mm"] = REPEATABILITY_LIMIT_MM
        return {"model": self.model_name, "manual_threshold_overrides": sorted(self.manual_axes),
                "active": active, "last_result": self.last_result,
                "charts_directory": self.charts_directory}

    def cmd_STATUS(self, gcmd):
        self.owner.cmd_STATUS(gcmd)
        gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), sort_keys=True))
