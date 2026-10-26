"""Synthetic motion/step histories for reviewer P1/P2; no printer access."""
import json
import math
import types
import unittest

from harness import (CommandError, ConfigError, FakeEndstop, acquire_reference, batch,
                     counts, make_homing, plugin)
from test_backends import baseline, feed, generic
from test_session import FakeRail, info, with_endstops
from test_single_touch import RampStepper
from test_workflows import Rig

HIT = plugin.mcu.MCU_trsync.REASON_ENDSTOP_HIT
HOST = plugin.mcu.MCU_trsync.REASON_HOST_REQUEST
G = 9806.65


class SignedRamp(RampStepper):
    def __init__(self, start, accel, speed, direction):
        super().__init__(start, accel, speed)
        self.direction = direction

    def get_past_mcu_position(self, stamp):
        return self.direction * super().get_past_mcu_position(stamp)


def prepared_generic(speed=50.0, velocity=500.0, accel=5000.0, direction=1,
                     options=None, start=10.060):
    obj = generic(options={"homing_accel": accel, **(options or {})})
    obj.toolhead.get_max_velocity = lambda: (velocity, accel)
    baseline(obj, start - 0.060)
    endstop = FakeEndstop("x")
    stepper = SignedRamp(start, accel, min(speed, velocity), direction)
    endstop.get_steppers = lambda: [stepper]
    obj.session_axes["x"] = FakeRail(endstop, info(speed=speed, retract=0, second=speed))
    acquire_reference(obj, endstop, start)
    attempt = obj.prepare(endstop, start)
    obj.arm(attempt)
    return obj, attempt, endstop.trigger_log


def prepared_fast_generic(direction=1, start=10.060):
    """An explicitly admitted 75 mm/s plan with synthetic fresh delivery."""
    speed, accel, age, rate = 75.0, 5360.0, 0.0001, 3200.0
    obj = generic(options={"homing_accel": 0.0, "max_sample_age": 0.005,
                           "stream_interval": 0.001, "max_report_travel": 1.0,
                           "max_startup_travel": 3.0})
    obj.toolhead.get_max_velocity = lambda: (500.0, accel)
    endstop = FakeEndstop("x")
    endstop.get_steppers = lambda: [SignedRamp(start, accel, speed, direction)]
    obj.session_axes["x"] = FakeRail(endstop, info(speed=speed, retract=0, second=speed))
    obj.reactor.now = start - obj.baseline_time - 0.020

    def stationary():
        now = obj.reactor.monotonic()
        # All 16 uniformly spaced rows are younger than the explicit 5 ms
        # policy; this models the fixture's delivery, not real-device latency.
        first = now - age - 15.0 / rate
        feed(obj, [(first + number / rate, 0.0, 0.0, G) for number in range(16)], age=age)

    obj.reactor.on_pause = stationary
    obj._prepare_reference()
    while obj.reactor.monotonic() + 0.005 <= start + 0.000000001:
        obj.reactor.pause(obj.reactor.monotonic() + 0.005)
    attempt = obj.prepare(endstop, start)
    obj.arm(attempt)
    return obj, attempt, endstop.trigger_log


def stream_ramp(obj, attempt, duration=0.150, direction=1, parked=False,
                response_lag=0.0, age=0.001, size=16):
    dt = 1.0 / 3200.0
    number = 0
    while number * dt < duration and not attempt.done:
        rows = []
        for index in range(size):
            offset = (number + index) * dt
            acceleration = 0.0
            if not parked and response_lag <= offset < response_lag + (
                    attempt.effective_speed / attempt.effective_accel):
                acceleration = direction * attempt.effective_accel
            # Balanced, fresh, varying noise avoids the separate frozen check.
            noise = 0.0002 * G * (1 if (number + index) % 2 else -1)
            rows.append((attempt.arm_time + offset, acceleration + noise, 0.0, G))
        feed(obj, rows, age=age)
        obj._watchdog(obj.reactor.monotonic())
        number += size
    return obj.reactor.monotonic()


