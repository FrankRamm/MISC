# Beacon accelerometer X/Y homing endstop for Klipper and Kalico
#
# Detects the bump of the toolhead against the X/Y stop with the Beacon's
# accelerometer and fires the stepper MCU trsync from the host. One touch per
# home. The homing axis is watched in either direction until the toolhead
# travels, then only against the travel (or in a configured direction). A
# legacy Beacon path recognizes pressed starts from commanded versus measured
# motion. Configured batch sensors fail homing if startup motion is unverified.
#
# Experimental. The inherited upstream Beacon implementation reports use on
# three printers; these generic adapters have only been tested synthetically.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import collections
import logging
import math
import struct
import sys
import threading

import mcu

try:
    from .accel_homing_backends import ConfiguredAccelBackend, motion_proof_window, select_sensor
except ImportError:  # Standalone offline replay harness
    from accel_homing_backends import ConfiguredAccelBackend, motion_proof_window, select_sensor

SAMPLE = struct.Struct("<hhh")
CLIPPED = 0x7FFF
NATIVE_LE = sys.byteorder == "little"
BEACON_ACCEL_DATA = "beacon_accel_data start_clock=%u delta_clock=%u data=%*s"
TRIGGER_CMD = "trsync_trigger oid=%c reason=%c"
MM_PER_S2_PER_G = 9806.65
WATCHDOG_INTERVAL = 0.010
STREAM_START_TIMEOUT = 0.5
BASELINE_MIN_SAMPLES = 50
IMPACT_WINDOW = 0.015      # s after the onset in which the impact peak is kept
IDLE_CLEANUP = 3.0         # s, closes a homing session left open by an error
SECOND_TOUCH_TOLERANCE = 1.5   # mm, only used if a second touch is configured
MIN_SPEED = 50.0           # mm/s, below this impacts were too soft in testing
AUTO_DIRECTION_SPEED = 30.0    # mm/s of travel that fixes which way an impact points
PRESSED_MIN_SPEED = 40.0   # mm/s commanded before missing motion counts as pressed
PRESSED_RATIO = 0.25       # measured below this share of commanded: not moving
MOVING_RATIO = 0.5         # measured above this share: moving freely
MOTION_LAG = 0.015         # s, commanded speed is taken this much earlier, so step
                           # and sensor timing may be off by 11 ms without harm
COMMAND_WINDOW = 0.004     # s, window for the commanded speed
EARLY_INTERVAL = 0.003     # s, watchdog interval until the toolhead is seen moving
GAP_SAMPLES = 5            # missing samples that make the integrated speed untrustworthy
ENABLE_SETTLE = 0.25       # s, motors switched on before homing settle before the reading
BASELINE_NOISE_RATIO = 0.20  # all-axis peak-to-peak / current impact threshold
AXES = {"x": 0, "y": 1}
FAILURES = {
    "no_data": "no fresh accelerometer data",
    "no_baseline": "no fresh quiet accelerometer baseline before the move",
    "travel": "no impact within the expected travel",
    "frozen": "accelerometer readings did not change while the steps ran",
    "scale": "another Beacon accelerometer client changed the range during homing",
    "stale_data": "accelerometer sample timestamps are stale or in the future",
    "invalid_data": "invalid or repeated accelerometer samples",
    "data_error": "accelerometer reported errors or overflows",
    "sample_gap": "accelerometer samples are missing or irregular",
    "sample_rate": "accelerometer sample rate is too low",
    "no_motion": "toolhead motion was not verified before the startup deadline",
}


class QuietReference:
    """Constant-size stationary-window statistics, protected by the owner lock."""

    def __init__(self, axis, cutoff, duration, limit):
        self.axis, self.cutoff, self.duration, self.limit = axis, cutoff, duration, limit
        self.first = self.last = None
        self.count = 0
        self.totals = [0.0, 0.0, 0.0]
        self.low = self.high = None

    def add(self, when, values):
        low = list(values) if self.low is None else [min(a, b) for a, b in zip(self.low, values)]
        high = list(values) if self.high is None else [max(a, b) for a, b in zip(self.high, values)]
        if any(b - a > self.limit for a, b in zip(low, high)):
            # A transient or a change between otherwise flat batches starts a
            # new candidate; the entire new quiet interval must follow it.
            self.first, self.count = None, 0
            self.totals = [0.0, 0.0, 0.0]
            low = high = list(values)
        if self.first is None:
            self.first = when
        self.last = when
        self.count += 1
        self.totals = [a + b for a, b in zip(self.totals, values)]
        self.low, self.high = low, high

    def baseline(self):
        if (self.count < BASELINE_MIN_SAMPLES or self.first is None
                or self.last - self.first + 0.000000001 < self.duration):
            return None
        return tuple(value / self.count for value in self.totals)


def clock_after(clock, reference):
    """32-bit clock comparison that survives wraparound."""
    return ((clock - reference) & 0xFFFFFFFF) < 0x80000000


def axis_values(data, index):
    """Raw samples of one sensor axis of a Beacon batch, without a Python loop."""
    if isinstance(data, tuple):
        return data[index::3]
    if NATIVE_LE:
        return memoryview(data).cast("h")[index::3]
    return [sample[index] for sample in SAMPLE.iter_unpack(data)]


def sample_count(data):
    return len(data) // (3 if isinstance(data, tuple) else 6)


def sample_slice(data, count):
    return data[:count * (3 if isinstance(data, tuple) else 6)]


def decode_clips(raw, previous, clips):
    """Beacon sends a clip in either direction as 0x7FFF. As in beacon_klipper,
    a clip takes the sign of the sample before it on the same axis."""
    out = list(raw)
    last = list(previous)
    for i, value in enumerate(out):
        axis = i % 3
        if value == CLIPPED:
            value = out[i] = clips[0] if last[axis] >= 0 else clips[1]
        last[axis] = value
    return out


def describe(r):
    """One line per homing pass, for the status command and klippy.log."""
    return ("%s pass %d: %s, onset %s g, peak %s g, noise +%.1f/-%.1f g, travel %s mm%s"
            % (r["axis"].upper(), r["pass"], r["result"], r["onset_g"], r["impact_peak_g"],
               r["noise_pos_g"], r["noise_neg_g"], r.get("travel_mm"),
               ", origin kept" if r.get("kept_origin") else ""))


def homing_warnings(name, info):
    """Rail settings known to weaken detection. Advice only, never an error."""
    warnings = []
    retract = info.retract_dist
    rehome = getattr(info, "min_home_dist", 0.0) or 0.0
    speeds = [info.speed] + ([info.second_homing_speed] if retract or rehome else [])
    if min(speeds) < MIN_SPEED:
        warnings.append(
            "beacon_accel_homing: %s homes at %.0f mm/s. Impacts below about %.0f mm/s"
            " can be too soft to detect, and the travel stop then ends homing with an error."
            % (name, min(speeds), MIN_SPEED))
    if (retract or rehome) and info.second_homing_speed < 0.6 * info.speed:
        warnings.append(
            "beacon_accel_homing: %s second_homing_speed is much lower than homing_speed."
            " A slow second touch can be too soft to detect. Use the same speed for both."
            % (name,))
    return warnings


