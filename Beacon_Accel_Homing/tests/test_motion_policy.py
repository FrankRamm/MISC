"""Synthetic motion/step histories for reviewer P1/P2; no printer access."""
import unittest

from harness import CommandError, FakeEndstop, acquire_reference, batch, counts, make_homing, plugin
from test_backends import baseline, feed, generic
from test_session import FakeRail, info
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
