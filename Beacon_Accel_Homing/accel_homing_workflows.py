# Supervised calibration and measured charts for accelerometer homing. GPLv3.
"""Bounded X/Y workflows. No workflow command ever requests a Z move.

An accelerometer and motor step count cannot establish physical position.
The operator confirms the initial center. Later automatic returns rely on a
qualified center-to-stop home and remain an experimental physical heuristic.
"""
import datetime
import configparser
from decimal import Decimal
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import types
import sys
import urllib.parse
import uuid

try:
    from .accel_homing_charts import (PNGRenderer, default_dependency_directory, preflight_renderer,
                                      validate_accuracy, write_accuracy, write_capture, write_noise,
                                      write_noise_capture)
except ImportError:
    from accel_homing_charts import (PNGRenderer, default_dependency_directory, preflight_renderer,
                                     validate_accuracy, write_accuracy, write_capture, write_noise,
                                     write_noise_capture)

try:
    from .homing import PrinterHoming as _PrinterHoming
    from .safe_z_home import SafeZHoming as _SafeZHoming
except ImportError:
    # Standalone analysis can import this module without Klipper. Workflows
    # fail closed until the installed stock classes can be established.
    _PrinterHoming = _SafeZHoming = None

try:
    from .homing_override import HomingOverride as _HomingOverride
except ImportError:
    _HomingOverride = None

_PRINTER_G28 = _PrinterHoming.cmd_G28 if _PrinterHoming is not None else None
_SAFE_Z_G28 = _SafeZHoming.cmd_G28 if _SafeZHoming is not None else None
_OVERRIDE_G28 = _HomingOverride.cmd_G28 if _HomingOverride is not None else None

AXES = {"x": 0, "y": 1}
REQUIRED_SUCCESSES = 10
REPEATABILITY_LIMIT_MM = 1.5
MIN_TRAVEL = 10.0
NOISE_ITERATIONS = 3
NOISE_DURATION = 1.0


