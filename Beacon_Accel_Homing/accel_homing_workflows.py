# Supervised calibration and measured charts for accelerometer homing. GPLv3.
"""Bounded X/Y workflows. No workflow command ever requests a Z move.

An accelerometer and motor step count cannot establish physical position.
The operator confirms the initial center. Later automatic returns rely on a
qualified center-to-stop home and remain an experimental physical heuristic.
"""
import datetime
import json
import logging
import math
import os
import re
import uuid

try:
    from .accel_homing_charts import write_capture
except ImportError:
    from accel_homing_charts import write_capture

AXES = {"x": 0, "y": 1}
REQUIRED_SUCCESSES = 10
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
        self.start_threshold = config.getfloat("calibration_start_threshold", 3.0,
                                               minval=3.0, maxval=15.0)
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
                ("ACCEL_HOMING_CONFIRM", self.cmd_CONFIRM,
                 "Confirm manual centering for the current supervised workflow"),
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

    def _preflight(self, axes, continuation=False):
        if hasattr(self.printer, "is_shutdown") and self.printer.is_shutdown():
            raise self._error("printer is shut down")
        if self._printing():
            raise self._error("cannot run while printing or paused")
        if not continuation and self._status("idle_timeout").get("state") == "Printing":
            raise self._error("printer is busy; wait until it is idle")
        if self.owner.homing_active or self.owner.attempt is not None:
            raise self._error("another homing operation is active")
        if self.printer.lookup_object("homing_override", None) is not None:
            raise self._error("workflows require stock G28; remove homing_override first")
        if self.printer.lookup_object("gcode_macro G28", None) is not None:
            raise self._error("workflows require stock G28; a G28 macro is configured")
        # A safe_z_home wrapper or a renamed macro can also add unrelated moves.
        handler = getattr(self.gcode, "ready_gcode_handlers", {}).get("G28")
        if handler is not None:
            normal = self.printer.lookup_object("homing", None)
            if normal is None or handler != getattr(normal, "cmd_G28", None):
                raise self._error("workflows require the unmodified stock G28 handler")
        toolhead = self.owner.toolhead
        if toolhead is None:
            raise self._error("printer is not connected")
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

    def _close_prompt(self):
        self.gcode.respond_info("action:prompt_end")

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
            self._close_prompt()

    def _shutdown(self, *args):
        if self.session is not None:
            self._finish({"result": "aborted", "reason": "printer shutdown"})

    def _timeout(self, eventtime):
        if self.session is None or self.session.get("state") != "waiting":
            return self.reactor.NEVER
        if eventtime >= self.session["expires"]:
            self._finish({"result": "cancelled", "reason": "confirmation expired"})
            self._info("confirmation expired; workflow cancelled")
            return self.reactor.NEVER
        return self.session["expires"]

    def check_home_axes(self, axes):
        """Called by core before an accelerometer homing session can move."""
        if self.session is not None and (not self.in_home or set(axes) != {self.home_axis}):
            raise self._error("a supervised workflow is active; resume its prompt or use CANCEL")

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
            raise gcmd.error("Manually center XY first, then explicitly confirm with CENTERED=1")

    def _require_unhomed_xy(self):
        homed = self.owner.toolhead.get_kinematics().get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if any(axis in homed for axis in AXES):
            raise self._error("calibration requires both X and Y unhomed; release XY and "
                              "manually center the toolhead before CALIBRATE CENTERED=1")

    def _begin(self, gcmd, mode, axis=None):
        if self.session is not None:
            raise gcmd.error("A workflow is already active; use ACCEL_HOMING_CANCEL first")
        axes = [axis] if axis else ["x", "y"]
        self._preflight(axes)
        if mode == "calibration":
            self._require_unhomed_xy()
        self.session = {"mode": mode, "axis": axis, "state": "starting",
                        "run_id": uuid.uuid4().hex, "successes": 0, "attempts": 0,
                        "candidate_qualified": False,
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
        centers = {a: self._geometry(a)[0] for a in ([session["axis"]] if session["axis"] else "xy")}
        instruction = "Move XY manually to the center of their travel, then confirm. "
        instruction += "Expected centers: " + ", ".join("%s=%.2f mm" % (a.upper(), v)
                                                       for a, v in centers.items())
        command = ("ACCEL_HOMING_CALIBRATE AXIS=%s CENTERED=1" % session["axis"].upper()
                   if session["mode"] == "calibration" else "ACCEL_HOMING_CONFIRM CENTERED=1")
        self._info("%s. %s Command: %s (expires in %.0f s)" % (
            reason, instruction, command, self.confirm_timeout))
        # Mainsail and Fluidd both understand Klipper action prompt responses.
        for message in ("action:prompt_begin Accelerometer homing - manual center",
                        "action:prompt_text " + reason,
                        "action:prompt_text " + instruction,
                        "action:prompt_text Verify the toolhead is clear before confirming.",
                        "action:prompt_footer_button Centered - continue|%s|primary" % command,
                        "action:prompt_footer_button Cancel|ACCEL_HOMING_CANCEL|error",
                        "action:prompt_show"):
            self.gcode.respond_info(message)

    def _threshold_limit(self):
        scale = getattr(self.owner, "full_scale_g", None)
        if scale is None and getattr(self.owner, "helper", None) is not None:
            scale = self.owner._full_scale_g(self.owner.scale)
        return min(self.max_threshold, 0.8 * scale) if scale else self.max_threshold

    def _home(self, axis, threshold=None):
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
            self.gcode.run_script_from_command("G28 " + axis.upper())
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
                candidate = round(session["candidate"] + self.threshold_step, 6)
                if candidate > self._threshold_limit() or session["attempts"] >= self.max_attempts:
                    raise self._error(reason + "; calibration threshold/attempt ceiling reached")
                session["candidate"] = candidate
                self._wait_center(reason + "; retry at %.2f g" % candidate)
                return
            if not session["candidate_qualified"]:
                session["candidate_qualified"] = True
                self._info("%s %.2f g candidate qualified; starting ten repeat validation homes" % (
                    axis.upper(), session["candidate"]))
                self._recenter(axis)
                continue
            session["successes"] += 1
            self._info("%s calibration %.2f g: %d/10 qualified homes" % (
                axis.upper(), session["candidate"], session["successes"]))
            if session["successes"] < REQUIRED_SUCCESSES:
                self._recenter(axis)
        self._save_model()

    def _save_model(self):
        session = self.session
        axis, threshold = session["axis"], session["candidate"]
        qualified = session["records"][-REQUIRED_SUCCESSES:]
        metadata = {"run_id": session["run_id"], **self._model_context(axis),
                    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "expected_center_travel_mm": self._geometry(axis)[1],
                    "travel_tolerance_fraction": self.travel_tolerance,
                    "min_commanded_travel_mm": min(r["travel_mm"] for r in qualified),
                    "max_commanded_travel_mm": max(r["travel_mm"] for r in qualified),
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
                  "run_id": session["run_id"], "save_config": session["save_config"]}
        save = session["save_config"]
        self._finish(result)
        self._info("%s model %s calibrated at %.2f g after ten qualified homes. %s" % (
            axis.upper(), self.model_name, threshold,
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
            self._continue()
            return
        self._begin(gcmd, "calibration", axis)
        try:
            self.session["state"] = "running"
            self._calibrate_trials()
        except Exception as exc:
            if self.session is not None:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    def cmd_CONFIRM(self, gcmd):
        session = self.session
        if session is None or session.get("state") != "waiting":
            raise gcmd.error("No workflow is waiting for manual centering")
        if session["mode"] == "calibration":
            raise gcmd.error("Resume with ACCEL_HOMING_CALIBRATE AXIS=%s CENTERED=1 after "
                             "manually centering XY" % session["axis"].upper())
        self._require_centered(gcmd)
        self._continue()

    def _continue(self):
        session = self.session
        try:
            if self.reactor.monotonic() >= session["expires"]:
                raise self._error("manual-centering confirmation expired")
            self._preflight([session["axis"]] if session["axis"] else "xy", continuation=True)
            if self._snapshot() != session["snapshot"]:
                raise self._error("printer state changed while awaiting confirmation; restart workflow")
            if session["mode"] == "calibration":
                self._require_unhomed_xy()
            session["state"] = "running"
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
            self._close_prompt()
            if session["mode"] == "calibration":
                self._calibrate_trials()
            else:
                self._test_xy()
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
        self._begin(gcmd, mode)
        try:
            self._wait_center("%s will home X and then Y with their current thresholds" % mode)
        except Exception:
            self._finish({"result": "aborted", "reason": "manual-center prompt failed"})
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
                                             "candidate_qualified", "attempts", "run_id", "expires")}
        return {"model": self.model_name, "manual_threshold_overrides": sorted(self.manual_axes),
                "active": active, "last_result": self.last_result,
                "charts_directory": self.charts_directory}

    def cmd_STATUS(self, gcmd):
        self.owner.cmd_STATUS(gcmd)
        gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), sort_keys=True))
