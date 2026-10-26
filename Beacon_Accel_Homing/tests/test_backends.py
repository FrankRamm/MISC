"""Offline contracts for configured sensor streams; no printer connection."""
import types
import unittest
from unittest import mock

from harness import (CommandError, ConfigError, FakeConfig, FakeEndstop, FakePrinter,
                     FakeToolhead, batch, counts, make_beacon, make_homing, plugin,
                     stationary_stream)
from accel_homing_backends import select_sensor
from test_session import FakeRail, info, with_endstops

G = 9806.65
HIT = plugin.mcu.MCU_trsync.REASON_ENDSTOP_HIT
HOST = plugin.mcu.MCU_trsync.REASON_HOST_REQUEST


class GenericPreparationTest(unittest.TestCase):
    """Real rails/move callbacks with decoded sensor delivery and fake clocks."""

    def session(self, legacy=False, backlog=False, options=None):
        obj = generic(legacy=legacy, options=options, connect=False)
        obj.fake_printer.send_event("klippy:connect")
        obj.threshold_g["x"] = 2.0
        endstop = with_endstops(obj)["x"]
        obj.reactor.now = 10.0
        stationary_stream(obj)
        plain = obj.reactor.on_pause
        called = [False]
        if backlog:
            def flow():
                if not called[0]:
                    called[0] = True
                    obj.backend.helper.emit({"data": samples(9.9, x=-15)})
                else:
                    plain()
            obj.reactor.on_pause = flow
        obj._handle_rails_begin(None, [FakeRail(endstop, info(speed=50, retract=0))])
        return obj, endstop

    def begin(self, obj, endstop):
        obj._handle_move_begin(types.SimpleNamespace(get_mcu_endstops=lambda: [endstop]))

    def test_existing_helper_backlog_is_drained_before_gate_without_poisoning(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                obj, endstop = self.session(legacy=legacy, backlog=True)
                self.begin(obj, endstop)
                self.assertIsNone(obj.sensor_fault)
                self.assertIsNotNone(obj.preparation.baseline())
                self.assertGreater(obj.preparation.first, obj.preparation.cutoff)
                self.assertEqual(endstop._dispatch.started, [])

    def test_late_pre_cutoff_peak_and_pending_are_excluded_from_reference(self):
        obj, endstop = self.session()
        obj.backend.pending = samples(9.9, size=1, x=-15)
        plain = obj.reactor.on_pause
        def flow():
            if obj.preparation is not None:
                obj.backend.helper.emit({"data": samples(9.9, x=-15)})
            plain()
        obj.reactor.on_pause = flow
        self.begin(obj, endstop)
        self.assertAlmostEqual(obj.preparation.baseline()[0], 0.0)
        self.assertIsNone(obj.sensor_fault)
        self.assertEqual(endstop._dispatch.started, [])

    def test_fault_or_noisy_generic_preparation_aborts_before_any_dispatch(self):
        for mode in ("noise", "no_data", "stale", "future", "duplicate", "gap",
                     "saturated", "nonfinite", "errors", "overflows"):
            with self.subTest(mode=mode):
                obj, endstop = self.session()
                sequence = [0]
                last = []
                def flow():
                    sequence[0] += 1
                    if mode == "no_data":
                        return
                    start = obj.reactor.now - 0.001 - 15 / 3200.0
                    x = (3 if sequence[0] % 2 else -3) if mode == "noise" else 0
                    fields = {}
                    if mode == "stale":
                        obj.reactor.now += 0.1
                    elif mode == "future":
                        start += 0.020
                    elif mode == "duplicate" and last:
                        start = last[0]
                    elif mode == "gap" and last:
                        start += 0.020
                        obj.reactor.now += 0.020
                    elif mode == "saturated":
                        x = 16.0
                    elif mode == "nonfinite":
                        x = float("nan")
                    elif mode in ("errors", "overflows"):
                        fields[mode] = 1
                    last.append(start)
                    obj.backend.helper.emit({"data": samples(start, x=x), **fields})
                obj.reactor.on_pause = flow
                with self.assertRaises(CommandError):
                    self.begin(obj, endstop)
                self.assertEqual(endstop._dispatch.started, [])
                self.assertEqual(endstop.get_mcu().log, [])
                self.assertEqual(obj.toolhead.moves, [])
                self.assertFalse(obj.homing_active)
                self.assertFalse(obj.backend.active)
                self.assertEqual(obj.backend.helper.batch_interval, 0.1)
                diagnostic = obj.get_status(obj.reactor.now)["baseline"]
                self.assertEqual(diagnostic["phase"], "acquisition")
                self.assertEqual(diagnostic["result"], obj.sensor_fault or "acquisition_timeout")
                if mode == "stale":
                    self.assertGreater(diagnostic["oldest_batch_age_ms"], diagnostic["max_sample_age_ms"])
                elif mode == "future":
                    self.assertLess(diagnostic["newest_batch_age_ms"], -5.0)

    def test_rounded_3200hz_reference_spans_small_deque_and_print_clock_offset(self):
        obj, endstop = self.session(options={"baseline_batches": 10})
        obj.threshold_g["x"] = 0.5
        # Decoded ADXL rows have microsecond-rounded print times, even when
        # host monotonic time and synchronized print time use different epochs.
        obj.main_mcu.estimated_print_time = lambda eventtime: eventtime + 123456.0
        def flow():
            now = obj.main_mcu.estimated_print_time(obj.reactor.now)
            rows = [(round(t, 6), x, y, z) for t, x, y, z in
                    samples(now - 0.001 - 15 / 3200.0, x=0.02, y=-0.03)]
            obj.backend.helper.emit({"data": rows})
        obj.reactor.on_pause = flow
        self.begin(obj, endstop)
        reference = obj.preparation
        self.assertGreaterEqual(reference.count, 321)
        self.assertGreaterEqual(reference.last - reference.first, 0.1)
        self.assertEqual(len(obj.recent), 10)
        self.assertAlmostEqual(reference.baseline()[0], 20.0)
        self.assertIsNone(obj.sensor_fault)
        self.assertEqual(endstop._dispatch.started, [])

    def test_timeout_retains_best_quiet_candidate_and_observed_noise(self):
        obj, endstop = self.session()
        obj.threshold_g["x"] = 0.5
        def flow():
            now = obj.reactor.now
            cutoff = obj.preparation.cutoff
            rows = [(round(t, 6), (0.2 if int((t - cutoff) / 0.090) % 2 else 0) * G, y, z)
                    for t, x, y, z in samples(now - 0.001 - 15 / 3200.0)]
            obj.backend.helper.emit({"data": rows})
        obj.reactor.on_pause = flow
        with self.assertRaisesRegex(CommandError, "acquisition timeout") as caught:
            self.begin(obj, endstop)
        diagnostic = obj.get_status(obj.reactor.now)["baseline"]
        self.assertEqual(diagnostic["result"], "acquisition_timeout")
        self.assertEqual(diagnostic["phase"], "acquisition")
        self.assertGreaterEqual(diagnostic["best_quiet_span_ms"], 80.0)
        self.assertLess(diagnostic["best_quiet_span_ms"], 100.0)
        self.assertGreater(diagnostic["best_quiet_samples"], diagnostic["quiet_samples"])
        self.assertGreater(diagnostic["quiet_resets"], 0)
        self.assertAlmostEqual(diagnostic["observed_peak_to_peak_g"]["x"], 0.2)
        self.assertAlmostEqual(diagnostic["quiet_limit_g"], 0.1)
        self.assertIn("g", str(caught.exception))
        self.assertIn("ms", str(caught.exception))
        self.assertFalse(obj.homing_active)
        self.assertIsNone(obj.preparation)
        self.assertEqual(endstop._dispatch.started, [])

    def test_timeout_differentiates_absent_from_pre_cutoff_only_data(self):
        for mode in ("absent", "pre_cutoff"):
            with self.subTest(mode=mode):
                obj, endstop = self.session()
                def flow():
                    if mode == "pre_cutoff":
                        obj.backend.helper.emit({"data": samples(obj.preparation.cutoff - 0.010)})
                obj.reactor.on_pause = flow
                with self.assertRaisesRegex(CommandError, "acquisition timeout"):
                    self.begin(obj, endstop)
                diagnostic = obj.get_status(obj.reactor.now)["baseline"]
                self.assertEqual(diagnostic["accepted_samples"], 0)
                self.assertEqual(diagnostic["quiet_samples"], 0)
                self.assertIsNone(diagnostic["sample_age_ms"])
                self.assertEqual(diagnostic["best_quiet_span_ms"], 0.0)
                self.assertEqual(diagnostic["pre_cutoff_samples"] > 0, mode == "pre_cutoff")
                self.assertEqual(endstop._dispatch.started, [])

    def test_ready_reference_expiry_before_arm_is_reported_separately(self):
        obj, endstop = self.session()
        self.begin(obj, endstop)
        obj.reactor.now += 0.040
        with self.assertRaisesRegex(CommandError, "expired before arm"):
            endstop.home_start(obj.reactor.now + 0.010, 0, 0, 0)
        diagnostic = obj.get_status(obj.reactor.now)["baseline"]
        self.assertEqual(diagnostic["result"], "expired_before_arm")
        self.assertEqual(diagnostic["phase"], "before_arm")
        self.assertGreaterEqual(diagnostic["best_quiet_span_ms"], 100.0)
        self.assertGreater(diagnostic["sample_age_ms"], diagnostic["max_sample_age_ms"])
        self.assertFalse(obj.homing_active)
        self.assertIsNone(obj.preparation)
        self.assertEqual(endstop._dispatch.started, [])

    def test_first_single_post_arm_contact_survives_generic_batch_boundary(self):
        obj, endstop = self.session()
        self.begin(obj, endstop)
        when = obj.backend.last_sample_time + 16 / 3200.0
        endstop.home_start(when, 0, 0, 0)
        rows = samples(when - 15 / 3200.0, x=-15)
        rows[-1] = (when, -3 * G, 0, G)
        feed(obj, rows)
        self.assertIsNone(obj.attempt.reason)
        feed(obj, samples(when + 1 / 3200.0, size=1))
        self.assertEqual(obj.attempt.reason, "impact")
        self.assertAlmostEqual(obj.onset_time(obj.attempt), when, places=6)
        self.assertEqual(endstop.get_mcu().log, [("mcu", [7, HIT])])

    def test_pending_sample_age_is_rechecked_before_contact(self):
        obj, endstop = self.session()
        self.begin(obj, endstop)
        when = obj.backend.last_sample_time + 1 / 3200.0
        endstop.home_start(when, 0, 0, 0)
        feed(obj, samples(when, size=1, x=-3))
        self.assertIsNone(obj.attempt.reason)
        feed(obj, samples(when + 1 / 3200.0, size=2), age=0.0197)
        self.assertEqual(obj.attempt.reason, "stale_data")
        self.assertEqual(endstop.get_mcu().log, [("mcu", [7, HOST])])

    def test_other_clients_survive_success_and_gate_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                obj, endstop = self.session()
                seen = []
                other = lambda message: seen.append(message) or True
                obj.backend.helper.add_client(other)
                if failure:
                    obj.reactor.on_pause = lambda: obj.backend.helper.emit({"data": []})
                    with self.assertRaises(CommandError):
                        self.begin(obj, endstop)
                else:
                    self.begin(obj, endstop)
                    obj._end_session()
                obj.backend.helper.emit({"data": samples(obj.reactor.now)})
                self.assertIn(other, obj.backend.helper.client_cbs)
                self.assertTrue(seen)
                self.assertEqual(obj.backend.helper.batch_interval, 0.1)


class BatchHelper:
    """The real bulk_sensor callback/False-unsubscribe contract."""
    def __init__(self):
        self.batch_interval = 0.1
        self.batch_timer = object()
        self.client_cbs = []

    def add_client(self, callback):
        self.client_cbs.append(callback)

    def emit(self, message):
        for callback in list(self.client_cbs):
            if not callback(message):
                self.client_cbs.remove(callback)


class LegacyHelper:
    """Klipper v0.12's web request / connection interface, not add_client(cb)."""
    def __init__(self):
        self.update_interval = 0.1
        self.update_timer = object()
        self.clients = {}

    def add_client(self, request):
        connection = request.get_client_connection()
        self.clients[connection] = request.get_dict("response_template", {})

    def emit(self, message):
        for connection in list(self.clients):
            if connection.is_closed():
                del self.clients[connection]
            else:
                connection.send({"params": message})


def generic(name="adxl345 toolhead", options=None, rate=3200, legacy=False, connect=True):
    helper = LegacyHelper() if legacy else BatchHelper()
    sensor = types.SimpleNamespace(data_rate=rate, axes_map=[(1, -123), (0, 123), (2, 123)])
    setattr(sensor, "api_dump" if legacy else "batch_bulk", helper)
    printer = FakePrinter(FakeToolhead(), make_beacon())
    printer.objects[name] = sensor
    cfg = FakeConfig(printer, {"accel_chip": name, **(options or {})},
                     sections=(name,), name="accel_homing")
    obj = plugin.BeaconAccelHoming(cfg)
    obj.fake_printer = printer
    if connect:
        printer.send_event("klippy:connect")
        obj.homing_active = True
        obj._start_stream()
    return obj


def samples(start, size=16, hz=3200.0, x=0.0, y=0.0, z=1.0):
    return [(start + n / hz, x * G, y * G, z * G) for n in range(size)]


def feed(obj, rows, age=0.001, **fields):
    obj.fake_printer.reactor.now = rows[-1][0] + age
    obj.backend.helper.emit({"data": rows, **fields})


def baseline(obj, start=10.0):
    for n in range(12):
        feed(obj, samples(start + n * 0.005))


def arm(obj, axis="x", when=10.060, deadline=20.0):
    endstop = FakeEndstop(axis)
    clock = obj.backend.print_time_to_clock
    attempt = plugin.Attempt(endstop, 1, when, clock(when) & 0xFFFFFFFF,
                             deadline, clock(deadline) & 0xFFFFFFFF,
                             plugin.AXES[axis], 1, obj.impact_sign[axis],
                             obj.threshold_g[axis] / obj.backend.count_g, obj.backend.count_g)
    obj.arm(attempt)
    return attempt, endstop.trigger_log


class SensorSelectionTest(unittest.TestCase):
    def config(self, sections, options=None, name="accel_homing"):
        return FakeConfig(None, options or {}, sections=sections, name=name)

    def test_named_standard_sensor_is_selected_unambiguously(self):
        self.assertEqual(select_sensor(self.config(("adxl345 toolhead",))), "adxl345 toolhead")

    def test_ambiguous_auto_requires_explicit_full_section(self):
        cfg = self.config(("beacon", "adxl345 toolhead"))
        with self.assertRaisesRegex(ConfigError, "accel_chip"):
            select_sensor(cfg)
        cfg.options["accel_chip"] = "adxl345 toolhead"
        self.assertEqual(select_sensor(cfg), "adxl345 toolhead")

    def test_legacy_beacon_default_is_preserved_with_other_sensors(self):
        self.assertEqual(select_sensor(self.config(("beacon", "adxl345"),
                                                  name="beacon_accel_homing")), "beacon")

    def test_probe_only_cartographer_is_not_an_acceleration_stream(self):
        with self.assertRaisesRegex(ConfigError, "found none"):
            select_sensor(self.config(("cartographer", "scanner")))

    def test_cartographer_standard_adxl_section_is_supported(self):
        self.assertEqual(select_sensor(self.config(("cartographer", "adxl345"))), "adxl345")

    def test_missing_configured_sensor_rejected(self):
        with self.assertRaisesRegex(ConfigError, "no \\[adxl345 missing\\]"):
            select_sensor(self.config(("adxl345",), {"accel_chip": "adxl345 missing"}))

    def test_duplicate_old_and_new_sections_rejected(self):
        printer = FakePrinter(FakeToolhead(), make_beacon())
        with self.assertRaisesRegex(ConfigError, "only one"):
            plugin.BeaconAccelHoming(FakeConfig(printer, {}, sections=("beacon", "accel_homing")))

    def test_unknown_stream_driver_needs_actual_range(self):
        with self.assertRaisesRegex(ConfigError, "sensor_range"):
            generic("custom_accel toolhead")
        obj = generic("custom_accel toolhead", {"sensor_range": 16})
        self.assertEqual(obj.backend.full_scale, 16)


class GenericTriggerTest(unittest.TestCase):
    def test_normalized_mm_s2_triggers_with_independent_thresholds(self):
        for axis, amplitude, expected in (("x", -7, HIT), ("y", -7, None), ("y", -12, HIT)):
            obj = generic(options={"x_threshold": 6, "y_threshold": 11,
                                   "x_impact_sign": -1, "y_impact_sign": -1})
            baseline(obj)
            attempt, trigger = arm(obj, axis)
            rows = samples(10.060)
            row = list(rows[7])
            row[plugin.AXES[axis] + 1] = amplitude * G
            rows[7] = tuple(row)
            feed(obj, rows)
            self.assertEqual(trigger.messages, [] if expected is None else [[expected]])
            if expected:
                self.assertAlmostEqual(obj.onset_time(attempt), rows[7][0], places=6)

    def test_already_mapped_driver_data_is_not_mapped_twice(self):
        obj = generic(options={"x_impact_sign": -1})
        baseline(obj)
        _attempt, trigger = arm(obj)
        feed(obj, samples(10.060, x=12.0))
        self.assertEqual(trigger.messages, [])
        feed(obj, samples(10.065, x=-12.0))
        self.assertEqual(trigger.messages, [[HIT]])

    def test_biased_stationary_baseline_is_subtracted(self):
        obj = generic(options={"x_threshold": 5, "x_impact_sign": -1})
        for n in range(12):
            feed(obj, samples(10.0 + n * 0.005, x=-1.0))
        attempt, trigger = arm(obj)
        feed(obj, samples(10.060, x=-5.9))
        self.assertEqual(trigger.messages, [])
        feed(obj, samples(10.065, x=-6.1))
        self.assertEqual(trigger.messages, [[HIT]])
        self.assertAlmostEqual(attempt.baseline, -1000.0)

    def test_batch_straddling_arm_keeps_only_post_arm_trigger(self):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj, when=10.0625)
        rows = samples(10.060)
        rows[1] = (rows[1][0], -15 * G, 0, G)  # Before arming: never a hit.
        rows[12] = (rows[12][0], -12 * G, 0, G)
        feed(obj, rows)
        self.assertEqual(trigger.messages, [[HIT]])
        self.assertAlmostEqual(obj.onset_time(attempt), rows[12][0], places=6)

    def test_clock_wrap_and_long_printer_uptime(self):
        obj = generic()
        base = 4294.94
        baseline(obj, base)
        attempt, trigger = arm(obj, when=base + 0.060, deadline=base + 10.0)
        feed(obj, samples(base + 0.060, x=-12))
        self.assertEqual(trigger.messages, [[HIT]])
        self.assertAlmostEqual(obj.onset_time(attempt), base + 0.060, places=6)

    def test_generic_never_infers_pressed_from_absent_motion(self):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj)
        attempt.steppers = [types.SimpleNamespace(
            get_past_mcu_position=lambda t: t * 5000, get_step_dist=lambda: 0.01)]
        for start in (10.060, 10.065, 10.070):
            feed(obj, samples(start))
            obj._check_pressed(attempt)
        self.assertEqual(trigger.messages, [])
        self.assertIsNone(obj._motion_state(attempt))

    def test_frozen_stream_fails_instead_of_homing_at_start(self):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj)
        attempt.steppers = [types.SimpleNamespace(
            get_past_mcu_position=lambda t: t * 5000, get_step_dist=lambda: 0.01)]
        for n in range(7):
            feed(obj, samples(10.060 + n * 0.005))
        self.assertEqual(attempt.reason, "frozen")
        self.assertEqual(trigger.messages, [[HOST]])