def _moonraker_read_bytes(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Moonraker discovery input exceeds its byte bound")
    return data


def _moonraker_path_environment(process):
    allowed = {b"HOME", b"MOONRAKER_DATA_PATH", b"MOONRAKER_CONFIG_PATH"}
    environment = {}
    for field in _moonraker_read_bytes(process / "environ", 65536).split(b"\0"):
        key, separator, value = field.partition(b"=")
        if separator and key in allowed:
            name = key.decode("ascii")
            if name in environment:
                raise ValueError("ambiguous Moonraker path environment")
            environment[name] = value.decode("utf-8")
    return environment


def _moonraker_expand_path(value, environment, uid):
    if value.startswith("~"):
        home = environment.get("HOME")
        if home is None:
            try:
                import pwd
                home = pwd.getpwuid(uid).pw_dir
            except (ImportError, KeyError):
                home = None
        if (not home or not Path(home).is_absolute()
                or (value != "~" and not value.startswith("~/"))):
            raise ValueError("cannot establish Moonraker user-home expansion")
        value = home.rstrip("/") + value[1:] or "/"
    return Path(value)


def verified_moonraker_config_root(start_args, proc_root="/proc"):
    """Correlate one same-user Moonraker data path to this Klippy Unix socket.

    Main-config containment alone does not identify Moonraker's config root.
    Unresolved includes, wrappers or paths deliberately return no web mapping.
    """
    socket = start_args.get("apiserver")
    if not socket or not hasattr(os, "getuid"):
        return None
    expected, roots = Path(socket).expanduser().resolve(), []
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            uid = process.stat().st_uid
            if uid != os.getuid():
                continue
            data = _moonraker_read_bytes(process / "cmdline", 65536)
            argv = [part.decode("utf-8") for part in data.split(b"\0") if part]
            indexes = [index for index, value in enumerate(argv) if Path(value).name == "moonraker.py"]
            indexes += [index + 1 for index, value in enumerate(argv[:-1])
                        if value == "-m" and argv[index + 1] == "moonraker"]
            if len(indexes) != 1:
                continue
            environment = _moonraker_path_environment(process)
            options = {}
            iterator = iter(argv[indexes[0] + 1:])
            for value in iterator:
                key, separator, inline = value.partition("=")
                if key in ("-d", "--datapath", "-c", "--configfile"):
                    name = "data" if key in ("-d", "--datapath") else "config"
                    options[name] = inline if separator else next(iterator, "")
                elif (key.startswith(("-d", "-c")) or
                      (key.startswith("--") and any(option.startswith(key)
                                                     for option in ("--datapath", "--configfile")))):
                    raise ValueError("unsupported Moonraker path argument form")
            raw_data = options.get("data", environment.get("MOONRAKER_DATA_PATH"))
            cwd = (process / "cwd").resolve()
            if not cwd.is_dir():
                continue
            data_path = _moonraker_expand_path(raw_data or "~/printer_data", environment, uid)
            data_path = (data_path if data_path.is_absolute() else cwd / data_path).resolve()
            raw_config = options.get("config", environment.get("MOONRAKER_CONFIG_PATH"))
            config_path = (_moonraker_expand_path(raw_config, environment, uid)
                           if raw_config is not None else data_path / "config/moonraker.conf")
            config_path = config_path if config_path.is_absolute() else cwd / config_path
            encoded = _moonraker_read_bytes(config_path, 262144)
            parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
            parser.read_string(encoded.decode("utf-8"))
            if not parser.has_section("server") or any(
                    re.match(r"include\s", section, re.I) for section in parser.sections()):
                continue
            raw_socket = parser.get("server", "klippy_uds_address", fallback="/tmp/klippy_uds")
            if any(character in raw_socket for character in ("{", "}", "$", "%", "\n")):
                continue
            actual_socket = _moonraker_expand_path(raw_socket, environment, uid)
            if (not (actual_socket.is_absolute() or raw_socket.startswith("/"))
                    or actual_socket.resolve() != expected):
                continue
            root = (data_path / "config").resolve()
            if root.is_dir():
                roots.append(str(root))
        except (OSError, UnicodeError, ValueError, configparser.Error):
            continue
    return next(iter(roots)) if len(roots) == 1 else None


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
        self.renderer = PNGRenderer(sys.executable, default_dependency_directory(), self.reactor)
        self.chart_start_args = dict(start_args)
        self.chart_config_root = None
        self.preflight_generation = 0
        self.chart_preflight_check = None
        self.kinematics_name = None
        if hasattr(config, "getsection"):
            self.kinematics_name = config.getsection("printer").get("kinematics")
        self.session = None
        self.last_result = None
        self.noise_recommendation = self.noise_seed_context = None
        self.noise_seeds = {}
        self.in_home = False
        self.home_axis = None
        self.timer = self.reactor.register_timer(self._timeout)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("klippy:disconnect", self._disconnect)
        self.printer.register_event_handler("klippy:connect", self._validate_model_connection)
        self.printer.register_event_handler("klippy:connect", self._find_chart_root)
        for name, callback, description in (
                ("ACCEL_HOMING_CALIBRATE", self.cmd_CALIBRATE,
                 "Calibrate one X or Y homing threshold with ten qualified homes"),
                ("ACCEL_HOMING_CALIBRATE_NOISE", self.cmd_NOISE,
                 "Measure three stationary one-second noise windows and suggest calibration starts"),
                ("ACCEL_HOMING_CANCEL", self.cmd_CANCEL,
                 "Cancel the current accelerometer homing workflow"),
                ("ACCEL_HOMING_TEST", self.cmd_TEST,
                 "Test X then Y from an explicitly confirmed center"),
                ("ACCEL_HOMING_CHARTS_GENERATE", self.cmd_CHARTS,
                 "Export acceleration PNG charts; optional AXIS=X or AXIS=Y, default both"),
                ("ACCEL_HOMING_ACCURACY", self.cmd_ACCURACY,
                 "Measure repeated X/Y commanded homing travel and accelerometer captures"),
                ("ACCEL_HOMING_STATUS", self.cmd_STATUS,
                 "Show calibration progress, model and chart output paths")):
            self.gcode.register_command(name, callback, desc=description)

    def _error(self, message):
        return self.printer.command_error("accel_homing: " + message)

    def _info(self, message):
        self.gcode.respond_info("accel_homing: " + message)

    def _find_chart_root(self):
        self.chart_config_root = verified_moonraker_config_root(self.chart_start_args)

    def _chart_check(self, session):
        if session.get("finish_interrupted"):
            raise self._error(session["finish_interrupted"])
        if session.get("finishing") and session.get("finish_result") != "complete":
            if self.session is not session:
                raise self._error("partial PNG export was interrupted")
            # A shutdown/print interruption is reportable after cleanup. This
            # branch exports only already-qualified data and cannot seed a
            # model, resume a trial or authorize movement.
            return
        if session["mode"] == "noise" and not session.get("sensor_stopped"):
            self._check_noise_running(session)
        else:
            self._check_running(session)
            if session["mode"] == "noise":
                self._check_stopped_noise_context(session)

    def _chart_options(self, session):
        return {"renderer": self.renderer, "check_running": lambda: self._chart_check(session)}

    def _chart_preflight(self):
        generation, operation = self.preflight_generation, self.owner.operation_generation
        def check():
            if (self.preflight_generation != generation or self.owner.operation_generation != operation):
                raise self._error("PNG renderer preflight was cancelled or disconnected")
            if hasattr(self.printer, "is_shutdown") and self.printer.is_shutdown():
                raise self._error("printer is shut down")
            if self._printing():
                raise self._error("cannot prepare PNG charts while printing or paused")
        try:
            preflight_renderer(renderer=self.renderer, check_running=check)
            check()
            self.chart_preflight_check = check
        except Exception as exc:
            raise self._error("PNG renderer is unavailable before motion/acquisition: %s. "
                              "Run the package installer for this Klipper Python environment" % exc)

    def _show_pngs(self, paths, description):
        root = Path(self.chart_config_root).resolve() if self.chart_config_root else None
        for label, filename in paths.items():
            path = Path(filename).resolve()
            if path.suffix.lower() != ".png":
                raise self._error("chart writer returned a non-PNG output")
            self._info(html.escape("%s %s: %s" % (description, label.upper(), path), quote=False))
            try:
                relative = path.relative_to(root) if root is not None else None
            except ValueError:
                relative = None
            if relative is None:
                self._info("PNG saved locally; the Moonraker config root is unverified for this path. "
                           "Open it through this printer's file browser when available")
                continue
            url = "/server/files/config/" + urllib.parse.quote(relative.as_posix(), safe="/")
            def attribute(value):
                # Fluidd linkifies known uppercase G-code tokens before its
                # HTML sanitizer, including tokens inside attributes. Numeric
                # references keep the owned URL/alt intact through that pass.
                return re.sub(r"[A-Z_]", lambda match: "&#%d;" % ord(match.group()),
                              html.escape(value, quote=True))
            escaped = attribute(url)
            alt = attribute("%s: %s" % (description, path.name))
            self.gcode.respond_info('<a href="%s">View PNG: %s<br><img src="%s" alt="%s" '
                                    'style="max-width:100%%;height:auto"></a>' % (
                                        escaped, alt, escaped, alt))

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
        """Resolve stock G28 directly or beneath one verified built-in wrapper."""
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
        override = self.printer.lookup_object("homing_override", None)
        if safe is not None and override is not None:
            raise self._error("workflows cannot combine safe_z_home and homing_override G28 wrappers")
        if override is not None:
            if (_HomingOverride is None or type(override) is not _HomingOverride
                    or getattr(override, "printer", None) is not self.printer
                    or getattr(override, "gcode", None) is not self.gcode
                    or not self._bound_handler(getattr(override, "cmd_G28", None),
                                               override, _OVERRIDE_G28)
                    or not all(self._bound_handler(handlers["G28"], override, _OVERRIDE_G28)
                               for handlers in registries)
                    or not self._bound_handler(getattr(override, "prev_G28", None),
                                               normal, _PRINTER_G28)):
                raise self._error("workflows require built-in homing_override with an unmodified "
                                  "stock G28 predecessor; custom or nested wrappers are unsupported")
            if getattr(override, "in_script", None) is not False:
                raise self._error("workflows cannot run from an active or unverifiable homing_override G28 script")
        elif safe is None:
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
        if session["mode"] == "noise":
            self.owner.stop_noise_measurement()
            session["sensor_stopped"] = True
            return
        try:
            self.owner._end_session()
        finally:
            self.owner.threshold_g.update(session["saved_thresholds"])
            self.owner.back_off_dist = session["saved_back_off"]
            self.in_home, self.home_axis = False, None

    def _finish(self, result, release=False):
        session = self.session
        if session is not None and session.get("finishing"):
            session["finish_interrupted"] = result.get("reason", "workflow interrupted during PNG export")
            return
        if session is not None:
            session["finishing"] = True
            session["finish_result"] = result.get("result")
        try:
            self._restore()
            if release:
                try:
                    self._disable_xy()
                except Exception:
                    logging.exception("accel_homing: unable to release/invalidate XY during cleanup")
        except Exception as exc:
            if session is not None and session["mode"] in ("accuracy", "noise"):
                result = {"result": "aborted", "reason": "workflow cleanup failed: %s" % exc}
            raise
        finally:
            try:
                if session is not None:
                    session["finish_result"] = result.get("result")
                if session is not None and session["mode"] == "accuracy":
                    result = self._accuracy_result(session, result)
                elif session is not None and session["mode"] == "noise":
                    result = self._noise_result(session, result)
            finally:
                self.last_result = result
                self.session = None
                self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _shutdown(self, *args):
        self.preflight_generation += 1
        self._clear_noise_seeds()
        if self.session is not None:
            self._finish({"result": "aborted", "reason": "printer shutdown"})

    def _disconnect(self, *args):
        self.preflight_generation += 1
        self._clear_noise_seeds()
        if self.session is not None:
            self._finish({"result": "aborted", "reason": "printer disconnected"})

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
        self._clear_noise_seeds()

    def _require_centered(self, gcmd):
        if str(gcmd.get("CENTERED", "")).strip() != "1":
            raise gcmd.error("Manually center XY first, then run this command with CENTERED=1")

    def _require_unhomed_xy(self):
        homed = self.owner.toolhead.get_kinematics().get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if any(axis in homed for axis in AXES):
            raise self._error("workflow requires both X and Y unhomed; release XY and "
                              "manually center the toolhead before running with CENTERED=1")

    def _begin(self, gcmd, mode, axis=None, axes=None):
        if self.session is not None:
            raise gcmd.error("A workflow is already active; use ACCEL_HOMING_CANCEL first")
        axes = axes if axes is not None else [axis] if axis else ["x", "y"]
        self._preflight(axes)
        self._require_unhomed_xy()
        if mode in ("charts", "accuracy") and self.chart_preflight_check is not None:
            self.chart_preflight_check()
        self.session = {"mode": mode, "axis": axis, "state": "starting",
                        "run_id": uuid.uuid4().hex, "successes": 0, "attempts": 0,
                        "candidate_qualified": False, "candidate_attempt": None,
                        "validation_records": [], "repeatability_spread_mm": None,
                        "records": [], "paths": [], "candidate": self.start_threshold,
                        "saved_thresholds": dict(self.owner.threshold_g),
                        "saved_back_off": self.owner.back_off_dist,
                        "save_config": (mode == "calibration" and bool(gcmd.get_int(
                            "SAVE_CONFIG", 1, minval=0, maxval=1)))}
        if mode == "calibration":
            seed = self._consume_noise_seed(axis)
            if seed is not None:
                self.session["candidate"] = seed
                self.session["noise_seed"] = {"run_id": self.noise_recommendation["run_id"],
                                               "start_threshold_g": seed}

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

    def _clear_noise_seeds(self):
        self.noise_recommendation = self.noise_seed_context = None
        self.noise_seeds = {}

    def _noise_context(self):
        return {"sensor": self.owner.noise_sensor_context(),
                "calibration_start_threshold": self.start_threshold,
                "calibration_step": self.threshold_step,
                "calibration_max_threshold": self.max_threshold,
                "axes": {axis: self._model_context(axis)
                         for axis in AXES if axis in self.owner.rails_by_axis}}

    def _consume_noise_seed(self, axis):
        if self.noise_seed_context is None:
            return None
        if self._noise_context() != self.noise_seed_context:
            self._clear_noise_seeds()
            self._info("Stationary noise context changed; using the configured calibration start")
            return None
        seed = self.noise_seeds.pop(axis, None)
        if seed is not None:
            self._info("%s calibration starts at %.6g g from stationary noise run %s. "
                       "This is an initial candidate; the quiet-baseline gate and ten-home "
                       "qualification are unchanged" % (
                           axis.upper(), seed, self.noise_recommendation["run_id"]))
        return seed

    def _noise_snapshot(self):
        manager = self.printer.lookup_object("stepper_enable", None)
        kin = self.owner.toolhead.get_kinematics()
        if manager is None or not callable(getattr(kin, "get_steppers", None)):
            raise self._error("cannot establish stationary motor enable state")
        motors = []
        for stepper in kin.get_steppers():
            name = stepper.get_name()
            line = manager.lookup_enable(name)
            if not callable(getattr(line, "is_motor_enabled", None)):
                raise self._error("cannot read motor enable state for %s" % name)
            motors.append((name, bool(line.is_motor_enabled())))
        return (tuple(self.owner.toolhead.get_position()),
                self._status("toolhead").get("homed_axes", ""),
                tuple(sorted(self.owner.threshold_g.items())),
                tuple(sorted(self.owner.impact_sign.items())),
                self.owner.homing_accel, self.owner.back_off_dist, tuple(sorted(motors)))

    def _preflight_noise(self):
        if self.session is not None:
            raise self._error("a workflow is already active; use ACCEL_HOMING_CANCEL first")
        if hasattr(self.printer, "is_shutdown") and self.printer.is_shutdown():
            raise self._error("printer is shut down")
        if self._printing() or self._status("idle_timeout").get("state") == "Printing":
            raise self._error("noise measurement requires an idle printer, not printing or paused")
        if self.owner.toolhead is None:
            raise self._error("printer is not connected")
        if self.owner.homing_active or self.owner.attempt is not None:
            raise self._error("another homing operation is active")
        self._noise_idle_queue()

    def _noise_idle_queue(self):
        toolhead = self.owner.toolhead
        if not callable(getattr(toolhead, "check_busy", None)):
            raise self._error("cannot establish that the stationary motion queue is empty")
        queued, estimated, empty = toolhead.check_busy(self.reactor.monotonic())
        if (not all(math.isfinite(value) for value in (queued, estimated))
                or not empty or queued > estimated + 0.000001):
            raise self._error("queued motion is active during stationary noise measurement")

    def _check_noise_running(self, session):
        self._check_running(session)
        if self.owner.operation_generation != session["noise_generation"]:
            raise self._error("stationary noise was interrupted")
        if (session.get("sensor_token") is None
                or self.owner.noise_session is not session["sensor_token"]):
            raise self._error("stationary sensor session was cancelled or disconnected")
        self._noise_idle_queue()
        if (self._noise_snapshot() != session["snapshot"]
                or self._noise_context() != session["context"]):
            raise self._error("printer or sensor context changed during stationary noise measurement")

    def _check_stopped_noise_context(self, session):
        self._noise_idle_queue()
        if (self.owner.operation_generation != session["noise_generation"]
                or self._noise_snapshot() != session["snapshot"]
                or self._noise_context() != session["context"]):
            raise self._error("stationary context was interrupted or changed during PNG export")

    def _noise_record(self, session, iteration, capture):
        samples = capture.get("samples", ())
        context = session["context"]["sensor"]
        cutoff = capture.get("cutoff_time_s")
        if (capture.get("dropped", 0) or capture.get("fault") or capture.get("error")
                or capture.pop("_noise_context", None) != context
                or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff)
                or len(samples) < 50):
            raise self._error("stationary noise capture is incomplete or lacks current sensor provenance")
        previous = None
        for sample in samples:
            if (not isinstance(sample, (tuple, list)) or len(sample) != 4
                    or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) for value in sample)
                    or sample[0] <= cutoff or (previous is not None and sample[0] <= previous)):
                raise self._error("invalid or stale stationary noise samples")
            previous = sample[0]
        if (samples[-1][0] - samples[0][0] + 0.000000001 < NOISE_DURATION
                or (session["iterations"] and
                    samples[0][0] <= session["iterations"][-1]["record"]["last_sample_time_s"])):
            raise self._error("stationary noise windows must be disjoint and span one second each")
        baseline = {axis: math.fsum(row[index + 1] for row in samples) / len(samples)
                    for index, axis in enumerate("xyz")}
        peaks, rms, spread = {}, {}, {}
        for index, axis in enumerate("xyz", 1):
            values = [row[index] for row in samples]
            deltas = [value - baseline[axis] for value in values]
            peaks[axis] = max(abs(value) for value in deltas)
            rms[axis] = math.sqrt(math.fsum(value * value for value in deltas) / len(deltas))
            spread[axis] = max(values) - min(values)
        capture["baseline_g"] = tuple(baseline[axis] for axis in "xyz")
        return {"run_id": session["run_id"], "noise_iteration": iteration,
                "duration_s": samples[-1][0] - samples[0][0], "sample_count": len(samples),
                "first_sample_time_s": samples[0][0], "last_sample_time_s": samples[-1][0],
                "baseline_g": baseline, "noise_peak_g": peaks,
                "noise_peak_to_peak_g": spread, "noise_rms_g": rms,
                "sensor": context["sensor"], "backend": context["backend"],
                "full_scale_g": context["full_scale_g"],
                "axis_context": "machine-mapped XYZ after configured axes_map",
                "sensor_context": context}

    def _noise_candidate(self, peak):
        required = max(self.start_threshold, peak * 1.2, peak + 0.1)
        # Keep the existing start+n*step candidate grid; never round a measured
        # peak downward or clamp an unsafe candidate back under the ceiling.
        steps = math.ceil((required - self.start_threshold) / self.threshold_step)
        return self.start_threshold + steps * self.threshold_step

    def _noise_recommend(self, session):
        peaks = {axis: max(item["record"]["noise_peak_g"][axis]
                           for item in session["iterations"]) for axis in "xyz"}
        full_scale = session["context"]["sensor"]["full_scale_g"]
        limit = min(self.max_threshold, 15.0, 0.8 * full_scale) if full_scale else None
        starts = {axis: self._noise_candidate(peaks[axis]) for axis in AXES}
        unavailable = [axis for axis, value in starts.items()
                       if limit is None or value > limit or value <= peaks[axis]]
        for axis in unavailable:
            starts[axis] = None
        return {"source": "stationary_noise", "method": "initial_candidate_heuristic",
                "noise_peak_g": peaks, "start_threshold_g": starts,
                "margin_factor": 1.2, "margin_g": 0.1,
                "candidate_step_g": self.threshold_step,
                "configured_start_g": self.start_threshold,
                "threshold_limit_g": limit, "unavailable_axes": unavailable, "clamped": False}

    def _noise_result(self, session, ending):
        complete = ending.get("result") == "complete" and len(session["iterations"]) == NOISE_ITERATIONS
        recommendation = session.get("recommendation") if complete else None
        result = {"mode": "noise", "run_id": session["run_id"],
                  "iterations_requested": NOISE_ITERATIONS,
                  "iterations": list(session["iterations"]),
                  "recommendation": recommendation, **ending}
        try:
            result["summary_paths"] = write_noise(
                self.charts_directory, session["run_id"], session["iterations"],
                recommendation, result["result"], result.get("reason"), **self._chart_options(session))
            self._show_pngs(result["summary_paths"], "Stationary noise")
        except Exception as exc:
            logging.exception("accel_homing: unable to export stationary noise summary")
            result.update(chart_error=str(exc), recommendation=None)
            if result["result"] == "complete":
                result.update(result="aborted", reason="noise summary export failed: %s" % exc)
        return result

    def cmd_NOISE(self, gcmd):
        self._clear_noise_seeds()
        self._preflight_noise()
        self._chart_preflight()
        self._preflight_noise()
        if gcmd.get("SAVE_CONFIG", None) is not None:
            raise gcmd.error("SAVE_CONFIG is not supported by ACCEL_HOMING_CALIBRATE_NOISE")
        context = self._noise_context()
        session = {"mode": "noise", "state": "running", "run_id": uuid.uuid4().hex,
                   "iterations": [], "context": context, "snapshot": self._noise_snapshot(),
                   "noise_generation": self.owner.operation_generation,
                   "iteration": 0, "recommendation": None}
        self.session = session
        try:
            if self.owner.start_noise_measurement() != context["sensor"]:
                raise self._error("sensor context changed before stationary noise measurement")
            session["sensor_token"] = self.owner.noise_session
            self._info("Recording three stationary one-second windows with the current motor/fan state. "
                       "Keep the toolhead still; no homing or motor-state changes are requested")
            for iteration in range(1, NOISE_ITERATIONS + 1):
                session["iteration"] = iteration
                capture = self.owner.collect_noise_window(lambda: self._check_noise_running(session),
                                                          NOISE_DURATION)
                self._check_noise_running(session)
                record = self._noise_record(session, iteration, capture)
                try:
                    paths = write_noise_capture(self.charts_directory, session["run_id"],
                                                iteration, capture, record, **self._chart_options(session))
                finally:
                    del capture
                session["iterations"].append({"record": record, "capture_paths": paths})
                self._check_noise_running(session)
                self._info("Stationary noise window %d/3 complete: X %.6g g, Y %.6g g, Z %.6g g "
                           "absolute peaks after subtracting this window's mean" % (
                               iteration, *(record["noise_peak_g"][axis] for axis in "xyz")))
            session["recommendation"] = self._noise_recommend(session)
            self._check_noise_running(session)
            self._finish({"result": "complete"})
            if self.last_result["result"] != "complete":
                raise self._error(self.last_result.get("reason", "noise export failed"))
            self._check_stopped_noise_context(session)
            self.noise_recommendation = {"run_id": session["run_id"], **session["recommendation"]}
            self.noise_seed_context = context
            self.noise_seeds = {axis: value for axis, value in
                                session["recommendation"]["start_threshold_g"].items()
                                if value is not None}
            self._info("Noise measurement complete. Next calibration candidates: %s. Ordinary homing "
                       "thresholds are unchanged. These seeds do not filter noise or model startup "
                       "disturbances; the existing quiet-baseline gate can still reject calibration" %
                       ", ".join("%s=%s" % (axis.upper(), "unavailable above the safe ceiling" if value is None
                                             else "%.6g g" % value)
                                 for axis, value in session["recommendation"]["start_threshold_g"].items()))
        except Exception as exc:
            self._clear_noise_seeds()
            if self.session is session:
                self._finish({"result": "aborted", "reason": str(exc)})
            raise

    def _home(self, axis, threshold=None):
        if axis not in AXES:
            raise self._error("workflow homing requires exactly one X or Y axis")
        session = self.session
        if session["attempts"] >= session.get("attempt_limit", self.max_attempts):
            raise self._error("maximum homing attempts reached")
        self._check_running(session)
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
            try:
                self.owner.start_capture()
                capture_started = True
                letter = axis.upper()
                gcmd = self.gcode.create_gcode_command("G28", "G28 " + letter, {letter: ""})
                # Public wrappers may hop Z, force coordinates, run SYNC_MOTORS
                # or execute arbitrary homing G-code. Invoke only verified stock
                # X/Y here, retaining public handlers and normal homing events.
                # Recheck after capture setup and command creation can yield.
                self._check_running(session)
                self._stock_g28()(gcmd)
                self.owner.toolhead.wait_moves()
            finally:
                self.in_home, self.home_axis = False, None
                try:
                    self.owner._end_session()
                finally:
                    capture = self.owner.stop_capture() if capture_started else {"samples": [], "dropped": 0}
        except Exception:
            if session["mode"] == "accuracy" and self.session is session:
                self._remember_accuracy_record(session, axis, old_record)
            raise
        self._check_running(session)
        record = self.owner.last.get(axis)
        if record is None or record is old_record:
            raise self._error("G28 returned without a new accelerometer homing result")
        if not isinstance(record, dict) or record.get("axis") != axis:
            raise self._error("G28 returned an unexpected homing result axis")
        source_record = record
        detector_record = capture.pop("_record", None)
        record = dict(record)
        record["run_id"] = session["run_id"]
        record["workflow_attempt"] = session["attempts"]
        record["workflow_threshold_g"] = (threshold if threshold is not None
                                          else self.owner.threshold_g[axis])
        record["workflow_phase"] = ("validation" if session["candidate_qualified"]
                                    else "discovery") if session["mode"] == "calibration" \
            else session["mode"]
        if session["mode"] == "accuracy":
            record = self._remember_accuracy_record(session, axis, old_record, record)
        if len(capture.get("samples", ())) < 2 or capture.get("dropped", 0):
            raise self._error("homing capture is empty or incomplete; sensor data is not usable")
        previous = None
        for sample in capture["samples"]:
            if len(sample) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                           for v in sample) \
                    or (previous is not None and sample[0] <= previous):
                raise self._error("homing capture has invalid or stale sensor samples")
            previous = sample[0]
        if session["mode"] in ("accuracy", "charts"):
            self._validate_accuracy_capture(session, axis, source_record, detector_record,
                                            record, capture)
            record["capture_valid"] = True
        if session["mode"] != "accuracy":
            session["records"].append(record)
        self._info("%s home: result %s; onset %s g; peak %s g; threshold %s g; "
                   "commanded travel %s mm" % (
            axis.upper(), record.get("result"), record.get("onset_g"),
            record.get("impact_peak_g"), record.get("threshold_g"), record.get("travel_mm")))
        session["terminal_home"] = (axis, record, source_record)
        return record, capture

    def _release_terminal_home(self, axis, record):
        session = self.session
        self._check_running(session)
        current = session.get("terminal_home")
        if (current is None or current[0] != axis or current[1] is not record
                or self._quality(axis, record)[0] != "good"
                or self.owner.last.get(axis) is not current[2]):
            raise self._error("terminal home is not the current qualified result")
        generation = self.owner.operation_generation
        self.owner.release_workflow_home(axis, current[2], session["saved_back_off"], session)
        self.owner.toolhead.wait_moves()
        self._check_running(session)
        if (self.owner.last.get(axis) is not current[2] or self.owner._homed(axis) is not True
                or self.owner.operation_generation != generation):
            raise self._error("terminal home changed or motors were released during final release")

    def _check_running(self, session):
        if session is None or self.session is not session:
            raise self._error("workflow cancelled or interrupted")
        if hasattr(self.printer, "is_shutdown") and self.printer.is_shutdown():
            raise self._error("printer is shut down")
        if self._printing():
            raise self._error("printing or pause detected during workflow")

    def _remember_accuracy_record(self, session, axis, old_record, record=None):
        if record is None:
            source = self.owner.last.get(axis)
            if not isinstance(source, dict) or source is old_record:
                return None
            record = dict(source)
        record.update(run_id=session["run_id"], workflow_attempt=session["attempts"],
                      workflow_threshold_g=session["saved_thresholds"][axis],
                      workflow_phase=("discovery" if session["sample_index"] == 0
                                      else "measurement"),
                      accuracy_sample=session["sample_index"], capture_valid=False)
        invalid = [key for key, value in record.items()
                   if isinstance(value, float) and not math.isfinite(value)]
        if invalid:
            # An invalid observed value must never prevent an honest partial
            # failure report from retaining earlier measurements as strict JSON.
            record.update((key, None) for key in invalid)
            record["invalid_numeric_fields"] = invalid
        session["records"].append(record)
        session["pending_record"] = record
        return record

    def _validate_accuracy_capture(self, session, axis, source_record, detector_record,
                                   record, capture):
        detector = capture.get("detector")
        baseline = capture.get("baseline_g")
        arm, onset = record.get("arm_time_s"), record.get("onset_time_s")
        finite = lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value))
        if (detector_record is not source_record or not isinstance(detector, dict)
                or capture.get("fault") or capture.get("error")
                or detector.get("axis") != axis or detector.get("attempt_count") != 1
                or not isinstance(baseline, (tuple, list)) or len(baseline) != 3
                or not all(finite(value) for value in baseline)
                or not finite(arm) or detector.get("arm_time_s") != arm
                or not finite(detector.get("threshold_g")) or detector["threshold_g"] <= 0
                or detector.get("direction") not in (-1, 0, 1)
                or not finite(detector.get("homing_speed_mm_s"))
                or detector["homing_speed_mm_s"] <= 0
                or not finite(detector.get("homing_accel_mm_s2"))
                or detector["homing_accel_mm_s2"] <= 0
                or not math.isclose(detector["threshold_g"], session["saved_thresholds"][axis],
                                    rel_tol=1e-12, abs_tol=1e-12)
                or (session.get("last_arm_time") is not None
                    and arm <= session["last_arm_time"])):
            raise self._error("homing capture does not belong to the current detector/reference")
        samples = capture["samples"]
        if not samples[0][0] < arm <= samples[-1][0]:
            raise self._error("homing capture lacks the current pre-arm reference or armed samples")
        if record.get("result") == "impact":
            captured_onset = detector.get("onset_time_s")
            def stamp(value):
                if (not isinstance(value, (tuple, list)) or len(value) != 4
                        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
                        or not 0 <= value[0] <= 0xFFFFFFFF or not 0 < value[1] <= 0xFFFFFFFF
                        or value[3] < 2 or not 0 <= value[2] < value[3]):
                    return None
                return tuple(value)
            def missing_impact(reason):
                number = lambda value: value if finite(value) else None
                diagnostics = {"reason": reason, "axis": axis, "sample_count": len(samples),
                               "first_sample_time_s": samples[0][0], "last_sample_time_s": samples[-1][0],
                               "arm_time_s": arm, "recorded_onset_time_s": number(onset),
                               "capture_onset_time_s": number(captured_onset),
                               "onset_sample": stamp(source_record.get("onset_sample")),
                               "capture_onset_sample": stamp(detector.get("onset_sample")),
                               "onset_sample_matches": number(detector.get("onset_sample_matches")),
                               "onset_sample_index": number(detector.get("onset_sample_index")),
                               "onset_g": number(detector.get("onset_g")),
                               "impact_peak_g": number(detector.get("impact_peak_g"))}
                record["capture_diagnostics"] = diagnostics
                detail = lambda value: "unknown" if value is None else "%.17g" % value
                raise self._error("homing capture lacks the current measured impact "
                                  "(%s; recorded onset=%s, capture onset=%s, last sample=%s; "
                                  "matches=%s, index=%s, samples=%d)" % (
                                      reason, detail(diagnostics["recorded_onset_time_s"]),
                                      detail(diagnostics["capture_onset_time_s"]),
                                      detail(diagnostics["last_sample_time_s"]),
                                      detail(diagnostics["onset_sample_matches"]),
                                      detail(diagnostics["onset_sample_index"]), len(samples)))
            if (not finite(onset) or not finite(detector.get("onset_g"))
                    or not finite(detector.get("impact_peak_g")) or detector["impact_peak_g"] < 0):
                missing_impact("invalid measured impact values")
            source_stamp = stamp(source_record.get("onset_sample"))
            capture_stamp = stamp(detector.get("onset_sample"))
            if (type(detector.get("onset_sample_version")) is not int
                    or detector["onset_sample_version"] != 1
                    or source_stamp is None or capture_stamp != source_stamp):
                missing_impact("missing or changed raw onset sample identity")
            index = detector.get("onset_sample_index")
            if (type(detector.get("onset_sample_matches")) is not int
                    or detector["onset_sample_matches"] != 1
                    or type(index) is not int or not 0 <= index < len(samples)):
                missing_impact("onset packet/sample was not retained exactly once")
            if (not finite(captured_onset) or detector.get("trigger_onset_time_s") != onset
                    or samples[index][0] != captured_onset):
                missing_impact("retained onset does not match the current detector/sample")
            if not arm <= onset or not arm <= captured_onset <= samples[-1][0]:
                missing_impact("onset lies outside the armed capture")
            # A later MCU clock adjustment may change chart timestamps.
            # Keep the original homing timestamp and align only this copied
            # report to its proven retained row; the detector is untouched.
            record["trigger_onset_time_s"] = onset
            record["onset_time_s"] = captured_onset
            record["capture_onset_sample_index"] = index
            record["capture_clock_shift_s"] = captured_onset - onset
        record["threshold_g"] = detector["threshold_g"]
        record["onset_g"] = detector.get("onset_g")
        record["impact_peak_g"] = detector.get("impact_peak_g")
        record["direction"] = detector.get("direction")
        record["configured_homing_speed_mm_s"] = self.owner.rails_by_axis[axis].get_homing_info().speed
        record["homing_speed_mm_s"] = detector.get("homing_speed_mm_s")
        record["homing_accel_mm_s2"] = detector.get("homing_accel_mm_s2")
        session["last_arm_time"] = arm

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
        session = self.session
        self._check_running(session)
        if axis not in self.owner.toolhead.get_kinematics().get_status(
                self.reactor.monotonic()).get("homed_axes", ""):
            raise self._error("axis lost homing before return to center")
        center, _expected = self._geometry(axis)
        coordinates = [None, None, None]
        coordinates[AXES[axis]] = center
        info = self.owner.rails_by_axis[axis].get_homing_info()
        self.owner.toolhead.manual_move(coordinates, min(self.reposition_speed, info.speed))
        self.owner.toolhead.wait_moves()
        self._check_running(session)

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
        self._release_terminal_home(axis, qualified[-1])
        # Final release may yield while its motion completes. Recheck all ten
        # accepted trials before staging any model settings.
        self._qualified_validation_records()
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
        if session.get("noise_seed") is not None:
            metadata["stationary_noise_seed"] = dict(session["noise_seed"])
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
        self.preflight_generation += 1
        self._clear_noise_seeds()
        if self.session is None:
            gcmd.respond_info("No accelerometer homing workflow is active")
            return
        self._finish({"result": "cancelled", "reason": "operator cancelled"})
        gcmd.respond_info("Accelerometer homing workflow cancelled; settings restored")

    def _start_xy(self, gcmd, mode, axes=("x", "y")):
        self._require_centered(gcmd)
        if mode == "charts":
            self._chart_preflight()
        self._begin(gcmd, mode, axes=axes)
        try:
            self.session.update(state="running", axes=axes)
            self._test_xy()
        except Exception as exc:
            if self.session is not None:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    def cmd_TEST(self, gcmd):
        self._start_xy(gcmd, "test")

    def cmd_CHARTS(self, gcmd):
        requested = gcmd.get("AXIS", None)
        axes = ("x", "y")
        if requested is not None:
            axis = str(requested).strip().lower()
            if axis not in AXES:
                raise gcmd.error("AXIS must be X or Y; omit AXIS to chart both axes")
            axes = (axis,)
        self._start_xy(gcmd, "charts", axes=axes)

    def cmd_ACCURACY(self, gcmd):
        requested = str(gcmd.get("AXIS", "XY")).strip().lower()
        if requested not in ("x", "y", "xy"):
            raise gcmd.error("AXIS must be X, Y or XY")
        samples = gcmd.get_int("SAMPLES", 10, minval=2, maxval=50)
        # Accuracy never stages/saves a model or restarts Klipper, including if
        # a SAVE_CONFIG token was copied from a calibration command.
        if gcmd.get("SAVE_CONFIG", None) is not None:
            raise gcmd.error("SAVE_CONFIG is not supported by ACCEL_HOMING_ACCURACY")
        self._require_centered(gcmd)
        self._chart_preflight()
        axes = list(requested)
        self._begin(gcmd, "accuracy", axes=axes)
        session = self.session
        session.update(state="running", axes=axes, samples_requested=samples,
                       attempt_limit=len(axes) * (samples + 1), sample_index=0,
                       configured_speeds={axis: self.owner.rails_by_axis[axis].get_homing_info().speed
                                          for axis in axes},
                       pending_record=None, last_arm_time=None,
                       axis_results={axis: {"requested_samples": samples, "complete": False,
                                            "discovery": None, "discovery_capture_paths": None,
                                            "trials": [], "statistics": None,
                                            "capture_paths": [], "rejected": None}
                                     for axis in axes})
        self._info("Accuracy uses the configured homing speed and threshold. CENTERED=1 "
                   "confirms manually centered XY with nozzle clearance; each axis's first "
                   "home is discovery only and is excluded from measured statistics")
        try:
            self._accuracy_trials(session)
            self._check_running(session)
            # Validate all exported captures and prospective summary inputs
            # before terminal release. Publish afterward so a release failure
            # receives an aborted summary retaining accepted measurements.
            validate_accuracy(self.charts_directory, session["run_id"], session["axis_results"],
                              **self._chart_options(session))
            axis, record, _source = session["terminal_home"]
            self._release_terminal_home(axis, record)
            self._finish({"result": "complete"})
            if self.last_result["result"] != "complete":
                raise self._error(self.last_result.get("reason", "accuracy export failed"))
        except Exception as exc:
            if self.session is session:
                self._finish({"result": "aborted", "reason": str(exc)}, release=True)
            raise

    @staticmethod
    def _accuracy_statistics(records):
        if not records:
            return None
        travels = [record["travel_mm"] for record in records]
        count = len(travels)
        mean = math.fsum(travels) / count
        variance = (math.fsum((value - mean) ** 2 for value in travels) / count
                    if count >= 2 else None)
        return {"count": count, "min_mm": min(travels), "max_mm": max(travels),
                "mean_mm": mean, "spread_mm": AccelHomingWorkflows._repeatability_spread(records),
                "stddev_mm": math.sqrt(variance) if variance is not None else None,
                "variance_mm2": variance,
                "variance_definition": ("population (divide by N)" if count >= 2 else
                                        "population (divide by N); at least 2 measured homes required")}

    def _accuracy_trials(self, session):
        for axis_index, axis in enumerate(session["axes"]):
            session["axis"] = axis
            axis_result = session["axis_results"][axis]
            for sample_index in range(session["samples_requested"] + 1):
                self._check_running(session)
                session["sample_index"] = sample_index
                session["pending_record"] = None
                record, capture = self._home(axis)
                quality, reason = self._quality(axis, record)
                if quality != "good":
                    axis_result["rejected"] = record
                elif sample_index == 0:
                    axis_result["discovery"] = record
                else:
                    axis_result["trials"].append(record)
                    axis_result["capture_paths"].append(None)
                    axis_result["statistics"] = self._accuracy_statistics(axis_result["trials"])
                    axis_result["complete"] = len(axis_result["trials"]) == session["samples_requested"]
                try:
                    paths = write_capture(self.charts_directory,
                                          "%s_a%03d" % (session["run_id"], session["attempts"]),
                                          axis, capture, record, **self._chart_options(session))
                finally:
                    # Bound memory to one capture, even on export rejection.
                    del capture
                self._check_running(session)
                session["paths"].append({"axis": axis, "phase": record["workflow_phase"],
                                         "sample_index": sample_index,
                                         "workflow_attempt": record["workflow_attempt"], **paths})
                if quality != "good":
                    axis_result["rejection_capture_paths"] = paths
                    raise self._error(reason + "; no automatic move follows this result; manually recenter")
                if sample_index == 0:
                    axis_result["discovery_capture_paths"] = paths
                    self._info("%s discovery home qualified; excluded from accuracy statistics" % axis.upper())
                else:
                    axis_result["capture_paths"][-1] = paths
                    self._info("%s accuracy measurement %d/%d retained" % (
                        axis.upper(), sample_index, session["samples_requested"]))
                # Only a qualified current home authorizes a return to center.
                # X must also return after its final trial before Y discovery.
                if sample_index < session["samples_requested"] or axis_index < len(session["axes"]) - 1:
                    self._recenter(axis)

    def _accuracy_result(self, session, ending):
        pending = session.get("pending_record")
        if pending is not None:
            axis_result = session["axis_results"][session["axis"]]
            if pending is not axis_result["discovery"] and not any(
                    pending is record for record in axis_result["trials"]):
                axis_result["rejected"] = pending
        for axis, axis_result in session["axis_results"].items():
            stats = axis_result["statistics"] = self._accuracy_statistics(axis_result["trials"])
            count = len(axis_result["trials"])
            axis_result["complete"] = count == session["samples_requested"]
            detail = ("count %d/%d; no qualified measured homes" % (count, session["samples_requested"])
                      if stats is None else
                      "count %d/%d; min %.3f mm; max %.3f mm; mean %.6f mm; spread %.3f mm; "
                      "stddev %s mm; variance %s mm^2 (%s)" % (
                          count, session["samples_requested"], stats["min_mm"], stats["max_mm"],
                          stats["mean_mm"], stats["spread_mm"],
                          "unavailable" if stats["stddev_mm"] is None else "%.6f" % stats["stddev_mm"],
                          "unavailable" if stats["variance_mm2"] is None else "%.9f" % stats["variance_mm2"],
                          stats["variance_definition"]))
            self._info("%s accuracy %s: %s" % (axis.upper(),
                                              "complete" if axis_result["complete"] else "incomplete", detail))
            if stats is not None and stats["spread_mm"] >= REPEATABILITY_LIMIT_MM:
                self._info("%s observed spread is at or above the existing %.2f mm calibration "
                           "criterion; all qualified accuracy trials remain in this report" % (
                               axis.upper(), REPEATABILITY_LIMIT_MM))
        result = {"mode": "accuracy", "run_id": session["run_id"],
                  "axes": list(session["axes"]), "samples_requested": session["samples_requested"],
                  "fixed_thresholds_g": {axis: session["saved_thresholds"][axis]
                                         for axis in session["axes"]},
                  "configured_homing_speeds_mm_s": dict(session["configured_speeds"]),
                  "attempts": session["attempts"], "axis_results": session["axis_results"],
                  "records": list(session["records"]), "paths": list(session["paths"]), **ending}
        try:
            paths = write_accuracy(self.charts_directory, session["run_id"],
                                   session["axis_results"], result["result"], result.get("reason"),
                                   **self._chart_options(session))
            result["summary_paths"] = paths
            self._show_pngs(paths, "Accuracy")
        except Exception as exc:
            logging.exception("accel_homing: unable to export accuracy summary")
            result["chart_error"] = str(exc)
            if result["result"] == "complete":
                result["result"] = "aborted"
                result["reason"] = "accuracy summary export failed: %s" % exc
        self._info("Accuracy run %s: %s. Commanded travel is reported to 0.001 mm; "
                   "these measurements do not certify physical stop position" % (
                       session["run_id"], result["result"]))
        return result

    def _test_xy(self):
        session = self.session
        axes = session["axes"]
        for index, axis in enumerate(axes):
            record, capture = self._home(axis)
            quality, reason = self._quality(axis, record)
            if session["mode"] == "charts":
                paths = write_capture(self.charts_directory, session["run_id"], axis, capture, record,
                                      **self._chart_options(session))
                session["paths"].append({"axis": axis, **paths})
                self._show_pngs(paths, axis.upper() + " measured acceleration")
            if quality != "good":
                paths = list(session["paths"])
                self._finish({"result": "rejected", "reason": reason, "paths": paths}, release=True)
                raise self._error(reason + "; no automatic move follows this result; manually recenter")
            if index + 1 < len(axes):
                # Return only the trusted axis to center when another axis
                # follows; the next axis retains its manually confirmed center.
                self._recenter(axis)
        self._release_terminal_home(axis, record)
        self._finish({"result": "complete", "mode": session["mode"],
                      "run_id": session["run_id"], "records": list(session["records"]),
                      "paths": list(session["paths"])})
        self._info("%s %s complete; these measurements do not certify physical stop position"
                   % (" and ".join(axis.upper() for axis in axes), session["mode"]))

    def get_status(self, eventtime):
        session = self.session
        active = None if session is None else {
            key: session.get(key) for key in ("mode", "state", "axis", "candidate", "successes",
                                             "candidate_qualified", "attempts", "run_id", "expires",
                                             "repeatability_spread_mm")}
        if session is not None and session["mode"] == "accuracy":
            active = {key: session.get(key) for key in ("mode", "state", "axis", "attempts", "run_id")}
            active.update(axes=list(session["axes"]),
                          samples_requested=session["samples_requested"],
                          sample_index=session["sample_index"],
                          fixed_thresholds_g={axis: session["saved_thresholds"][axis]
                                              for axis in session["axes"]},
                          configured_homing_speeds_mm_s=dict(session["configured_speeds"]),
                          completed_samples={axis: len(item["trials"])
                                             for axis, item in session["axis_results"].items()},
                          axis_results=session["axis_results"])
        elif session is not None and session["mode"] == "noise":
            active = {"mode": "noise", "state": session["state"], "run_id": session["run_id"],
                      "iteration": session["iteration"], "iterations_requested": NOISE_ITERATIONS,
                      "iterations_completed": len(session["iterations"]),
                      "duration_per_iteration_s": NOISE_DURATION}
        elif active is not None:
            active["repeatability_limit_mm"] = REPEATABILITY_LIMIT_MM
        return {"model": self.model_name, "manual_threshold_overrides": sorted(self.manual_axes),
                "active": active, "last_result": self.last_result,
                "noise_recommendation": self.noise_recommendation,
                "noise_seed_axes": sorted(self.noise_seeds),
                "charts_directory": self.charts_directory}

    def cmd_STATUS(self, gcmd):
        self.owner.cmd_STATUS(gcmd)
        gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), sort_keys=True))
