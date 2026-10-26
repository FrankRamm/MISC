"""Synthetic motion/step histories for reviewer P1/P2; no printer access."""
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
                           "stream_interval": 0.001})
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
        obj = generic(options={"homing_accel": 100.0})
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
        obj, attempt, _trigger = prepared_generic()
        obj.disarm()
        obj.toolhead.get_max_velocity = lambda: (500.0, 100.0)
        with self.assertRaisesRegex(CommandError, "host stop request"):
            obj.prepare(attempt.endstop, 11.0)

    def test_default_deadline_and_nominal_command_request_budget_are_explicit(self):
        obj, _attempt, _trigger = prepared_generic()
        status = obj.backend.get_status()
        self.assertAlmostEqual(status["startup_motion_timeout_s"], 0.055)
        self.assertAlmostEqual(status["startup_motion_request_travel_mm"], 2.65)
        self.assertEqual(status["startup_motion_request_limit_mm"], 3.0)
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
        obj, toolhead, _rails = self.machine(accel=1500.0, connect=False)
        with self.assertRaisesRegex(ConfigError, "3.23 mm"):
            obj.fake_printer.send_event("klippy:connect")
        self.assert_no_effects(obj, toolhead)

    def test_runtime_preflight_rejects_lower_live_accel_before_any_effect(self):
        obj, toolhead, _rails = self.machine()
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
                obj, toolhead, rails = self.machine(speeds=(30.0,), options={"homing_accel": cap})
                toolhead.accel = live
                rails[0].info = info(speed=50.0, retract=0)
                with self.assertRaisesRegex(CommandError, "acceleration %s mm/s" % expected):
                    obj.preflight_motion_policy("x")
                self.assert_no_effects(obj, toolhead)
                self.assertEqual(toolhead.accel, live)

    def test_all_selected_xy_plans_are_checked_and_z_is_ignored(self):
        obj, toolhead, rails = self.machine(speeds=(30.0, 30.0))
        toolhead.accel = 1500.0
        rails[1].info = info(speed=50.0, retract=0)
        obj.preflight_motion_policy("xz")
        with self.assertRaisesRegex(CommandError, "3.23 mm"):
            obj.preflight_motion_policy("xy")
        self.assert_no_effects(obj, toolhead)

    def test_runtime_75_preserves_speed_and_requires_explicit_compatible_limits(self):
        obj, toolhead, rails = self.machine(
            speeds=(75.0,), accel=5360.0,
            options={"homing_accel": 0.0, "max_sample_age": 0.005, "stream_interval": 0.001})
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
        obj = generic()
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
            obj = generic(options=options, connect=False)
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
        with self.assertRaisesRegex(CommandError, "3.0 mm policy"):
            obj.backend.validate_motion_plan(50.0, 5e-324, CommandError)

    def test_nonfinite_or_nonpositive_live_limits_are_rejected(self):
        obj = generic()
        for limits in ((float("nan"), 5000.0), (float("inf"), 5000.0), (0.0, 5000.0),
                       (500.0, float("nan")), (500.0, float("inf")), (500.0, 0.0)):
            with self.subTest(limits=limits):
                obj.toolhead.get_max_velocity = lambda: limits
                with self.assertRaisesRegex(CommandError, "max_velocity/max_accel"):
                    obj.backend.validate_rail(info(speed=50.0, retract=0), CommandError)


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