class StreamIntegrityTest(unittest.TestCase):
    def check_fault(self, rows, reason, age=0.001, **fields):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj)
        feed(obj, rows, age=age, **fields)
        self.assertEqual(attempt.reason, reason)
        self.assertEqual(trigger.messages, [[HOST]])
        with self.assertRaises(CommandError):
            obj.finish(attempt.endstop, attempt, HOST)

    def test_stale_impact_never_sets_origin(self):
        self.check_fault(samples(10.060, x=-15), "stale_data", age=0.050)

    def test_oldest_threshold_sample_is_checked_not_only_latest(self):
        self.check_fault(samples(10.060, size=160, x=-15), "stale_data")

    def test_future_timestamp_never_sets_origin(self):
        self.check_fault(samples(10.060, x=-15), "stale_data", age=-0.050)

    def test_replayed_timestamps_fail_closed(self):
        self.check_fault(samples(10.055, x=-15), "invalid_data")

    def test_missing_samples_fail_closed(self):
        self.check_fault(samples(10.070, x=-15), "sample_gap")

    def test_nonfinite_and_wrong_shape_are_rejected(self):
        for row in ((10.060, float("nan"), 0, G), (10.060, 0, 0)):
            self.check_fault([row] + samples(10.0603125, size=15), "invalid_data")

    def test_sensor_errors_and_overflows_are_rejected(self):
        self.check_fault(samples(10.060, x=-15), "data_error", errors=1)
        self.check_fault(samples(10.060, x=-15), "data_error", overflows=1)

    def test_unconfigured_slow_rate_is_rejected_from_timestamps(self):
        obj = generic(rate=None)
        # Metadata absent, timestamp validation must still reject 800 Hz.
        obj.backend.rate = None
        obj.fake_printer.reactor.now = 10.01
        obj.backend.helper.emit({"data": samples(10.0, size=4, hz=800)})
        self.assertEqual(obj.sensor_fault, "sample_rate")

    def test_rate_and_range_are_checked_at_startup(self):
        with self.assertRaisesRegex(ConfigError, "below min_sample_rate"):
            generic(rate=800)
        with self.assertRaisesRegex(ConfigError, "below 6.4 g"):
            generic("mpu9250 toolhead", {"threshold": 8})
        obj = generic("mpu9250 toolhead")
        self.assertEqual(obj.threshold_g, {"x": 4.0, "y": 4.0})

    def test_watchdog_checks_sensor_time_without_new_callback(self):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj)
        feed(obj, samples(10.060))
        attempt.last_data = 10.1  # Receipt is fresh but measurement is old.
        obj._watchdog(10.1)
        self.assertEqual(attempt.reason, "stale_data")
        self.assertEqual(trigger.messages, [[HOST]])

    def test_empty_batches_do_not_refresh_last_data(self):
        obj = generic()
        baseline(obj)
        attempt, trigger = arm(obj)
        last_data = attempt.last_data
        obj.backend.helper.emit({"data": []})
        self.assertEqual(attempt.last_data, last_data)
        obj._watchdog(10.1)
        self.assertEqual(trigger.messages, [[HOST]])