class GenericNoMotionPolicyTests(unittest.TestCase):
    def test_parked_stop_with_varying_noise_fails_first_and_repeated_home(self):
        obj, attempt, trigger = prepared_generic()
        for pass_number in (1, 2):
            if pass_number == 2:
                # Reuse the module/endstop with a new ordinary G28 session;
                # a previous failure must never turn the next home into a hit.
                rail = obj.session_axes["x"]
                obj._end_session()
                obj.homing_active = True
                obj.session_axes["x"] = rail
                obj.passes = 0
                obj.recent.clear()
                obj._start_stream()
                baseline(obj, 11.0)
                attempt.endstop.get_steppers = lambda: [SignedRamp(11.060, 5000.0, 50.0, 1)]
                acquire_reference(obj, attempt.endstop, 11.060)
                attempt = obj.prepare(attempt.endstop, 11.060)
                obj.arm(attempt)
                trigger.messages.clear()
            stopped = stream_ramp(obj, attempt, parked=True)
            self.assertTrue(attempt.varied)
            self.assertEqual(attempt.reason, "no_motion")
            self.assertEqual(trigger.messages, [[HOST]])
            self.assertLess(stopped - attempt.arm_time, 0.065)
            self.assertLess(stopped, attempt.deadline - 6.0)
            obj.disarm()
            self.assertFalse(attempt.record["motion_seen"])
            with self.assertRaisesRegex(CommandError, "motion was not verified"):
                obj.finish(attempt.endstop, attempt, HOST)

    def test_positive_negative_and_delayed_real_ramps_are_not_false_stopped(self):
        for speed, accel in ((50.0, 5000.0), (20.0, 100.0), (30.0, 5000.0)):
            for direction in (-1, 1):
                with self.subTest(speed=speed, accel=accel, direction=direction):
                    obj, attempt, trigger = prepared_generic(speed=speed, accel=accel,
                                                              direction=direction)
                    stream_ramp(obj, attempt, duration=0.300, direction=direction,
                                response_lag=0.010, age=0.014)
                    self.assertIsNone(attempt.reason)
                    self.assertEqual(trigger.messages, [])
                    self.assertTrue(attempt.motion_seen)

    def test_admitted_75_positive_and_negative_ramps_prove_motion(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                obj, attempt, trigger = prepared_fast_generic(direction)
                stream_ramp(obj, attempt, direction=direction, age=0.0001)
                self.assertEqual(attempt.effective_speed, 75.0)
                self.assertEqual(attempt.effective_accel, 5360.0)
                self.assertTrue(attempt.motion_seen)
                self.assertIsNone(attempt.reason)
                self.assertEqual(trigger.messages, [])
                self.assertIsNone(obj.sensor_fault)

    def test_admitted_75_parked_noisy_traces_still_stop_within_request_budget(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                obj, attempt, trigger = prepared_fast_generic(direction)
                stopped = stream_ramp(obj, attempt, direction=direction, parked=True, age=0.0001)
                timeout = obj.backend.motion_startup_timeout(75.0, 5360.0)
                self.assertTrue(attempt.varied)
                self.assertFalse(attempt.motion_seen)
                self.assertEqual(attempt.reason, "no_motion")
                self.assertEqual(trigger.messages, [[HOST]])
                self.assertLessEqual(stopped - attempt.arm_time, timeout + 0.003)
                commanded_travel = 75.0 * (stopped - attempt.arm_time) - 0.5 * 75.0 ** 2 / 5360.0
                self.assertLessEqual(commanded_travel, 3.0)
                self.assertIsNone(obj.sensor_fault)

    def test_one_mm_s_uses_full_proof_history_with_19ms_sample_age(self):
        for direction in (-1, 1):
            obj, attempt, trigger = prepared_generic(
                speed=1.0, accel=1000.0, direction=direction, options={"stream_interval": 0.001})
            stream_ramp(obj, attempt, duration=0.150, direction=direction,
                        age=0.019, size=3)
            self.assertIsNone(attempt.reason)
            self.assertEqual(trigger.messages, [])
            self.assertTrue(attempt.motion_seen)
            self.assertAlmostEqual(obj.backend.motion_startup_timeout(1.0, 1000.0), 0.086)

    def test_effective_velocity_and_lower_policy_cap_qualify_motion(self):
        for speed, velocity, options, expected in (
                (50.0, 20.0, {}, 20.0), (25.0, 500.0, {"max_report_travel": 0.5}, 25.0)):
            obj, attempt, trigger = prepared_generic(speed=speed, velocity=velocity, options=options)
            self.assertEqual(attempt.effective_speed, expected)
            obj.backend.validate_rail(obj.session_axes["x"].get_homing_info(), CommandError)
            stream_ramp(obj, attempt)
            self.assertEqual(trigger.messages, [])
            self.assertTrue(attempt.motion_seen)

    def test_low_acceleration_rejected_before_enable_or_backoff(self):
        obj = generic(options={"homing_accel": 100.0, "max_startup_travel": 3.0})
        rail = FakeRail(FakeEndstop("x"), info(speed=50, retract=0))
        obj.endstops["x"] = rail.endstop
        calls = []
        obj._limit_accel = lambda: calls.append("limit")
        obj._enable_motors = lambda axis: calls.append("enable")
        obj._back_off_others = lambda axis: calls.append("backoff")
        with self.assertRaisesRegex(CommandError, "3.0 mm policy"):
            obj._handle_rails_begin(None, [rail])
        self.assertEqual(calls, [])
        self.assertEqual(obj.toolhead.moves, [])

    def test_prepare_rechecks_budget_before_dispatch(self):
        obj, attempt, _trigger = prepared_generic(options={"max_startup_travel": 3.0})
        obj.disarm()
        obj.toolhead.get_max_velocity = lambda: (500.0, 100.0)
        with self.assertRaisesRegex(CommandError, "host stop request"):
            obj.prepare(attempt.endstop, 11.0)

    def test_default_deadline_and_nominal_command_request_budget_are_explicit(self):
        obj, _attempt, _trigger = prepared_generic()
        status = obj.backend.get_status()
        self.assertAlmostEqual(status["startup_motion_timeout_s"], 0.055)
        self.assertAlmostEqual(status["startup_motion_request_travel_mm"], 2.65)
        self.assertIsNone(status["startup_motion_request_limit_mm"])
        self.assertFalse(status["latency_guaranteed"])


class MotionPolicyPreflightTests(unittest.TestCase):
    def machine(self, speeds=(50.0,), options=None, accel=20000.0, connect=True):
        obj = generic(options=options, connect=False)
        axes = "xy"[:len(speeds)]
        endstops = with_endstops(obj, axes)
        rails = [FakeRail(endstops[axis], info(speed=speed, retract=0), "stepper_" + axis)
                 for axis, speed in zip(axes, speeds)]
        toolhead = obj.fake_printer.objects["toolhead"]
        toolhead.accel = accel
        toolhead.kin = types.SimpleNamespace(rails=rails)
        if connect:
            obj.fake_printer.send_event("klippy:connect")
        return obj, toolhead, rails

    def assert_no_effects(self, obj, toolhead):
        self.assertFalse(obj.homing_active)
        self.assertFalse(obj.stream_on)
        self.assertFalse(obj.backend.active)
        self.assertEqual(toolhead.moves, [])
        self.assertEqual(toolhead.dwells, [])
        self.assertEqual(toolhead.accel_log, [])
        self.assertIsNone(obj.saved_accel)
        self.assertEqual(obj.reactor.monotonic(), 0.0)
        self.assertEqual(obj.fake_printer.scripts, [])
        self.assertTrue(all(not endstop._dispatch.started for endstop in obj.endstops.values()))

    def test_static_inadmissible_plan_is_rejected_at_connect_without_motion(self):
        obj, toolhead, _rails = self.machine(accel=1500.0, options={"max_startup_travel": 3.0}, connect=False)
        with self.assertRaisesRegex(ConfigError, "3.23 mm"):
            obj.fake_printer.send_event("klippy:connect")
        self.assert_no_effects(obj, toolhead)

    def test_runtime_preflight_rejects_lower_live_accel_before_any_effect(self):
        obj, toolhead, _rails = self.machine(options={"max_startup_travel": 3.0})
        toolhead.accel = 1500.0
        with self.assertRaisesRegex(CommandError, "effective homing speed 50 mm/s and acceleration 1500"):
            obj.preflight_motion_policy("x")
        self.assert_no_effects(obj, toolhead)
        self.assertEqual(toolhead.accel, 1500.0)

    def test_preflight_uses_live_velocity_and_does_not_change_it(self):
        obj, toolhead, _rails = self.machine()
        toolhead.get_max_velocity = lambda: (20.0, 1500.0)
        obj.preflight_motion_policy("x")
        self.assertEqual(toolhead.get_max_velocity(), (20.0, 1500.0))
        self.assert_no_effects(obj, toolhead)

    def test_configured_homing_accel_cap_and_zero_are_respected(self):
        for cap, live, expected in ((1500.0, 20000.0, 1500), (0.0, 1500.0, 1500)):
            with self.subTest(cap=cap):
                obj, toolhead, rails = self.machine(speeds=(30.0,), options={"homing_accel": cap, "max_startup_travel": 3.0})
                toolhead.accel = live
                rails[0].info = info(speed=50.0, retract=0)
                with self.assertRaisesRegex(CommandError, "acceleration %s mm/s" % expected):
                    obj.preflight_motion_policy("x")
                self.assert_no_effects(obj, toolhead)
                self.assertEqual(toolhead.accel, live)

    def test_all_selected_xy_plans_are_checked_and_z_is_ignored(self):
        obj, toolhead, rails = self.machine(speeds=(30.0, 30.0), options={"max_startup_travel": 3.0})
        toolhead.accel = 1500.0
        rails[1].info = info(speed=50.0, retract=0)
        obj.preflight_motion_policy("xz")
        with self.assertRaisesRegex(CommandError, "3.23 mm"):
            obj.preflight_motion_policy("xy")
        self.assert_no_effects(obj, toolhead)

    def test_runtime_75_preserves_speed_and_requires_explicit_compatible_limits(self):
        obj, toolhead, rails = self.machine(
            speeds=(75.0,), accel=5360.0,
            options={"homing_accel": 0.0, "max_sample_age": 0.005, "stream_interval": 0.001,
                     "max_report_travel": 1.0, "max_startup_travel": 3.0})
        self.assertEqual(obj.backend.max_speed, 200.0)
        obj.preflight_motion_policy("x")
        self.assertLess(obj.backend.motion_startup_request_travel(75.0, 5360.0), 3.0)
        self.assertEqual(rails[0].get_homing_info().speed, 75.0)
        self.assert_no_effects(obj, toolhead)
        # The unchanged 3 mm check still rejects this same configured speed
        # when a later command lowers global acceleration to 5000.
        toolhead.accel = 5000.0
        with self.assertRaisesRegex(CommandError, "needs 3.04 mm"):
            obj.preflight_motion_policy("x")
        self.assertEqual(rails[0].get_homing_info().speed, 75.0)
        self.assertEqual(toolhead.accel, 5000.0)
        self.assert_no_effects(obj, toolhead)

    def test_missing_selected_rail_fails_with_the_requested_error_type(self):
        obj, toolhead, _rails = self.machine()
        del obj.rails_by_axis["x"]
        with self.assertRaisesRegex(ConfigError, "axis X must use"):
            obj.preflight_motion_policy("x", error=ConfigError)
        self.assert_no_effects(obj, toolhead)

    def test_legacy_beacon_preflight_preserves_its_existing_motion_policy(self):
        obj = make_homing(connect=False)
        obj.preflight_motion_policy("xy")
        self.assertIsNone(obj.toolhead)
        self.assertEqual(obj.fake_printer.objects["toolhead"].moves, [])


class MotionPolicyDiagnosticTests(unittest.TestCase):
    def test_reported_three_point_two_three_budget_has_effective_values_and_a_safe_speed(self):
        obj = generic(options={"max_report_travel": 1.0, "max_startup_travel": 3.0})
        budget = obj.backend.motion_startup_request_travel(50.0, 1500.0)
        self.assertAlmostEqual(budget, 3.2333333333333334)
        maximum = obj.backend.motion_startup_speed_limit(1500.0)
        self.assertGreater(maximum, 47.0)
        self.assertLess(maximum, 48.0)
        with self.assertRaises(CommandError) as caught:
            obj.backend.validate_motion_plan(50.0, 1500.0, CommandError)
        message = str(caught.exception)
        for text in ("3.23 mm", "3.0 mm policy", "speed 50 mm/s", "acceleration 1500 mm/s^2",
                     "at most %.6g mm/s" % maximum, "not a guarantee of physical stopping travel"):
            self.assertIn(text, message)
        obj.backend.validate_motion_plan(maximum, 1500.0, CommandError)

    def test_speed_limit_is_admissible_across_proof_windows_and_extreme_acceleration(self):
        for options in ({"max_sample_age": 0.005, "stream_interval": 0.001}, {},
                        {"max_sample_age": 0.050, "stream_interval": 0.020}):
            obj = generic(options={"max_report_travel": 1.0, "max_startup_travel": 3.0, **options}, connect=False)
            for accel in (5e-324, 0.001, 0.1, 1.0, 100.0, 1500.0, 5000.0, 1e308):
                with self.subTest(options=options, accel=accel):
                    maximum = obj.backend.motion_startup_speed_limit(accel)
                    self.assertTrue(math.isfinite(maximum))
                    self.assertGreater(maximum, 0.0)
                    self.assertLessEqual(maximum, obj.backend.max_speed)
                    obj.backend.validate_motion_plan(maximum, accel, CommandError)
                    if maximum * 1.00002 < obj.backend.max_speed:
                        with self.assertRaisesRegex(CommandError, "3.0 mm policy"):
                            obj.backend.validate_motion_plan(maximum * 1.00002, accel, CommandError)

    def test_unbounded_derived_travel_fails_closed(self):
        obj = generic()
        with self.assertRaisesRegex(CommandError, "derived startup timeout and nominal travel must be finite"):
            obj.backend.validate_motion_plan(50.0, 5e-324, CommandError)

    def test_nonfinite_or_nonpositive_live_limits_are_rejected(self):
        obj = generic()
        for limits in ((float("nan"), 5000.0), (float("inf"), 5000.0), (0.0, 5000.0),
                       (500.0, float("nan")), (500.0, float("inf")), (500.0, 0.0)):
            with self.subTest(limits=limits):
                obj.toolhead.get_max_velocity = lambda: limits
                with self.assertRaisesRegex(CommandError, "max_velocity/max_accel"):
                    obj.backend.validate_rail(info(speed=50.0, retract=0), CommandError)


class ConfigurableStartupBudgetTests(unittest.TestCase):
    """Explicit modeled budgets do not qualify a physical stopping distance."""
    options = {"max_report_travel": 1.5, "max_startup_travel": 4.5}

    def test_explicit_legacy_limits_refuse_75_with_complete_unchanged_settings(self):
        obj, toolhead, rails = MotionPolicyPreflightTests().machine(
            speeds=(75.0,), accel=5000.0,
            options={"max_report_travel": 1.0, "max_startup_travel": 3.0}, connect=False)
        with self.assertRaises(ConfigError) as caught:
            obj.fake_printer.send_event("klippy:connect")
        message = str(caught.exception)
        for part in ("exceeds 50.0", "effective homing speed 75 mm/s", "acceleration 5000 mm/s^2",
                     "report needs 1.5 mm", "max_sample_age 0.02 s", "max_report_travel 1 mm",
                     "startup needs 4.1625 mm", "max_startup_travel 3 mm", "stream_interval 0.005 s",
                     "speed and acceleration were not changed", "not a guarantee of physical stopping travel"):
            self.assertIn(part, message)
        self.assertEqual(rails[0].info.speed, 75.0)
        self.assertEqual(toolhead.accel, 5000.0)
        self.assertEqual(toolhead.moves, [])
        self.assertEqual(toolhead.accel_log, [])
        self.assertEqual(obj.backend.startup_travel, 3.0)
        self.assertEqual(obj.backend.report_travel, 1.0)

    def test_explicit_75_budgets_preserve_timing_and_allow_real_motion(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                obj, attempt, trigger = prepared_generic(speed=75.0, options=self.options,
                                                          direction=direction)
                self.assertEqual(attempt.effective_speed, 75.0)
                self.assertEqual(attempt.effective_accel, 5000.0)
                self.assertEqual(obj.backend.max_age, 0.020)
                self.assertEqual(obj.backend.interval, 0.005)
                self.assertAlmostEqual(obj.backend.motion_startup_timeout(75.0, 5000.0), 0.060)
                self.assertAlmostEqual(obj.backend.motion_startup_request_travel(75.0, 5000.0), 4.1625)
                stream_ramp(obj, attempt, direction=direction)
                self.assertTrue(attempt.motion_seen)
                self.assertIsNone(attempt.reason)
                self.assertEqual(trigger.messages, [])

    def test_explicit_75_parked_request_keeps_deadline_and_configured_budget(self):
        obj, attempt, trigger = prepared_generic(speed=75.0, options=self.options)
        stopped = stream_ramp(obj, attempt, parked=True)
        self.assertEqual(attempt.reason, "no_motion")
        self.assertFalse(attempt.motion_seen)
        self.assertEqual(trigger.messages, [[HOST]])
        elapsed = stopped - attempt.arm_time
        self.assertLessEqual(elapsed, obj.backend.motion_startup_timeout(75.0, 5000.0) + 0.003)
        travel = 75.0 * elapsed - 0.5 * 75.0 ** 2 / 5000.0
        self.assertLessEqual(travel, obj.backend.startup_travel)

    def test_same_75_budgets_refuse_lower_live_accel_before_workflow_effects(self):
        obj, toolhead, rails = MotionPolicyPreflightTests().machine(
            speeds=(75.0,), accel=5000.0, options=self.options)
        toolhead.accel = 1500.0
        with self.assertRaises(CommandError) as caught:
            obj.preflight_motion_policy(["x"])
        message = str(caught.exception)
        for part in ("startup needs 5.475 mm", "max_startup_travel 4.5 mm",
                     "speed 75 mm/s", "acceleration 1500 mm/s^2"):
            self.assertIn(part, message)
        self.assertEqual(toolhead.accel, 1500.0)
        self.assertEqual(toolhead.accel_log, [])
        self.assertEqual(toolhead.moves, [])
        self.assertEqual(rails[0].info.speed, 75.0)
        self.assertFalse(obj.backend.active)

    def test_budget_bounds_and_nonfinite_values_are_rejected(self):
        for value in (0.0, 0.099999, 5.000001, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                generic(options={"max_startup_travel": value}, connect=False)
        for value in (0.1, 3.0, 4.5, 5.0):
            obj = generic(options={"max_startup_travel": value}, connect=False)
            self.assertEqual(obj.backend.startup_travel, value)
            self.assertEqual(obj.backend.get_status()["startup_motion_request_limit_mm"], value)
            self.assertAlmostEqual(obj.backend.motion_startup_timeout(75.0, 5000.0), 0.060)

    def test_exact_startup_and_report_boundaries_match_strict_installer_policy(self):
        required = 4.1625
        for configured, accepted in ((required - 0.0000001, False),
                                     (required, True), (required + 0.0000001, True)):
            obj = generic(options={"max_report_travel": 1.5, "max_startup_travel": configured})
            if accepted:
                obj.backend.validate_motion_plan(75.0, 5000.0, CommandError)
            else:
                with self.assertRaises(CommandError):
                    obj.backend.validate_motion_plan(75.0, 5000.0, CommandError)
        for configured, accepted in ((1.5 - 0.000000001, False), (1.5, True)):
            obj = generic(options={"max_report_travel": configured, "max_startup_travel": 4.5})
            if accepted:
                obj.backend.validate_motion_plan(75.0, 5000.0, CommandError)
            else:
                with self.assertRaisesRegex(CommandError, "sample-age/report-travel"):
                    obj.backend.validate_motion_plan(75.0, 5000.0, CommandError)

    def test_speed_ceiling_uses_each_configured_budget_at_extreme_accelerations(self):
        for budget in (0.1, 3.0, 4.5, 5.0):
            obj = generic(options={"max_report_travel": 2.0, "max_startup_travel": budget})
            for accel in (5e-324, 1500.0, 5000.0, 1e308):
                with self.subTest(budget=budget, accel=accel):
                    maximum = obj.backend.motion_startup_speed_limit(accel)
                    self.assertTrue(math.isfinite(maximum))
                    self.assertGreater(maximum, 0.0)
                    self.assertLessEqual(maximum, obj.backend.max_speed)
                    obj.backend.validate_motion_plan(maximum, accel, CommandError)
                    # The diagnostic includes a fixed 1 micrometre interior
                    # margin; a 0.1% overshoot also clears it at the 0.1 mm cap.
                    if maximum * 1.001 < obj.backend.max_speed:
                        with self.assertRaises(CommandError):
                            obj.backend.validate_motion_plan(maximum * 1.001, accel, CommandError)

    def test_final_prepare_rechecks_reporting_even_when_startup_would_fit(self):
        obj, attempt, _trigger = prepared_generic(speed=75.0, options=self.options)
        obj.disarm()
        obj.session_axes["x"].info = info(speed=80.0, second=80.0, retract=0)
        self.assertLess(obj.backend.motion_startup_request_travel(80.0, 5000.0), 4.5)
        with self.assertRaisesRegex(CommandError, "exceeds 75.0"):
            obj.prepare(attempt.endstop, 11.0)

    def test_second_pass_speed_uses_both_configured_limits(self):
        obj = generic(options=self.options)
        obj.backend.validate_rail(info(speed=50.0, second=75.0, retract=5.0), CommandError)
        plain = generic(options={"max_report_travel": 1.0, "max_startup_travel": 3.0})
        with self.assertRaisesRegex(CommandError, "speed 75 mm/s"):
            plain.backend.validate_rail(info(speed=50.0, second=75.0, retract=5.0), CommandError)

    def test_status_and_console_expose_configured_nominal_limits(self):
        obj = generic(options=self.options)
        status = obj.backend.get_status()
        self.assertEqual(status["startup_motion_request_limit_mm"], 4.5)
        self.assertEqual(status["max_report_travel_mm"], 1.5)
        self.assertEqual(status["max_homing_speed"], 75.0)
        self.assertFalse(status["latency_guaranteed"])
        messages = []
        obj.cmd_STATUS(types.SimpleNamespace(respond_info=messages.append))
        self.assertTrue(any("max_report_travel 1.500 mm, max_startup_travel 4.500 mm" in line
                            and "not physical stopping distances" in line for line in messages))
        self.assertTrue(any("report-age speed ceiling 75.0 mm/s" in line for line in messages))


class OptionalTravelPolicyTests(unittest.TestCase):
    """Configured speeds have no default plugin travel ceiling; live guards remain."""

    def test_absent_policies_accept_75_and_100_connect_and_preflight_without_edits(self):
        obj, toolhead, rails = MotionPolicyPreflightTests().machine(speeds=(75.0, 100.0), accel=5000.0)
        obj.preflight_motion_policy("xy")
        self.assertEqual([rail.info.speed for rail in rails], [75.0, 100.0])
        self.assertIsNone(obj.backend.report_travel)
        self.assertIsNone(obj.backend.startup_travel)
        self.assertIsNone(obj.backend.max_speed)
        self.assertIsNone(obj.backend.motion_startup_speed_limit(5000.0))
        self.assertEqual(obj.backend.max_age, 0.020)
        self.assertEqual(obj.backend.interval, 0.005)
        MotionPolicyPreflightTests().assert_no_effects(obj, toolhead)

    def test_absent_policies_prepare_real_75_and_100_in_both_directions(self):
        for speed in (75.0, 100.0):
            for direction in (-1, 1):
                with self.subTest(speed=speed, direction=direction):
                    obj, attempt, trigger = prepared_generic(speed=speed, direction=direction)
                    self.assertEqual(attempt.effective_speed, speed)
                    self.assertEqual(attempt.effective_accel, 5000.0)
                    self.assertEqual(obj.session_axes["x"].info.speed, speed)
                    stream_ramp(obj, attempt, direction=direction)
                    self.assertTrue(attempt.motion_seen)
                    self.assertIsNone(attempt.reason)
                    self.assertEqual(trigger.messages, [])

    def test_absent_policies_parked_75_and_100_still_stop_at_same_deadline(self):
        for speed in (75.0, 100.0):
            for direction in (-1, 1):
                with self.subTest(speed=speed, direction=direction):
                    obj, attempt, trigger = prepared_generic(speed=speed, direction=direction)
                    stopped = stream_ramp(obj, attempt, parked=True)
                    self.assertFalse(attempt.motion_seen)
                    self.assertEqual(attempt.reason, "no_motion")
                    self.assertEqual(trigger.messages, [[HOST]])
                    self.assertLessEqual(stopped - attempt.arm_time,
                                         obj.backend.motion_startup_timeout(speed, 5000.0) + 0.003)
                    with self.assertRaisesRegex(CommandError, "motion was not verified"):
                        obj.finish(attempt.endstop, attempt, HOST)

    def test_each_explicit_policy_is_enforced_independently(self):
        for name in ("max_report_travel", "max_startup_travel"):
            for invalid in (None, "None"):
                with self.subTest(option=name, invalid=invalid), self.assertRaises((TypeError, ValueError)):
                    generic(options={name: invalid})
        report_only = generic(options={"max_report_travel": 1.0})
        self.assertIsNone(report_only.backend.startup_travel)
        with self.assertRaisesRegex(CommandError, "sample-age/report-travel"):
            report_only.backend.validate_motion_plan(75.0, 5000.0, CommandError)
        self.assertEqual(report_only.backend.motion_startup_speed_limit(5000.0), 50.0)
        startup_only = generic(options={"max_startup_travel": 3.0})
        self.assertIsNone(startup_only.backend.max_speed)
        with self.assertRaisesRegex(CommandError, "3.0 mm policy"):
            startup_only.backend.validate_motion_plan(75.0, 5000.0, CommandError)
        self.assertAlmostEqual(startup_only.backend.motion_startup_speed_limit(5000.0), 55.9729, places=3)
        generic(options={"max_startup_travel": 4.5}).backend.validate_motion_plan(75.0, 5000.0, CommandError)
        generic(options={"max_report_travel": 1.5}).backend.validate_motion_plan(75.0, 1500.0, CommandError)

    def test_startup_only_ceiling_is_bounded_without_reporting_cap(self):
        for accel in (5e-324, 100.0, 5000.0, 1e308):
            obj = generic(options={"max_startup_travel": 3.0})
            maximum = obj.backend.motion_startup_speed_limit(accel)
            self.assertTrue(math.isfinite(maximum))
            self.assertGreater(maximum, 0.0)
            obj.backend.validate_motion_plan(maximum, accel, CommandError)

    def test_disabled_status_is_json_safe_and_console_explicit(self):
        obj, attempt, _trigger = prepared_generic(speed=100.0)
        status = obj.get_status(obj.reactor.now)
        backend = status["backend"]
        self.assertIsNone(backend["max_homing_speed"])
        self.assertIsNone(backend["max_report_travel_mm"])
        self.assertIsNone(backend["startup_motion_request_limit_mm"])
        self.assertEqual(backend["startup_motion_evaluated_speed_mm_s"], 100.0)
        self.assertAlmostEqual(backend["startup_motion_timeout_s"], 0.065)
        json.dumps(status, allow_nan=False)
        messages = []
        obj.cmd_STATUS(types.SimpleNamespace(respond_info=messages.append))
        self.assertTrue(any("report-age speed ceiling disabled" in text for text in messages))
        self.assertTrue(any("max_report_travel disabled, max_startup_travel disabled" in text for text in messages))
        attempt.effective_speed = attempt.effective_accel = None  # Legacy detector replay has no plan.
        speed, accel, timeout = obj._motion_startup_plan(attempt)
        self.assertEqual(speed, 500.0)
        self.assertEqual(accel, 5000.0)
        self.assertTrue(math.isfinite(timeout))
        self.assertIsNone(obj._motion_state(attempt))

    def test_disabled_policies_still_reject_nonfinite_derived_timeout_or_travel(self):
        obj = generic()
        for speed, accel in ((50.0, 5e-324), (1.7e308, 5e307)):
            with self.subTest(speed=speed, accel=accel), self.assertRaisesRegex(
                    CommandError, "derived startup timeout and nominal travel must be finite"):
                obj.backend.validate_motion_plan(speed, accel, CommandError)
        with self.assertRaisesRegex(CommandError, "derived homing deadline must be finite"):
            prepared_generic(speed=1e-320)
        obj.homing_accel = 0.0
        for speed, accel in ((float("nan"), 5000.0), (1.7e308, 5e307)):
            obj.toolhead.get_max_velocity = lambda: (speed, accel)
            status = obj.get_status(obj.reactor.now)
            json.dumps(status, allow_nan=False)
            self.assertIsNone(status["backend"]["startup_motion_request_travel_mm"])

    def test_absent_policies_do_not_disable_missing_stale_or_frozen_faults(self):
        for fault in ("no_data", "stale_data", "frozen"):
            with self.subTest(fault=fault):
                obj, attempt, trigger = prepared_generic(speed=75.0)
                if fault == "no_data":
                    obj.reactor.now = attempt.arm_time + 0.021
                    obj._watchdog(obj.reactor.now)
                elif fault == "stale_data":
                    feed(obj, [(attempt.arm_time + number / 3200.0, 0.0, 0.0, G)
                               for number in range(16)], age=0.050)
                else:
                    while not attempt.done and obj.reactor.now < attempt.arm_time + 0.060:
                        first = obj.backend.last_sample_time + 1 / 3200.0
                        feed(obj, [(first + number / 3200.0, 0.0, 0.0, G) for number in range(16)])
                        obj._watchdog(obj.reactor.now)
                self.assertEqual(attempt.reason, fault)
                self.assertEqual(trigger.messages, [[HOST]])
                self.assertFalse(attempt.motion_seen)

    def test_firmware_velocity_cap_is_preserved_without_editing_rail_speed(self):
        obj, attempt, _trigger = prepared_generic(speed=1000.0, velocity=100.0)
        self.assertEqual(obj.session_axes["x"].info.speed, 1000.0)
        self.assertEqual(attempt.effective_speed, 100.0)
        self.assertEqual(obj.toolhead.get_max_velocity()[0], 100.0)


class BeaconSlowMotionProofTests(unittest.TestCase):
    def test_slow_beacon_motion_is_proved_without_altering_legacy_pressed_gate(self):
        for speed in (20.0, 30.0):
            for direction in (-1, 1):
                obj = make_homing(impact_sign=0)
                for start in range(0, 96, 8):
                    obj._inspect(batch(start, [(0, 0, counts(1.0))] * 8))
                endstop = FakeEndstop("x")
                endstop.get_steppers = lambda: [SignedRamp(0.2, 5000.0, speed, direction)]
                obj.session_axes["x"] = FakeRail(endstop, info(speed=speed, retract=0, second=speed))
                acquire_reference(obj, endstop, 0.2)
                attempt = obj.prepare(endstop, 0.2)
                obj.arm(attempt)
                for start in range(200, 296, 8):
                    data = []
                    for offset in range(8):
                        elapsed = (start + offset) / 1000.0 - attempt.arm_time
                        accel = direction * 5000.0 if 0.0 <= elapsed < speed / 5000.0 else 0.0
                        data.append((counts(accel / G) + offset % 2, 0, counts(1.0)))
                    obj._inspect(batch(start, data))
                    obj._check_pressed(attempt)
                self.assertTrue(attempt.motion_seen)
                self.assertFalse(attempt.moving)  # Legacy pressed gate remains40mm/s.
                self.assertEqual(endstop.trigger_log.messages, [])
                obj._inspect(batch(296, [(counts(-12 * direction), 0, counts(1.0))] * 8))
                obj.disarm()
                self.assertEqual(obj.finish(endstop, attempt, HIT), 0.296)
                self.assertFalse(attempt.record["moving"])
                self.assertTrue(attempt.record["motion_seen"])


class WorkflowMotionProofIntegrationTests(unittest.TestCase):
    def test_relative_motion_proof_is_accepted_when_legacy_moving_is_false(self):
        rig = Rig()
        quality, _message = rig.workflow._quality("x", {
            "result": "impact", "travel_mm": 150.0, "moving": False, "motion_seen": True})
        self.assertEqual(quality, "good")

    def test_initial_no_motion_failure_aborts_calibration_without_reposition(self):
        rig = Rig()
        rig.home_results.append(CommandError("toolhead motion was not verified"))
        with self.assertRaisesRegex(CommandError, "motion was not verified"):
            rig.calibrate()
        self.assertIsNone(rig.workflow.session)
        self.assertEqual(rig.workflow.last_result["result"], "aborted")
        self.assertEqual(rig.toolhead.moves, [])
        self.assertEqual(rig.configfile.saved, {})
        self.assertFalse(rig.capture_active)


if __name__ == "__main__":
    unittest.main()