class Attempt:
    """State of one armed homing move. The trigger path runs in the Beacon serial thread."""

    def __init__(self, endstop, pass_no, arm_time, arm_clock, deadline, deadline_clock,
                 index, map_sign, direction, limit_raw, count_g):
        self.endstop = endstop
        self.axis = endstop.axis
        self.pass_no = pass_no
        self.arm_time = arm_time
        self.arm_clock = arm_clock
        self.deadline = deadline
        self.deadline_clock = deadline_clock
        self.index = index
        self.map_sign = map_sign
        self.direction = direction
        self.limit_raw = limit_raw
        self.count_g = count_g
        self.baseline = None
        self.done = False
        self.reason = None  # "impact", "travel", "no_data", "no_baseline"
        self.last_data = 0.0
        self.last_samples = [0, 0, 0]  # previous sample per sensor axis, clips decoded
        self.noise_pos = self.noise_neg = 0.0
        self.noise_checked = 0.0   # highest reading compared with the threshold
        self.pending_noise = (0.0, 0.0, 0.0)
        self.bases = None           # resting reading of all three sensor axes
        self.sums = [0.0, 0.0, 0.0]  # integrated readings since the pass started
        self.sample_dt = 0.0
        self.first_clock = self.last_end_clock = None
        self.auto_direction = 0     # impact direction found from the travel
        self.moving = False         # toolhead seen moving with the steps
        self.motion_seen = False    # relative motion proof, including slow homes
        self.effective_speed = self.effective_accel = None
        self.varied = False         # readings changed at least once (sensor not frozen)
        self.gap = False            # samples were missing, so the speed is not known
        self.pressed_checks = 0
        self.checked_clock = None
        self.steppers = []
        self.halt_delta = None      # axis movement from start to halt, signed
        self.onset_batch = None
        self.onset_raw = 0.0
        self.impact_peak = 0.0
        self.impact_until = None
        self.record = None
        self.start_position = self.expected_travel = None


class BeaconAccelEndstop:
    """Endstop object returned for endstop_pin: beacon_accel:x or beacon_accel:y."""

    def __init__(self, owner, axis):
        self._owner = owner
        self.axis = axis
        self._mcu = None
        self._dispatch = None
        self._triggers = []

    # Endstop interface used by the homing code
    def get_mcu(self):
        return self._mcu

    def add_stepper(self, stepper):
        # The trigger sync lives on the stepper MCU, taken from the first stepper.
        if self._dispatch is None:
            self._mcu = stepper.get_mcu()
            self._dispatch = mcu.TriggerDispatch(self._mcu)
        self._dispatch.add_stepper(stepper)

    def get_steppers(self):
        return self._dispatch.get_steppers() if self._dispatch is not None else []

    def setup_trigger(self, command_for):
        # One trsync per stepper MCU, each triggered on a dedicated command queue
        # of its MCU, so the stop never waits behind queued trsync or stepper
        # traffic (measured 20-74 ms delays on the shared queue).
        if self._dispatch is None:
            return
        trsyncs = getattr(self._dispatch, "_trsyncs", None)
        if trsyncs:
            self._triggers = [(command_for(t.get_mcu()), t.get_oid()) for t in trsyncs]
        else:
            self._triggers = [(command_for(self._mcu), self._dispatch.get_oid())]

    def home_start(self, print_time, sample_time, sample_count, rest_time, triggered=True):
        attempt = self._owner.prepare(self, print_time)
        completion = self._dispatch.start(print_time)
        self._owner.arm(attempt)
        return completion

    def home_wait(self, home_end_time):
        try:
            self._dispatch.wait_end(home_end_time)
        finally:
            attempt = self._owner.disarm()
        result = self._dispatch.stop()
        return self._owner.finish(self, attempt, result)

    def query_endstop(self, print_time):
        return 0

    def trigger(self, reason):
        for command, oid in self._triggers:
            command.send([oid, reason])