class SubscriptionAndPolicyTest(unittest.TestCase):
    def test_shared_clients_survive_and_interval_is_restored(self):
        obj = generic(connect=False)
        obj.fake_printer.send_event("klippy:connect")
        helper = obj.backend.helper
        seen = []
        other = lambda message: seen.append(message) or True
        helper.add_client(other)
        obj.homing_active = True
        obj._start_stream()
        self.assertEqual(helper.batch_interval, 0.005)
        self.assertEqual(obj.reactor.timers[helper.batch_timer], 0.005)
        obj._stop_stream()
        self.assertEqual(helper.batch_interval, 0.1)
        helper.emit({"data": samples(10)})
        self.assertEqual(helper.client_cbs, [other])
        self.assertEqual(len(seen), 1)

    def test_later_external_interval_change_is_preserved(self):
        obj = generic()
        obj.backend.helper.batch_interval = 0.003
        obj._stop_stream()
        self.assertEqual(obj.backend.helper.batch_interval, 0.003)

    def test_prior_session_callback_cannot_process_new_attempt(self):
        obj = generic()
        old = obj.backend.helper.client_cbs[0]
        obj._stop_stream()
        obj._start_stream()
        self.assertFalse(old({"data": samples(10, x=-15)}))
        self.assertIsNone(obj.last_batch)

    def test_legacy_web_request_contract_and_unsubscribe(self):
        obj = generic(legacy=True)
        baseline(obj)
        attempt, trigger = arm(obj)
        feed(obj, samples(10.060, x=-12))
        self.assertEqual(trigger.messages, [[HIT]])
        self.assertEqual(obj.backend.helper.update_interval, 0.005)
        obj._stop_stream()
        obj.backend.helper.emit({"data": []})
        self.assertFalse(obj.backend.helper.clients)
        self.assertEqual(obj.backend.helper.update_interval, 0.1)

    def test_excessive_rail_speed_rejected_before_any_motion(self):
        obj = generic()
        rail = FakeRail(with_endstops(obj)["x"], info(speed=100, retract=0))
        with self.assertRaisesRegex(CommandError, "exceeds 50.0"):
            obj._handle_rails_begin(None, [rail])
        self.assertEqual(obj.toolhead.moves, [])
        self.assertEqual(obj.toolhead.accel_log, [])

    def test_speed_policy_tracks_report_travel_and_sample_age(self):
        obj = generic(options={"max_report_travel": 0.5})
        self.assertEqual(obj.backend.max_speed, 25)
        with self.assertRaises(ConfigError):
            obj.backend.validate_rail(info(speed=30, retract=0), ConfigError)

    def test_standard_command_and_pin_aliases_exist_once(self):
        obj = generic()
        self.assertIs(obj.fake_printer.chips["beacon_accel"], obj.fake_printer.chips["accel_homing"])
        self.assertIn("ACCEL_HOMING_STATUS", obj.fake_printer.commands)
        self.assertIn("BEACON_ACCEL_HOMING_STATUS", obj.fake_printer.commands)