class BeaconAccelHoming:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        if not callable(getattr(mcu, "TriggerDispatch", None)):
            raise config.error("accel_homing: this fork/version has no MCU TriggerDispatch;"
                               " update to a compatible Klipper or Kalico version")
        other = "accel_homing" if self.name == "beacon_accel_homing" else "beacon_accel_homing"
        if config.has_section(other):
            raise config.error("Use only one [accel_homing] or [beacon_accel_homing] section")
        self.sensor_name = select_sensor(config)
        self.is_beacon = self.sensor_name.split()[0] == "beacon"
        self.beacon_name = self.sensor_name
        if config.get("mcu", None) is not None:
            config.deprecate("mcu")  # the stepper MCU now comes from the steppers
        threshold = config.getfloat("threshold", 8.0, minval=3.0, maxval=15.0)
        self.threshold_g = {a: config.getfloat("%s_threshold" % a, threshold, minval=3.0, maxval=15.0)
                            for a in AXES}
        self.impact_sign = {a: config.getint("%s_impact_sign" % a, 0, minval=-1, maxval=1)
                            for a in AXES}
        self.homing_accel = config.getfloat("homing_accel", 5000.0, minval=0.0)
        self.back_off_dist = config.getfloat("back_off_dist", 5.0, minval=0.0, maxval=20.0)
        self.baseline_batches = config.getint("baseline_batches", 25, minval=10, maxval=200)
        self.baseline_time = config.getfloat("baseline_time", 0.1, minval=0.05, maxval=1.0)
        self.enable_settle_time = config.getfloat("enable_settle_time", ENABLE_SETTLE,
                                                  minval=0.0, maxval=2.0)
        self.stream_timeout = config.getfloat("stream_timeout", 0.05, minval=0.01, maxval=0.5)
        self.travel_margin = config.getfloat("travel_margin", 1.1, minval=1.0, maxval=2.0)
        self.watchdog_margin = config.getfloat("watchdog_margin", 0.15, minval=0.0, maxval=2.0)
        scalar_options = {"threshold": threshold, "homing_accel": self.homing_accel,
                          "back_off_dist": self.back_off_dist, "baseline_time": self.baseline_time,
                          "enable_settle_time": self.enable_settle_time,
                          "stream_timeout": self.stream_timeout, "travel_margin": self.travel_margin,
                          "watchdog_margin": self.watchdog_margin}
        scalar_options.update((axis + "_threshold", value) for axis, value in self.threshold_g.items())
        for option, value in scalar_options.items():
            if not math.isfinite(value):
                raise config.error("accel_homing: %s must be finite" % option)
        # Load the sensor first so its MCU is configured before our connect handler.
        self.sensor = self.printer.load_object(config, self.sensor_name)
        self.backend = None if self.is_beacon else ConfiguredAccelBackend(self, config, self.sensor)
        if self.backend is not None and config.get("threshold", None) is None:
            default_threshold = max(3.0, min(8.0, self.backend.full_scale * 0.5))
            if self.backend.full_scale * 0.8 < 3.0:
                raise config.error("accel_homing: configured sensor range is too small"
                                   " for the minimum 3 g homing threshold")
            for axis in AXES:
                if config.get(axis + "_threshold", None) is None:
                    self.threshold_g[axis] = default_threshold
        if self.backend is not None and self.stream_timeout > self.backend.max_age:
            self.stream_timeout = self.backend.max_age

        self.lock = threading.Lock()
        self.endstops = {}
        self.trigger_cmds = {}
        self.beacon = self.helper = self.toolhead = self.main_mcu = None
        self.impact_window = 0
        self.beacon_freq = 1.0
        self.saved_handler = None
        self.sensor_clock = self.clock_to_time = None
        self.sensor_fault = None
        self.scale = self.session_scale = None
        self.clip_values = (CLIPPED, -CLIPPED - 1)
        self.stream_alone = False
        self.full_scale_g = None
        self.recent = collections.deque(maxlen=self.baseline_batches)
        self.attempt = self.tail = self.pending_move = None
        self.homing_active = self.stream_on = False
        self.preparing = False
        self.preparation = None
        self.session_axes = {}
        self.rails_by_axis = {}
        self.passes = 0
        self.saved_accel = None
        self.last_batch = None
        self.last_activity = 0.0
        self.homed_before = self.last_pass = None
        self.last = {}
        self.history = collections.deque(maxlen=20)
        self.capture_batches = None
        self.capture_samples = self.capture_dropped = 0
        self.capture_limit = 200000
        self.capture_baseline = None
        self.watchdog = self.reactor.register_timer(self._watchdog)
        self.idle_timer = self.reactor.register_timer(self._idle_check)

        self.printer.lookup_object("pins").register_chip("beacon_accel", self)
        self.printer.lookup_object("pins").register_chip("accel_homing", self)
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("homing:home_rails_begin", self._handle_rails_begin)
        self.printer.register_event_handler("homing:home_rails_end", self._handle_rails_end)
        self.printer.register_event_handler("homing:homing_move_begin", self._handle_move_begin)
        self.printer.register_event_handler("homing:homing_move_end", self._handle_move_end)
        self.printer.register_event_handler("stepper_enable:motor_off", self._end_session)
        self.printer.register_event_handler("klippy:shutdown", self._end_session)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("BEACON_ACCEL_HOMING_STATUS", self.cmd_STATUS,
                               desc="Report the last Beacon accelerometer homing results")
        gcode.register_command("SET_BEACON_ACCEL_HOMING", self.cmd_SET,
                               desc="Change the threshold or impact sign until restart")
        gcode.register_command("SET_ACCEL_HOMING", self.cmd_SET,
                               desc="Change independent X/Y thresholds until restart")
        try:
            from .accel_homing_workflows import AccelHomingWorkflows
        except ImportError:
            from accel_homing_workflows import AccelHomingWorkflows
        self.workflows = AccelHomingWorkflows(self, config)

    # Pin chip
    def setup_pin(self, pin_type, pin_params):
        if pin_type != "endstop" or pin_params["pin"] not in AXES:
            raise self.printer.config_error("beacon_accel pins are 'x' and 'y' endstops only")
        if pin_params["invert"] or pin_params["pullup"]:
            raise self.printer.config_error("beacon_accel endstops cannot be inverted or pulled up")
        axis = pin_params["pin"]
        if axis in self.endstops:
            raise self.printer.config_error("beacon_accel:%s is used twice" % axis)
        self.endstops[axis] = BeaconAccelEndstop(self, axis)
        return self.endstops[axis]

    # Startup checks: problems show at restart, not in the middle of a home
    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.main_mcu = self.printer.lookup_object("mcu")
        if self.is_beacon:
            self._attach_beacon()
            self.sensor_clock = self.beacon._mcu
            self.clock_to_time = self.beacon._clock32_to_time
        else:
            self.helper = self.backend.helper
            self.scale = {"name": "normalized_g", "id": None, "scale": 1.0}
            self.sensor_clock = self.backend
            self.clock_to_time = self.backend.clock_to_time
        self.impact_window = int(self.sensor_clock.seconds_to_clock(IMPACT_WINDOW))
        self.beacon_freq = float(self.sensor_clock.seconds_to_clock(1.0))
        full_scale = self._full_scale_g(self.scale)
        if full_scale is not None and max(self.threshold_g.values()) > 0.8 * full_scale:
            raise self.printer.config_error(
                "beacon_accel_homing: threshold must stay below %.1f g, the"
                " accelerometer range is %.0f g" % (0.8 * full_scale, full_scale))
        for endstop in self.endstops.values():
            endstop.setup_trigger(self._trigger_command)
        if self.homing_accel and not any(hasattr(self.toolhead, n) for n in (
                "set_max_velocities", "set_accel", "_calc_junction_deviation")):
            raise self.printer.config_error(
                "beacon_accel_homing: this Klipper version cannot change the homing"
                " acceleration. Update it or set homing_accel: 0")
        configfile = self.printer.lookup_object("configfile")
        for rail in getattr(self.toolhead.get_kinematics(), "rails", []):
            axes = [axis for axis, es in self.endstops.items()
                    if any(es is candidate for candidate, _name in rail.get_endstops())]
            if axes:
                for axis in axes:
                    self.rails_by_axis[axis] = rail
                if self.backend is not None:
                    self.backend.validate_rail(rail.get_homing_info(), self.printer.config_error)
                for msg in homing_warnings(rail.get_name(), rail.get_homing_info()):
                    configfile.runtime_warning(msg)

    def _trigger_command(self, stepper_mcu):
        command = self.trigger_cmds.get(stepper_mcu)
        if command is None:
            queue = stepper_mcu.alloc_command_queue()
            command = stepper_mcu.lookup_command(TRIGGER_CMD, cq=queue)
            self.trigger_cmds[stepper_mcu] = command
        return command

    def _attach_beacon(self):
        error = self.printer.config_error
        beacon = self.printer.lookup_object(self.beacon_name)
        helper = getattr(beacon, "accel_helper", None)
        if helper is None:
            raise error("beacon_accel_homing: the Beacon reports no accelerometer."
                        " A Beacon RevH (or newer) with current firmware is required")
        # Internal names of beacon_klipper this module relies on.
        missing = [n for n, obj in (
            ("accel_stream_cmd", helper), ("_scale", helper), ("_stream_en", helper),
            ("config", helper), ("_handle_accel_data", helper), ("_mcu", beacon),
            ("compat_mcu_register_response", beacon), ("_clock32_to_time", beacon))
            if not hasattr(obj, n)]
        if missing or not hasattr(helper.config, "axes_map"):
            raise error("beacon_accel_homing: this beacon_klipper version is not supported"
                        " (missing %s)" % ", ".join(missing or ["axes_map"]))
        scales = list(getattr(helper, "_scales", {}).values()) or [helper._scale]
        widest = max(scales, key=lambda s: s.get("scale", 0.0))
        if not 0.01 < widest.get("scale", 0.0) < 100.0:
            raise error("beacon_accel_homing: implausible Beacon accelerometer scale %r" % (widest,))
        self.beacon, self.helper, self.scale = beacon, helper, widest
        clips = getattr(helper, "_clip_values", None)
        self.clip_values = tuple(clips) if clips else (CLIPPED, -CLIPPED - 1)
        serial = getattr(beacon._mcu, "_serial", None)
        self.saved_handler = getattr(serial, "handlers", {}).get(
            ("beacon_accel_data", None), helper._handle_accel_data)
        beacon.compat_mcu_register_response(self._batch_hook, BEACON_ACCEL_DATA)

    def _full_scale_g(self, scale):
        if self.backend is not None:
            return self.backend.full_scale
        clip = getattr(self.helper, "_clip_values", (None,))[0]
        return clip * scale["scale"] / 1000.0 if clip else None

    def _raw_axis(self, axis):
        """Sensor index and sign of a machine axis, from Beacon's accel_axes_map."""
        if self.backend is not None:
            return AXES[axis], 1  # The configured driver already applies axes_map.
        index, sign = self.helper.config.axes_map[AXES[axis]]
        return index, sign

    # Stream and acceleration, owned only for the duration of a homing session
    def _start_stream(self):
        self.sensor_fault = None
        if self.backend is not None:
            self.session_scale = self.scale
            self.full_scale_g = self.backend.full_scale
            self.backend.start()
            self.stream_on = True
            return
        helper = self.helper
        # Streaming alone at our own scale: a Beacon client starting later
        # (the web API dump) would switch the sensor range mid-move.
        self.stream_alone = not helper._stream_en
        if helper._stream_en:
            # Already streaming for someone else, at their scale.
            self.session_scale = helper._scale
        else:
            self.session_scale = self.scale
            helper.accel_stream_cmd.send([1, self.scale["id"]])
        self.stream_on = True
        self.full_scale_g = self._full_scale_g(self.session_scale)

    def _stop_stream(self):
        if self.backend is not None:
            self.stream_on = False
            self.backend.stop()
            return
        if self.stream_on:
            self.stream_on = False
            if not self.helper._stream_en:
                self.helper.accel_stream_cmd.send([0, 0])

    def _limit_accel(self):
        accel = self.toolhead.get_max_velocity()[1]
        if self.homing_accel and accel > self.homing_accel and self.saved_accel is None:
            self.saved_accel = accel
            self._set_accel(self.homing_accel)

    def _restore_accel(self):
        if self.saved_accel is not None:
            accel, self.saved_accel = self.saved_accel, None
            self._set_accel(accel)

    def _set_accel(self, accel):
        toolhead = self.toolhead
        if hasattr(toolhead, "set_max_velocities"):   # Klipper since 2024
            toolhead.set_max_velocities(None, accel, None, None)
        elif hasattr(toolhead, "set_accel"):          # Kalico
            toolhead.set_accel(accel)
        else:                                         # older Klipper
            toolhead.max_accel = accel
            toolhead._calc_junction_deviation()

    def _end_session(self, *args):
        """Stop the stream and restore the acceleration. Safe to call any time."""
        self.homing_active = False
        with self.lock:
            self.preparation = None
        self.preparing = False
        self.session_axes = {}
        self.reactor.update_timer(self.idle_timer, self.reactor.NEVER)
        if self.helper is not None:
            self._stop_stream()
        if self.toolhead is not None:
            self._restore_accel()

    def _idle_check(self, eventtime):
        # Klipper sends no event when homing fails outside G28, so a session
        # left open closes itself once nothing has been armed for a while.
        if not self.homing_active:
            return self.reactor.NEVER
        if not self.preparing and self.attempt is None and eventtime - self.last_activity > IDLE_CLEANUP:
            logging.info("beacon_accel_homing: closing a homing session left open")
            self._end_session()
            return self.reactor.NEVER
        return eventtime + 0.5

    # Homing lifecycle
    def _handle_rails_begin(self, homing, rails):
        # Rails come from the homing call itself, so no kinematics or rail order
        # is assumed.
        axes = {}
        for rail in rails:
            for es, _name in rail.get_endstops():
                for axis, endstop in self.endstops.items():
                    if es is endstop:
                        axes[axis] = rail
        if not axes:
            return
        self.workflows.check_home_axes(axes)
        self._end_session()
        if len(axes) > 1:
            raise self.printer.command_error("beacon_accel_homing homes one axis at a time")
        if self.helper is None:
            raise self.printer.command_error("beacon_accel_homing: accelerometer is not available")
        if self.backend is not None:
            for rail in axes.values():
                self.backend.validate_rail(rail.get_homing_info(), self.printer.command_error)
        self.rails_by_axis.update(axes)
        self.session_axes = axes
        self.passes = 0
        self.recent.clear()
        self.tail = self.last_pass = None
        self.homed_before = self._homed_at_stop(*list(axes.items())[0])
        self.homing_active = True
        self.last_activity = started = self.reactor.monotonic()
        self.reactor.update_timer(self.idle_timer, started + 0.5)
        self.preparing = True
        try:
            self._limit_accel()
            self._enable_motors(list(axes)[0])
            self._back_off_others(list(axes)[0])
            if not self.homing_active or self.session_axes is not axes:
                raise self.printer.command_error("accel_homing: homing preparation cancelled")
            self.last_batch = None
            # Enabling/backoff may itself yield while the toolhead drains a
            # large queue. Sensor startup gets its own actual start time.
            self.last_activity = started = self.reactor.monotonic()
            self._start_stream()
        except Exception:
            self._end_session()
            raise
        finally:
            self.preparing = False
        # Wait in real time until accelerometer data actually flows.
        while self.last_batch is None or self.last_batch < started:
            now = self.reactor.monotonic()
            if self.sensor_fault is not None:
                reason = self.sensor_fault
                self._end_session()
                raise self.printer.command_error("accel_homing: %s" % FAILURES[reason])
            if now - started > STREAM_START_TIMEOUT:
                self._end_session()
                raise self.printer.command_error(
                    "beacon_accel_homing: accelerometer stream did not start")
            self.reactor.pause(now + 0.005)

    def _homed(self, axis):
        kin = self.toolhead.get_kinematics()
        try:
            return axis in kin.get_status(self.reactor.monotonic())["homed_axes"]
        except Exception:
            return None

    def _enable_motors(self, axis):
        """Switch on the motors of the homing move before the resting reading.
        Klipper switches a motor on with its first step, and the snap of motors
        that were off (after M84 or a failed home) fired the trigger at the start
        of the move."""
        stepper_enable = self.printer.lookup_object("stepper_enable", None)
        kin = self.toolhead.get_kinematics()
        if stepper_enable is None or kin is None:
            return
        lines = []
        for stepper in kin.get_steppers():
            try:
                if stepper.is_active_axis(axis):
                    line = stepper_enable.lookup_enable(stepper.get_name())
                    if not line.is_motor_enabled():
                        lines.append(line)
            except Exception:
                continue
        if not lines:
            return
        self.toolhead.flush_step_generation()
        print_time = self.toolhead.get_last_move_time()
        for line in lines:
            line.motor_enable(print_time)
        logging.info("beacon_accel_homing: switched on %d motors before homing %s",
                     len(lines), axis.upper())
        self.toolhead.dwell(self.enable_settle_time)

    def _back_off_others(self, homing_axis):
        """Move axes this module homed off their stops before homing another one.
        An axis left pushed into its stop keeps the belts and the gantry loaded,
        which disturbs the next axis (false triggers, lost steps)."""
        others = [(axis, rail) for axis, rail in self.rails_by_axis.items()
                  if axis != homing_axis and self.back_off_dist and self._homed(axis)]
        if not others:
            return
        coord, speeds = [None, None, None], []
        position = self.toolhead.get_position()
        for axis, rail in others:
            info = rail.get_homing_info()
            away = -1.0 if info.positive_dir else 1.0
            # Distance from the stop into the travel range, negative past the stop.
            free = (position[AXES[axis]] - info.position_endstop) * away
            if free >= self.back_off_dist - 0.01:
                continue
            low, high = rail.get_range()
            target = info.position_endstop + away * self.back_off_dist
            coord[AXES[axis]] = min(max(target, low), high)
            speeds.append(getattr(info, "retract_speed", None) or info.speed)
        if speeds:
            self.toolhead.manual_move(coord, min(speeds))
            # Let the gantry settle before the resting reading is taken.
            self.toolhead.dwell(self.baseline_time)

    def _homed_at_stop(self, axis, rail):
        """Position before homing if the axis is homed and Klipper already has the
        toolhead at the stop, or within 5 mm past it in the homing direction."""
        homed = self._homed(axis)
        if homed is None:
            return None
        info = rail.get_homing_info()
        position = self.toolhead.get_position()[AXES[axis]]
        past = position - info.position_endstop
        past = past if info.positive_dir else -past
        return position if homed and -0.01 <= past <= 5.0 else None

    def _handle_rails_end(self, homing, rails):
        if not self.homing_active:
            return
        attempt = self.last_pass
        if (attempt is not None and attempt.reason == "pressed" and self.homed_before is not None
                and attempt.halt_delta is not None):
            # Pressed start on an axis that was already homed at the stop, for
            # example G28 twice in a row. The accelerometer cannot measure how
            # far the belts were already stretched, so the previous origin is
            # kept instead of moving it by that stretch on every home.
            position = self.toolhead.get_position()
            position[AXES[attempt.axis]] = self.homed_before + attempt.halt_delta
            self.toolhead.set_position(position)
            attempt.record["kept_origin"] = True
        self._end_session()

    def _handle_move_begin(self, hmove):
        # A standstill right before every pass (first touch and second touch)
        # gives the resting reading the impact is compared against.
        if self.homing_active and any(es in self.endstops.values() for es in hmove.get_mcu_endstops()):
            self._prepare_reference()

    def _prepare_reference(self):
        """Finish queued enable/backoff/retract, then acquire before trsync/motion.

        Stock homing captures the move's print time only after this event.
        A queued dwell alone does not establish that the physical enable time
        has passed or that delivered sensor data was sampled afterwards.
        """
        self.preparing = True
        session_axes = self.session_axes
        try:
            self.toolhead.flush_step_generation()
            self.toolhead.wait_moves()
            if not self.homing_active or self.session_axes is not session_axes:
                raise self.printer.command_error("accel_homing: homing preparation cancelled")
            started = self.reactor.monotonic()
            cutoff = self.main_mcu.estimated_print_time(started)
            axis = next(iter(self.session_axes))
            count_g = self.session_scale["scale"] / 1000.0
            reference = QuietReference(axis, cutoff, self.baseline_time,
                                       BASELINE_NOISE_RATIO * self.threshold_g[axis] / count_g)
            with self.lock:
                self.recent.clear()
                self.tail = None
                self.preparation = reference
                if self.backend is not None:
                    self.backend.prepare_reference()
            while True:
                now = self.reactor.monotonic()
                self.last_activity = now
                if not self.homing_active or self.session_axes is not session_axes:
                    raise self.printer.command_error("accel_homing: homing preparation cancelled")
                if self.sensor_fault is not None:
                    raise self.printer.command_error("accel_homing: %s" % FAILURES[self.sensor_fault])
                with self.lock:
                    ready = reference.baseline() is not None
                    last = reference.last
                sensor_now = self.main_mcu.estimated_print_time(now)
                if ready and last is not None and -0.005 <= sensor_now - last <= self._reference_max_age():
                    return
                if now - started > self.baseline_time + STREAM_START_TIMEOUT:
                    raise self.printer.command_error("accel_homing: %s" % FAILURES["no_baseline"])
                self.reactor.pause(now + 0.005)
        except Exception:
            self._end_session()
            raise
        finally:
            self.preparing = False

    def _reference_max_age(self):
        return self.stream_timeout if self.backend is None else self.backend.max_age

    def _collect_reference(self, params, data, count, now):
        """Validate sensor time and every axis while no homing move is armed."""
        interval = params["delta_clock"] / (count - 1.0) / self.beacon_freq
        start = self.clock_to_time(params["start_clock"])
        if not math.isfinite(interval) or interval <= 0.0 or not math.isfinite(start):
            return "invalid_data"
        sensor_now = self.main_mcu.estimated_print_time(now)
        raw = [axis_values(sample_slice(data, count), index) for index in range(3)]
        with self.lock:
            reference = self.preparation
            if reference is None:
                return None
            if (self.backend is None and self.helper._stream_en
                    and self.helper._scale.get("id") != self.session_scale.get("id")):
                return "scale"
            for number in range(count):
                when = start + number * interval
                if when <= reference.cutoff:
                    continue
                if not -0.005 <= sensor_now - when <= self._reference_max_age():
                    return "stale_data"
                if reference.last is not None:
                    if when <= reference.last:
                        return "invalid_data"
                    if when - reference.last > 5.0 * interval + 0.000003:
                        return "sample_gap"
                values = tuple(axis[number] for axis in raw)
                if (not all(math.isfinite(value) for value in values)
                        or (self.backend is None and CLIPPED in values)
                        or (self.full_scale_g is not None
                            and any(abs(value) * self.session_scale["scale"] / 1000.0
                                    >= self.full_scale_g for value in values))):
                    return "invalid_data"
                reference.add(when, values)
        return None

    def _handle_move_end(self, hmove):
        attempt, self.pending_move = self.pending_move, None
        if attempt is None or attempt.endstop not in hmove.get_mcu_endstops():
            return
        record = attempt.record
        positions = [sp for sp in getattr(hmove, "stepper_positions", []) if sp.trig_pos is not None]
        record["trigger_position_mm"] = {
            sp.stepper_name: round(sp.trig_pos * sp.stepper.get_step_dist(), 4) for sp in positions}
        travel = self._travel(positions, attempt.axis)
        record["travel_mm"] = None if travel is None else round(abs(travel), 3)
        attempt.halt_delta = self._travel(positions, attempt.axis, halt=True)
        self.last_pass = attempt
        self.workflows.check_home_result(record)
        logging.info("beacon_accel_homing: %s, %.4f s after arm", describe(record),
                     record["trigger_after_arm_s"] or 0.0)
        if travel is not None:
            travel = abs(travel)
        if attempt.reason != "impact" or travel is None:
            return
        if attempt.pass_no >= 2:
            info = self.session_axes[attempt.axis].get_homing_info()
            # The second touch starts one retract away from the stop. A trigger
            # much later came from somewhere else, for example the end of a
            # second touch that followed an early first trigger.
            dists = [d for d in (info.retract_dist, getattr(info, "min_home_dist", 0.0)) if d]
            if dists and not (min(dists) - SECOND_TOUCH_TOLERANCE <= travel
                              <= max(dists) + SECOND_TOUCH_TOLERANCE):
                raise self.printer.command_error(
                    "beacon_accel_homing: second touch on %s triggered after %.2f mm instead"
                    " of about %.1f mm. The trigger was not at the stop, homing stopped"
                    % (attempt.axis.upper(), travel,
                       min(dists) if travel < min(dists) else max(dists)))

    def _travel(self, positions, axis, halt=False):
        """Signed distance moved along the homing axis until the trigger (or the
        halt), from the steppers."""
        try:
            kin = self.toolhead.get_kinematics()
            moved = {s.get_name(): 0.0 for s in kin.get_steppers()}
            for sp in positions:
                end = sp.halt_pos if halt else sp.trig_pos
                moved[sp.stepper_name] = (end - sp.start_pos) * sp.stepper.get_step_dist()
            return kin.calc_position(moved)[AXES[axis]]
        except Exception:
            logging.exception("beacon_accel_homing: travel calculation failed")
            return None

    def prepare(self, endstop, print_time):
        axis = endstop.axis
        rail = self.session_axes.get(axis)
        if rail is None or not self.homing_active:
            raise self.printer.command_error(
                "beacon_accel_homing: %s can only be homed with G28" % axis.upper())
        self.passes += 1
        info = rail.get_homing_info()
        max_velocity, accel = self.toolhead.get_max_velocity()
        if (not all(math.isfinite(value) and value > 0 for value in
                    (max_velocity, accel, info.speed, info.second_homing_speed))
                or not math.isfinite(self.threshold_g[axis])):
            raise self.printer.command_error("accel_homing: homing speed, acceleration"
                                             " and threshold must be finite positive values")
        if self.passes == 1:
            low, high = rail.get_range()
            travel, speed = (high - low) * self.travel_margin, info.speed
        else:
            retract = max(info.retract_dist, getattr(info, "min_home_dist", 0.0) or 0.0)
            travel, speed = 2.0 * retract + 3.0, info.second_homing_speed
        # The toolhead never moves faster than its max_velocity.
        speed = min(speed, max_velocity)
        if self.backend is not None:
            self.backend.validate_motion_plan(speed, accel, self.printer.command_error)
        # Cruise time plus the time lost while ramping up to speed.
        deadline = print_time + travel / speed + speed / accel + self.watchdog_margin
        count_g = self.session_scale["scale"] / 1000.0
        threshold = self.threshold_g[axis]
        if self.full_scale_g is not None and threshold > 0.8 * self.full_scale_g:
            raise self.printer.command_error(
                "beacon_accel_homing: the accelerometer range of %.0f g is too small for a"
                " %.1f g threshold" % (self.full_scale_g, threshold))
        index, map_sign = self._raw_axis(axis)
        direction = self.impact_sign[axis]
        if self.sensor_fault is not None:
            raise self.printer.command_error("accel_homing: %s" % FAILURES[self.sensor_fault])
        now = self.main_mcu.estimated_print_time(self.reactor.monotonic())
        with self.lock:
            reference = self.preparation
            bases = reference.baseline() if reference is not None and reference.axis == axis else None
            if (bases is None or reference.last is None
                    or not -0.005 <= now - reference.last <= self._reference_max_age()):
                bases = None
            self.preparation = None
        if bases is None:
            self._end_session()
            raise self.printer.command_error("accel_homing: %s" % FAILURES["no_baseline"])
        beacon_mcu = self.sensor_clock
        attempt = Attempt(endstop, self.passes, print_time,
                          beacon_mcu.print_time_to_clock(print_time) & 0xFFFFFFFF,
                          deadline, beacon_mcu.print_time_to_clock(deadline) & 0xFFFFFFFF,
                          index, map_sign, direction, threshold / count_g, count_g)
        attempt.steppers = endstop.get_steppers()
        attempt.bases = bases
        attempt.baseline = bases[index]
        attempt.last_samples = list(bases)
        attempt.effective_speed, attempt.effective_accel = speed, accel
        if hasattr(self.toolhead, "get_position"):
            attempt.start_position = self.toolhead.get_position()[AXES[axis]]
            attempt.expected_travel = abs(attempt.start_position - info.position_endstop)
        return attempt

    def arm(self, attempt):
        attempt.last_data = self.last_activity = self.reactor.monotonic()
        with self.lock:
            self.tail = None
            self.attempt = attempt
        self.reactor.update_timer(self.watchdog, self.reactor.NOW)

    def disarm(self):
        with self.lock:
            attempt, self.attempt = self.attempt, None
            if attempt is not None:
                attempt.done = True
        self.reactor.update_timer(self.watchdog, self.reactor.NEVER)
        self.last_activity = self.reactor.monotonic()
        if attempt is not None:
            self._record(attempt)
            self.pending_move = attempt
            if attempt.onset_batch is not None:
                self.tail = attempt
        return attempt

    def finish(self, endstop, attempt, result):
        if result >= mcu.MCU_trsync.REASON_COMMS_TIMEOUT:
            raise self.printer.command_error("Communication timeout during homing")
        if attempt is not None and attempt.reason in FAILURES:
            raise self.printer.command_error(
                "beacon_accel_homing: %s on %s, homing stopped"
                % (FAILURES[attempt.reason], endstop.axis.upper()))
        if result != mcu.MCU_trsync.REASON_ENDSTOP_HIT or attempt is None:
            return 0.0
        return self.trigger_time(attempt)

    def trigger_time(self, attempt):
        """Time Klipper takes the trigger position from.

        A toolhead that never moved with the steps was already pressed against
        the stop, so the origin is where the move started. Otherwise it is the
        first accelerometer sample over the threshold."""
        if attempt.reason == "impact" and self._motion_state(attempt) is True:
            attempt.reason = "pressed"
            if attempt.record is not None:
                attempt.record["result"] = "pressed"
        if attempt.record is not None:
            attempt.record["moving"] = attempt.moving
            attempt.record["motion_seen"] = attempt.motion_seen or attempt.moving
        if attempt.reason == "pressed":
            return attempt.arm_time
        if attempt.onset_batch is None:
            return 0.0
        return self.onset_time(attempt)

    def _commanded_speed(self, attempt, print_time, window=COMMAND_WINDOW):
        """Stepper speed along the move at a print time, from the step history."""
        speed = 0.0
        for stepper in attempt.steppers:
            moved = (stepper.get_past_mcu_position(print_time)
                     - stepper.get_past_mcu_position(print_time - window))
            speed = max(speed, abs(moved) * stepper.get_step_dist() / window)
        return speed

    def _measured_speed(self, attempt):
        """Speed of the toolhead in any direction, integrated from all three axes."""
        scale = attempt.count_g * MM_PER_S2_PER_G * attempt.sample_dt
        return sum((v * scale) ** 2 for v in attempt.sums) ** 0.5

    def _motion_state(self, attempt):
        """True when the steps run but the toolhead does not move, False once it
        moves with them, None while that cannot be told yet."""
        if attempt.moving:
            return False
        if attempt.last_end_clock is None or attempt.gap:
            # Missing samples leave the integrated speed too low, so a moving
            # toolhead could look pressed. Only an impact can end such a move.
            return None
        measured_at = self.clock_to_time(attempt.last_end_clock)
        commanded = self._commanded_speed(attempt, measured_at - MOTION_LAG)
        measured = self._measured_speed(attempt)
        # Workflow proof must also work when the configured rail or toolhead
        # velocity limit is below the legacy pressed-start cutoff.  A slightly
        # wider step-history window avoids single-step quantization at very low
        # speeds.  This does not alter Beacon's pressed-start decision below.
        effective = attempt.effective_speed
        if effective is None:
            effective = self.backend.max_speed if self.backend is not None else PRESSED_MIN_SPEED
        proof_min_speed = min(PRESSED_MIN_SPEED, 0.5 * effective)
        window = motion_proof_window(effective)
        proof_commanded = (commanded if window == COMMAND_WINDOW else
                           self._commanded_speed(attempt, measured_at - MOTION_LAG, window))
        if proof_commanded >= proof_min_speed and measured >= MOVING_RATIO * proof_commanded:
            attempt.motion_seen = True
        if self.backend is not None:
            # A generic stream may prove motion at its actual homing speed,
            # but missing motion is never a successful pressed-start home.
            if attempt.motion_seen:
                attempt.moving = True
                return False
            return None
        if commanded < PRESSED_MIN_SPEED:
            return None
        if measured >= MOVING_RATIO * commanded:
            attempt.moving = True
            return False
        return True if measured < PRESSED_RATIO * commanded else None

    def _motion_startup_plan(self, attempt):
        speed, accel = self.toolhead.get_max_velocity()
        speed = attempt.effective_speed or min(speed, self.backend.max_speed)
        accel = attempt.effective_accel or (min(accel, self.homing_accel)
                                           if self.homing_accel else accel)
        return speed, accel, self.backend.motion_startup_timeout(speed, accel)

    def _check_no_motion(self, attempt, print_time):
        """Generic-only fail-closed startup check, independent of impact.

        The reporting/lag allowance leaves the complete acceleration ramp in
        fresh samples before proof is required.  If proof is still absent,
        stop with an error instead of pushing until the full-travel watchdog.
        """
        self._motion_state(attempt)
        if attempt.motion_seen or attempt.moving:
            return False
        _speed, _accel, timeout = self._motion_startup_plan(attempt)
        if print_time < attempt.arm_time + timeout:
            return False
        self._fire(attempt, "no_motion", mcu.MCU_trsync.REASON_HOST_REQUEST)
        return True

    def _fire(self, attempt, reason, code):
        with self.lock:
            # A move that already ended must never stop the next one.
            if attempt.done or attempt is not self.attempt:
                return
            attempt.done = True
            attempt.reason = reason
            attempt.endstop.trigger(code)

    def _watchdog(self, eventtime):
        """Data-independent safety net: stops the move if the stream dies or travel runs out."""
        attempt = self.attempt
        if attempt is None or attempt.done:
            return self.reactor.NEVER
        print_time = self.main_mcu.estimated_print_time(eventtime)
        if print_time < attempt.arm_time:
            attempt.last_data = max(attempt.last_data, eventtime)
            return eventtime + WATCHDOG_INTERVAL
        if eventtime - attempt.last_data > self.stream_timeout:
            self._fire(attempt, "no_data", mcu.MCU_trsync.REASON_HOST_REQUEST)
            return self.reactor.NEVER
        if self.backend is not None and self.backend.last_sample_time is not None:
            if print_time - self.backend.last_sample_time > self.backend.max_age:
                self._fire(attempt, "stale_data", mcu.MCU_trsync.REASON_HOST_REQUEST)
                return self.reactor.NEVER
        if print_time > attempt.deadline + self.stream_timeout:
            self._fire(attempt, "travel", mcu.MCU_trsync.REASON_HOST_REQUEST)
            return self.reactor.NEVER
        if attempt.moving:
            return eventtime + WATCHDOG_INTERVAL
        if self.backend is not None:
            if self._check_no_motion(attempt, print_time):
                return self.reactor.NEVER
            return eventtime + EARLY_INTERVAL
        if self._check_pressed(attempt):
            return self.reactor.NEVER
        return eventtime + EARLY_INTERVAL

    def _check_pressed(self, attempt):
        """Pressed against the stop: the steps run but the toolhead does not
        move. Stops the move after two checks on different data."""
        if attempt.checked_clock == attempt.last_end_clock:
            return False
        attempt.checked_clock = attempt.last_end_clock
        if self._motion_state(attempt) is not True:
            attempt.pressed_checks = 0
            return False
        attempt.pressed_checks += 1
        if attempt.pressed_checks < 2:
            return False
        if not attempt.varied:
            # A live sensor always shows some noise. Identical readings mean a
            # frozen stream, which would make a moving toolhead look pressed and
            # set the origin wherever the move started.
            self._fire(attempt, "frozen", mcu.MCU_trsync.REASON_HOST_REQUEST)
            return True
        self._fire(attempt, "pressed", mcu.MCU_trsync.REASON_ENDSTOP_HIT)
        return True

    # Sample path: Beacon serial thread, every batch (about 400 per second).
    def _batch_hook(self, params):
        try:
            self._inspect(params)
        except Exception:
            logging.exception("beacon_accel_homing: batch inspection failed")
            self._sensor_fault("invalid_data")
        if self.helper._stream_en:
            self.saved_handler(params)

    def _inspect(self, params):
        try:
            self._inspect_batch(params)
        finally:
            # Optional raw references are retained only after detector/trigger
            # processing.  Conversion for charts is deferred to stop_capture.
            if self.capture_batches is not None:
                self._capture_batch(params)

    def _sensor_fault(self, reason):
        self.sensor_fault = reason
        attempt = self.attempt
        if attempt is not None and not attempt.done:
            self._fire(attempt, reason, mcu.MCU_trsync.REASON_HOST_REQUEST)

    def _inspect_batch(self, params):
        data = params["data"]
        count = sample_count(data)
        if count < 2:
            return
        now = params.get("#receive_time") or self.reactor.monotonic()
        self.last_batch = now
        attempt = self.attempt
        if attempt is None:
            if self.tail is not None:
                self._track_impact(self.tail, params, data, count)
            if self.homing_active:
                if self.preparation is not None:
                    reason = self._collect_reference(params, data, count, now)
                    if reason is not None:
                        self._sensor_fault(reason)
                self.recent.append(data)
            return
        attempt.last_data = now
        if attempt.done:
            self._track_impact(attempt, params, data, count)
            return
        helper = self.helper
        if (self.backend is None and self.stream_alone and helper._stream_en
                and helper._scale.get("id") != self.session_scale.get("id")):
            self._fire(attempt, "scale", mcu.MCU_trsync.REASON_HOST_REQUEST)
            return
        start_clock = params["start_clock"]
        interval = params["delta_clock"] / (count - 1.0)
        if not clock_after(start_clock, attempt.arm_clock):
            # A delivered batch can contain both a pre-enable transient and
            # the first real impact. Preserve every sample at/after arm,
            # including a single remaining sample, instead of dropping it.
            skip = int(math.ceil(((attempt.arm_clock - start_clock) & 0xFFFFFFFF) / interval))
            if skip >= count:
                return
            width = 3 if isinstance(data, tuple) else 6
            data = data[skip * width:]
            count -= skip
            start_clock = (start_clock + int(round(skip * interval))) & 0xFFFFFFFF
            params = dict(params, data=data, start_clock=start_clock,
                          delta_clock=int(round((count - 1) * interval)))
        if attempt.first_clock is None:
            if attempt.bases is None:
                # Standalone detector replay uses no homing lifecycle. The
                # production prepare() path always supplies a validated one.
                attempt.bases = self._baseline()
            attempt.first_clock = start_clock
            attempt.sample_dt = interval / self.beacon_freq
            if attempt.bases is None:
                self._fire(attempt, "no_baseline", mcu.MCU_trsync.REASON_HOST_REQUEST)
                return
            attempt.baseline = attempt.bases[attempt.index]
            attempt.last_samples = list(attempt.bases)
            if ((start_clock - attempt.arm_clock) & 0xFFFFFFFF
                    > params["delta_clock"] + GAP_SAMPLES * interval):
                attempt.gap = True
        elif (attempt.last_end_clock is not None
              and (start_clock - attempt.last_end_clock) & 0xFFFFFFFF > GAP_SAMPLES * interval):
            attempt.gap = True
        if clock_after(start_clock, attempt.deadline_clock):
            self._fire(attempt, "travel", mcu.MCU_trsync.REASON_HOST_REQUEST)
            return
        data = sample_slice(data, count)
        raw = (data if isinstance(data, tuple) else memoryview(data).cast("h") if NATIVE_LE
               else [v for sample in SAMPLE.iter_unpack(data) for v in sample])
        if self.backend is None and CLIPPED in raw:
            raw = decode_clips(raw, attempt.last_samples, self.clip_values)
        values = raw[attempt.index::3]
        top, bottom = max(values), min(values)
        if top != bottom:
            attempt.varied = True
        base = attempt.baseline
        above, below = top - base, base - bottom
        pos, neg = (above, below) if attempt.map_sign > 0 else (below, above)
        direction = attempt.direction or attempt.auto_direction
        level = pos if direction > 0 else neg if direction < 0 else max(pos, neg)
        if level <= attempt.limit_raw:
            # Quiet batch: keep the cheap statistics only. Noise peaks count one
            # batch late, so the rising edge right before an impact is not noise.
            pending_pos, pending_neg, pending_level = attempt.pending_noise
            if pending_pos > attempt.noise_pos:
                attempt.noise_pos = pending_pos
            if pending_neg > attempt.noise_neg:
                attempt.noise_neg = pending_neg
            if pending_level > attempt.noise_checked:
                attempt.noise_checked = pending_level
            attempt.pending_noise = (pos, neg, level)
            sums, bases = attempt.sums, attempt.bases
            # One new list, so the watchdog never reads a half-updated set.
            sums = attempt.sums = [sums[0] + sum(raw[0::3]) - bases[0] * count,
                                   sums[1] + sum(raw[1::3]) - bases[1] * count,
                                   sums[2] + sum(raw[2::3]) - bases[2] * count]
            attempt.last_end_clock = start_clock + params["delta_clock"]
            attempt.last_samples = [raw[-3], raw[-2], raw[-1]]
            if not direction:
                # The stop decelerates the toolhead, so once it travels the
                # impact can only point against the travel. A start-of-move
                # jolt in the travel direction is then ignored.
                speed = (sums[attempt.index] * attempt.map_sign * attempt.count_g
                         * MM_PER_S2_PER_G * attempt.sample_dt)
                if abs(speed) >= AUTO_DIRECTION_SPEED:
                    attempt.auto_direction = -1 if speed > 0 else 1
            return
        self._onset(attempt, params, values, count)

    def _onset(self, attempt, params, values, count):
        base, map_sign = attempt.baseline, attempt.map_sign
        direction = attempt.direction or attempt.auto_direction
        # Clips are already decoded, so they pass the same direction check.
        for position, raw in enumerate(values):
            value = (raw - base) * map_sign
            if (value * direction if direction else abs(value)) > attempt.limit_raw:
                break
        else:
            return
        start_clock = params["start_clock"]
        attempt.onset_batch = (start_clock, params["delta_clock"], position, count)
        attempt.onset_raw = value
        attempt.impact_until = (start_clock + self.impact_window) & 0xFFFFFFFF
        self._fire(attempt, "impact", mcu.MCU_trsync.REASON_ENDSTOP_HIT)
        self._track_impact(attempt, params, None, count, values)

    def _track_impact(self, attempt, params, data, count, values=None):
        """Largest reading shortly after the onset, for status."""
        if attempt.impact_until is None or not clock_after(attempt.impact_until, params["start_clock"]):
            return
        if values is None:
            values = axis_values(sample_slice(data, count), attempt.index)
        top, bottom = max(values), min(values)
        peak = (self.full_scale_g / attempt.count_g
                if self.backend is None and top == CLIPPED and self.full_scale_g
                else max(top - attempt.baseline, attempt.baseline - bottom))
        if peak > attempt.impact_peak:
            attempt.impact_peak = peak
            if attempt.record is not None:
                attempt.record["impact_peak_g"] = round(peak * attempt.count_g, 2)

    def _baseline(self):
        totals, counts = [0, 0, 0], [0, 0, 0]
        for data in list(self.recent):
            for index in range(3):
                values = [v for v in axis_values(sample_slice(data, sample_count(data)), index)
                          if self.backend is not None or v != CLIPPED]
                totals[index] += sum(values)
                counts[index] += len(values)
        if min(counts) < BASELINE_MIN_SAMPLES:
            return None
        return tuple(t / c for t, c in zip(totals, counts))

    def onset_time(self, attempt):
        start_clock, delta_clock, position, count = attempt.onset_batch
        start = self.clock_to_time(start_clock)
        end = self.clock_to_time(start_clock + delta_clock)
        return start + (end - start) * position / max(count - 1, 1)

    def _velocity(self, attempt):
        """Speed along the homing axis before the impact, integrated from the
        accelerometer (machine frame of Beacon's axes map)."""
        if attempt.last_end_clock is None:
            return None
        return (attempt.sums[attempt.index] * attempt.map_sign * attempt.count_g
                * MM_PER_S2_PER_G * attempt.sample_dt)

    # Reporting
    def start_capture(self):
        """Capture one requested workflow in memory; no streaming or motion."""
        with self.lock:
            if self.capture_batches is not None:
                raise self.printer.command_error("accel_homing: capture already active")
            self.capture_batches = []
            self.capture_samples = self.capture_dropped = 0
            self.capture_baseline = None

    def _capture_batch(self, params):
        data = params["data"]
        count = sample_count(data)
        if count < 2:
            return
        with self.lock:
            if self.capture_batches is None:
                return
            if self.capture_samples + count > self.capture_limit:
                self.capture_dropped += count
                return
            self.capture_batches.append((params["start_clock"], params["delta_clock"],
                                         data, self.session_scale["scale"] / 1000.0))
            self.capture_samples += count
            attempt = self.attempt or self.last_pass
            if self.capture_baseline is None and attempt is not None and attempt.bases is not None:
                mapping = [self._raw_axis(a) for a in ("x", "y")]
                mapping.append((2, 1) if self.backend is not None else self.helper.config.axes_map[2])
                self.capture_baseline = tuple(attempt.bases[i] * sign * attempt.count_g
                                              for i, sign in mapping)

    def stop_capture(self):
        """Convert bounded captured batches to machine-axis g off the trigger path.

        `samples` include gravity and bias; `baseline_g` must be subtracted when
        comparing channels to directional impact thresholds in charts.
        """
        with self.lock:
            batches, self.capture_batches = self.capture_batches or [], None
            dropped, baseline = self.capture_dropped, self.capture_baseline
        mapping = ([(0, 1), (1, 1), (2, 1)] if self.backend is not None
                   else self.helper.config.axes_map)
        samples, previous = [], [0, 0, 0]
        for start_clock, delta_clock, data, count_g in batches:
            count = sample_count(data)
            raw = (list(data) if isinstance(data, tuple)
                   else [v for row in SAMPLE.iter_unpack(sample_slice(data, count)) for v in row])
            if self.backend is None and CLIPPED in raw:
                raw = decode_clips(raw, previous, self.clip_values)
            previous = raw[-3:]
            start, end = self.clock_to_time(start_clock), self.clock_to_time(start_clock + delta_clock)
            dt = (end - start) / (count - 1)
            for number in range(count):
                xyz = raw[number * 3:number * 3 + 3]
                samples.append((start + number * dt,) + tuple(
                    xyz[index] * sign * count_g for index, sign in mapping))
        if baseline is None and samples:
            resting = samples[:min(50, len(samples))]
            baseline = tuple(sum(row[i] for row in resting) / len(resting) for i in (1, 2, 3))
        return {"samples": samples, "dropped": dropped, "baseline_g": baseline}

    def _record(self, attempt):
        to_g = attempt.count_g
        if attempt.reason != "impact":
            attempt.noise_pos = max(attempt.noise_pos, attempt.pending_noise[0])
            attempt.noise_neg = max(attempt.noise_neg, attempt.pending_noise[1])
            attempt.noise_checked = max(attempt.noise_checked, attempt.pending_noise[2])
        onset = attempt.onset_batch is not None
        velocity = self._velocity(attempt)
        result = {
            "axis": attempt.axis,
            "pass": attempt.pass_no,
            "result": attempt.reason or "no_trigger",
            "threshold_g": round(attempt.limit_raw * to_g, 2),
            "direction": attempt.direction or attempt.auto_direction,
            "onset_g": round(attempt.onset_raw * to_g, 2) if onset else None,
            "impact_sign": (1 if attempt.onset_raw > 0 else -1 if attempt.onset_raw < 0 else None)
                           if onset else None,
            "impact_peak_g": round(attempt.impact_peak * to_g, 2) if onset else None,
            "noise_pos_g": round(attempt.noise_pos * to_g, 2),
            "noise_neg_g": round(attempt.noise_neg * to_g, 2),
            "noise_g": round(attempt.noise_checked * to_g, 2),
            "velocity_mm_s": None if velocity is None else round(velocity, 1),
            "moving": attempt.moving,
            "motion_seen": attempt.motion_seen or attempt.moving,
            "arm_time_s": attempt.arm_time,
            "onset_time_s": self.onset_time(attempt) if onset else None,
            "start_position_mm": attempt.start_position,
            "expected_travel_mm": attempt.expected_travel,
            "backend": "beacon_raw" if self.backend is None else self.backend.kind,
            "trigger_after_arm_s": round(self.onset_time(attempt) - attempt.arm_time, 4) if onset else None,
        }
        attempt.record = result
        self.last[attempt.axis] = result
        self.history.append(result)

    def get_status(self, eventtime):
        return {
            "threshold_g": dict(self.threshold_g),
            "impact_sign": dict(self.impact_sign),
            "homing_accel": self.homing_accel,
            "back_off_dist": self.back_off_dist,
            "homing": self.homing_active,
            "last": {axis: dict(r) for axis, r in self.last.items()},
            "sensor": self.sensor_name,
            "backend": {"type": "beacon_raw", "sensor": self.sensor_name,
                        "latency_guaranteed": False} if self.backend is None else self.backend.get_status(),
            "workflows": self.workflows.get_status(eventtime),
        }

    def cmd_STATUS(self, gcmd):
        kind = "beacon_raw" if self.backend is None else self.backend.kind
        gcmd.respond_info("accel_homing: sensor %s, backend %s, X %.2f g, Y %.2f g"
                          % (self.sensor_name, kind, self.threshold_g["x"], self.threshold_g["y"]))
        if self.backend is not None:
            gcmd.respond_info("accel_homing: requested report interval %.4f s, sample-age limit"
                              " %.4f s, observed rate %s Hz, maximum homing speed %.1f mm/s;"
                              " total stop latency is not guaranteed"
                              % (self.backend.interval, self.backend.max_age,
                                 self.backend.observed_rate, self.backend.max_speed))
        if not self.history:
            gcmd.respond_info("beacon_accel_homing: no homes recorded since restart")
            return
        gcmd.respond_info("\n".join(describe(r) for r in self.history))

    def cmd_SET(self, gcmd):
        self.workflows.check_settings_change()
        axis = gcmd.get("AXIS").lower()
        if axis not in AXES:
            raise gcmd.error("AXIS must be X or Y")
        threshold = gcmd.get_float("THRESHOLD", None, minval=3.0, maxval=15.0)
        sign = gcmd.get_int("IMPACT_SIGN", None, minval=-1, maxval=1)
        if threshold is not None:
            if not math.isfinite(threshold):
                raise gcmd.error("THRESHOLD must be finite")
            full_scale = self._full_scale_g(self.scale) if self.helper is not None else None
            if full_scale is not None and threshold > 0.8 * full_scale:
                raise gcmd.error("THRESHOLD must stay below %.1f g, the accelerometer"
                                 " range is %.0f g" % (0.8 * full_scale, full_scale))
            self.threshold_g[axis] = threshold
        if sign is not None:
            self.impact_sign[axis] = sign
        gcmd.respond_info("beacon_accel_homing %s: threshold %.1f g, impact_sign %d (until restart)"
                          % (axis.upper(), self.threshold_g[axis], self.impact_sign[axis]))


def load_config(config):
    return BeaconAccelHoming(config)