class CaptureTest(unittest.TestCase):
    def test_beacon_capture_uses_mapping_and_retains_baseline(self):
        obj = make_homing(axes_map=((1, -1), (0, 1), (2, 1)))
        obj.start_capture()
        obj._inspect(batch(0, [(counts(0.5), counts(-0.25), counts(1))] * 8))
        capture = obj.stop_capture()
        self.assertEqual(capture["dropped"], 0)
        self.assertAlmostEqual(capture["samples"][0][1], 0.25)
        self.assertAlmostEqual(capture["samples"][0][2], 0.5)
        self.assertAlmostEqual(capture["baseline_g"][2], 1.0)

    def test_capture_is_bounded_and_reports_lost_samples(self):
        obj = make_homing()
        obj.capture_limit = 8
        obj.start_capture()
        for start in (0, 8, 16):
            obj._inspect(batch(start, [(0, 0, counts(1))] * 8))
        capture = obj.stop_capture()
        self.assertEqual(len(capture["samples"]), 8)
        self.assertEqual(capture["dropped"], 16)

    def test_generic_capture_records_g_and_timestamp_without_raw_clip_sentinel(self):
        obj = generic()
        obj.start_capture()
        baseline(obj)
        arm(obj)
        feed(obj, samples(10.060, x=-12))
        capture = obj.stop_capture()
        self.assertTrue(any(abs(row[1] + 12) < 0.0001 for row in capture["samples"]))
        self.assertEqual(capture["baseline_g"], (0, 0, 1))
        self.assertEqual(capture["dropped"], 0)

    def test_capture_failure_cannot_delay_or_prevent_trigger(self):
        obj = make_homing()
        from harness import arm_axis
        for start in range(0, 96, 8):
            obj._inspect(batch(start, [(0, 0, counts(1))] * 8))
        attempt, trigger = arm_axis(obj, "x", 200, 1000)
        obj.start_capture()
        def fail_after_trigger(params):
            self.assertEqual(trigger.messages, [[HIT]])
            raise ValueError("capture failure")
        obj._capture_batch = fail_after_trigger
        with self.assertLogs(level="ERROR"):
            obj._batch_hook(batch(200, [(counts(-12), 0, counts(1))] * 8))
        self.assertEqual(trigger.messages, [[HIT]])


class InvalidNumericOptionsTest(unittest.TestCase):
    def test_nonfinite_core_controls_are_rejected(self):
        for option in ("threshold", "x_threshold", "y_threshold", "homing_accel",
                       "back_off_dist", "baseline_time", "stream_timeout",
                       "travel_margin", "watchdog_margin"):
            with self.subTest(option=option):
                with self.assertRaises(ConfigError):
                    make_homing(options={option: float("nan")})

    def test_nonfinite_backend_controls_are_rejected(self):
        for option in ("stream_interval", "max_sample_age", "min_sample_rate",
                       "max_report_travel", "sensor_range"):
            with self.subTest(option=option):
                with self.assertRaises(ConfigError):
                    generic(options={option: float("nan")})
        with self.assertRaises(ConfigError):
            generic(rate=float("nan"))

    def test_runtime_set_rejects_nan_and_keeps_threshold(self):
        from test_trigger import FakeGcmd
        obj = generic()
        with self.assertRaisesRegex(FakeGcmd.Error, "finite"):
            obj.cmd_SET(FakeGcmd(AXIS="X", THRESHOLD=float("nan")))
        self.assertEqual(obj.threshold_g["x"], 8.0)

    def test_missing_trigger_dispatch_has_actionable_startup_error(self):
        with mock.patch.object(plugin.mcu, "TriggerDispatch", None):
            with self.assertRaisesRegex(ConfigError, "TriggerDispatch"):
                generic()

    def test_post_disarm_motion_assessment_updates_record(self):
        obj = generic()
        baseline(obj)
        attempt, _trigger = arm(obj)
        # Real pre-impact integration shows travel, but the watchdog has not
        # inspected the latest batch yet when dispatch finishes.
        feed(obj, samples(10.060, x=0.5))
        feed(obj, samples(10.065, x=-12))
        attempt.steppers = [types.SimpleNamespace(
            get_past_mcu_position=lambda t: t * 5000, get_step_dist=lambda: 0.01)]
        attempt.sums = [100000.0, 0.0, 0.0]
        obj.disarm()
        self.assertFalse(attempt.record["moving"])
        obj.trigger_time(attempt)
        self.assertTrue(attempt.record["moving"])
        self.assertTrue(attempt.record["motion_seen"])


if __name__ == "__main__":
    unittest.main()
