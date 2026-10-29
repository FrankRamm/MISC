"""Source-only premature-trigger regressions; execution requires operator approval."""
import itertools
import json
import unittest
from unittest import mock

from harness import CommandError
from test_calibration_auto_return import intercept_return, machine
from test_workflows import GCmd, Rig


class MidtravelRecoveryTests(unittest.TestCase):
    def test_51_175_mm_trigger_reverses_halt_then_discovers_at_2_5_before_ten_fresh_checks(self):
        for axis, positive, cartesian, validation, overshoot in itertools.product(
                "xy", (False, True), (False, True), (False, True), (0., 2.)):
            with self.subTest(axis=axis, positive=positive, cartesian=cartesian,
                              validation=validation, overshoot=overshoot):
                rig = machine(axis, positive, cartesian)
                rail = rig.owner.rails_by_axis[axis]
                rail.bounds = (0., 218.5)
                rail.info.position_endstop = rail.bounds[1 if positive else 0]
                index, sign = "xy".index(axis), 1 if positive else -1
                initial = rig.toolhead.get_position()
                prefix = [{}, {}] if validation else []
                halt = 51.175 + overshoot
                rig.home_results.extend(prefix + [{"travel_mm": 51.175,
                                                   "halt_travel_mm_signed": sign * halt}] + [{}] * 11)
                moves, returning, accepted = [], [], []
                move, save = rig.toolhead.manual_move, rig.workflow._save_model

                def moving(coordinates, speed):
                    moves.append((rig.toolhead.get_position(), list(coordinates)))
                    return move(coordinates, speed)

                def evidence(r, selected, record):
                    captured = r.workflow.session["early_halt"]
                    self.assertEqual(selected, axis)
                    self.assertIs(captured["source"], r.owner.last[axis])
                    self.assertEqual(captured["attempt"], len(prefix) + 1)
                    self.assertEqual(record["travel_mm_exact"], 51.175)
                    self.assertTrue(record["early_capture_valid"])
                    returning.append(record)

                def saving():
                    accepted.extend(record["workflow_attempt"]
                                    for record in rig.workflow.session["validation_records"])
                    return save()

                with intercept_return(rig, evidence), \
                        mock.patch.object(rig.toolhead, "manual_move", side_effect=moving), \
                        mock.patch.object(rig.toolhead, "set_position", side_effect=AssertionError("coordinate rebase")), \
                        mock.patch.object(rig.workflow, "_save_model", side_effect=saving):
                    rig.calibrate(axis.upper(), save=0, START_THRESHOLD=2.25, STEP=.25)
                before, coordinates = moves[len(prefix)]
                self.assertAlmostEqual(coordinates[index] - before[index], -sign * halt)
                self.assertEqual(coordinates[index], rail.info.position_endstop - sign * 51.175)
                self.assertTrue(all(value is None for i, value in enumerate(coordinates) if i != index))
                restarted = rig.home_calls[len(prefix) + 1]
                self.assertEqual(restarted["position"][index], coordinates[index])
                self.assertEqual(restarted["homed_axes"], "z")
                self.assertFalse(restarted["candidate_qualified"])
                self.assertEqual(restarted["successes"], 0)
                self.assertEqual([call["threshold"] for call in rig.home_calls],
                                 [2.25] * (len(prefix) + 1) + [2.5] * 11)
                self.assertEqual(accepted, list(range(len(prefix) + 3, len(prefix) + 13)))
                self.assertEqual(len(returning), 1)
                self.assertEqual(returning[0]["early_return_mm_signed"], -sign * halt)
                self.assertFalse(returning[0]["early_return_physical_position_verified"])
                self.assertEqual(rig.toolhead.position[1 - index], initial[1 - index])
                self.assertEqual(rig.toolhead.position[2:], initial[2:])
                self.assertEqual(rig.disabled, [])
                result = rig.workflow.last_result
                self.assertEqual(result["threshold_g"], 2.5)
                self.assertEqual(result["successes"], 10)
                self.assertEqual(result["recovery"]["count"], 1)
                self.assertEqual(result["recovery"]["distance_mm"], halt)
                self.assertEqual(result["recovery"]["distance_limit_mm"], 98.325)
                self.assertFalse(result["recovery"]["physical_clearance_verified"])
                metadata = json.loads(rig.configfile.saved[("accel_homing_model default", axis + "_metadata")])
                self.assertEqual(metadata["recovery"], result["recovery"])

    def test_second_51_175_mm_return_exceeds_98_325_mm_budget_without_moving_or_incrementing(self):
        for axis, positive in itertools.product("xy", (False, True)):
            with self.subTest(axis=axis, positive=positive):
                rig = machine(axis, positive)
                rail = rig.owner.rails_by_axis[axis]
                rail.bounds = (0., 218.5)
                rail.info.position_endstop = rail.bounds[1 if positive else 0]
                other, z_limits = 1 - "xy".index(axis), (20., 480.)
                other_limits = (5., 195.)
                rig.toolhead.kin.limits[2] = z_limits
                initial, home = rig.toolhead.get_position(), rig.workflow._home

                def home_with_other_axis_state(selected, threshold=None):
                    # Install this state after command preflight, before _home
                    # snapshots it. Changing it inside stock G28 must abort.
                    rig.toolhead.kin.limits[other] = other_limits
                    return home(selected, threshold)

                rig.home_results.extend([{"travel_mm": 51.175}] * 2)
                with mock.patch.object(rig.workflow, "_home", side_effect=home_with_other_axis_state), \
                        self.assertRaisesRegex(CommandError, "recovery budget exhausted.*102.350000/98.325000"):
                    rig.calibrate(axis.upper(), save=0, START_THRESHOLD=2.25, STEP=.25)
                self.assertEqual([call["threshold"] for call in rig.home_calls], [2.25, 2.5])
                self.assertEqual(len(rig.toolhead.moves), 1)
                self.assertEqual(rig.workflow.last_result["candidate_g"], 2.5)
                self.assertEqual(rig.workflow.last_result["restarts"], 1)
                budget = rig.workflow.last_result["recovery"]
                self.assertEqual(budget["count"], 1)
                self.assertEqual(budget["distance_mm"], 51.175)
                self.assertEqual(budget["distance_remaining_mm"], 47.15)
                self.assertEqual(rig.toolhead.kin.limits["xy".index(axis)], (1., -1.))
                self.assertEqual(rig.toolhead.kin.limits[other], other_limits)
                self.assertEqual(rig.toolhead.kin.limits[2], z_limits)
                self.assertEqual(rig.toolhead.position[other], initial[other])
                self.assertEqual(rig.toolhead.position[2:], initial[2:])
                self.assertEqual(rig.disabled, [])
                self.assertEqual(rig.configfile.saved, {})
                self.assertIsNone(rig.workflow.session)

    def test_inclusive_distance_limit_accepts_90_mm_but_rejects_one_extra_step(self):
        for extra in (0., .000001):
            with self.subTest(extra=extra):
                rig = machine("y")
                rig.home_results.extend([{"travel_mm": 45.},
                                         {"travel_mm": 45., "halt_travel_mm_signed": -45. - extra}] + [{}] * 11)
                if extra:
                    with self.assertRaisesRegex(CommandError, "recovery budget exhausted"):
                        rig.calibrate("Y", save=0)
                    self.assertEqual(len(rig.toolhead.moves), 1)
                    self.assertEqual(len(rig.home_calls), 2)
                    self.assertEqual(rig.workflow.last_result["candidate_g"], 1.25)
                    self.assertEqual(rig.workflow.last_result["restarts"], 1)
                else:
                    rig.calibrate("Y", save=0)
                    self.assertEqual(rig.workflow.last_result["threshold_g"], 1.5)
                    self.assertEqual(rig.workflow.last_result["recovery"]["count"], 2)
                    self.assertEqual(rig.workflow.last_result["recovery"]["distance_mm"], 90.)
                    self.assertEqual(rig.workflow.last_result["recovery"]["distance_remaining_mm"], 0.)
                self.assertEqual(rig.workflow.last_result["recovery"]["distance_limit_mm"], 90.)

    def test_eighth_zero_halt_reserves_count_and_ninth_aborts_without_increase(self):
        rig = Rig({"calibration_max_attempts": 100})
        rig.home_results.extend([{"travel_mm": 0.}] * 9)
        with self.assertRaisesRegex(CommandError, "recovery budget exhausted.*9/8 returns"):
            rig.calibrate(save=0)
        self.assertEqual([call["threshold"] for call in rig.home_calls], [1. + .25 * i for i in range(9)])
        self.assertEqual(rig.toolhead.moves, [])
        self.assertEqual(rig.workflow.last_result["candidate_g"], 3.)
        self.assertEqual(rig.workflow.last_result["restarts"], 8)
        budget = rig.workflow.last_result["recovery"]
        self.assertEqual(budget["count"], 8)
        self.assertEqual(budget["count_limit"], 8)
        self.assertEqual(budget["count_remaining"], 0)
        self.assertEqual(budget["distance_mm"], 0.)
        self.assertEqual(rig.configfile.saved, {})
        self.assertEqual(rig.disabled, [])

    def test_legacy_and_midtravel_reservations_survive_spread_candidate_reset(self):
        rig = Rig()
        rig.home_results.extend([{"travel_mm": 8.}, {"travel_mm": 51.175}, {},
                                 {"travel_mm": 150.}, {"travel_mm": 152.0001}, {"travel_mm": 80.}])
        states = []
        rig.on_home = lambda axis: states.append(rig.workflow.get_status(0)["active"])
        with self.assertRaisesRegex(CommandError, "recovery budget exhausted"):
            rig.calibrate(save=0)
        self.assertEqual([call["threshold"] for call in rig.home_calls], [1., 1.25, 1.5, 1.5, 1.5, 1.75])
        self.assertEqual(len(rig.toolhead.moves), 5)
        self.assertEqual(rig.workflow.last_result["candidate_g"], 1.75)
        self.assertEqual(rig.workflow.last_result["restarts"], 3)
        for status in (states[2], states[-1], rig.workflow.last_result):
            self.assertEqual(status["recovery"]["count"], 2)
            self.assertEqual(status["recovery"]["distance_mm"], 59.175)
        self.assertFalse(states[-1]["candidate_qualified"])
        self.assertEqual(states[-1]["successes"], 0)
        self.assertEqual(rig.configfile.saved, {})

    def test_recovery_limits_use_opposite_bound_not_endstop_and_preserve_nominal_margin(self):
        for positive, bounds, endstop, clearance, margin, limit in (
                (False, (10., 310.), 20., 150., 15., 135.),
                (True, (10., 310.), 300., 150., 15., 135.),
                (False, (0., 42.), 0., 21., 2.1, 18.9)):
            with self.subTest(positive=positive, bounds=bounds):
                rig = machine(positive=positive)
                rail = rig.owner.rails_by_axis["x"]
                rail.bounds, rail.info.position_endstop = bounds, endstop
                rig.workflow._begin(GCmd(SAVE_CONFIG=0), "calibration", "x")
                budget = rig.workflow.get_status(0)["active"]["recovery"]
                self.assertEqual(budget["nominal_opposite_clearance_mm"], clearance)
                self.assertEqual(budget["margin_mm"], margin)
                self.assertEqual(budget["distance_limit_mm"], limit)
                self.assertEqual(budget["count_remaining"], 8)
                self.assertFalse(budget["physical_clearance_verified"])
                self.assertEqual(rig.toolhead.moves, [])

    def test_same_run_manual_continuation_retains_all_earlier_reservations(self):
        rig = Rig()
        workflow = rig.workflow
        workflow._begin(GCmd(SAVE_CONFIG=0), "calibration", "x")
        workflow.session["state"] = "running"
        rig.home_results.append({"travel_mm": 51.175})
        record, _capture = workflow._home("x", 1.)
        workflow._return_early_calibration("x", record)
        workflow._invalidate_calibration_axis(workflow.session)
        workflow._next_calibration_candidate("synthetic retained wait after recovery")
        session, run_id = workflow.session, workflow.session["run_id"]
        workflow._wait_center("synthetic retained manual-centering wait")
        self.assertIs(workflow.session, session)
        self.assertEqual(workflow.get_status(0)["active"]["recovery"]["count"], 1)
        rig.home_results.append({"travel_mm": 90.})
        with self.assertRaisesRegex(CommandError, "recovery budget exhausted"):
            rig.calibrate(save=0)
        self.assertEqual(rig.home_calls[-1]["threshold"], 1.25)
        self.assertEqual(len(rig.toolhead.moves), 1)
        self.assertEqual(rig.workflow.last_result["candidate_g"], 1.25)
        self.assertEqual(rig.workflow.last_result["restarts"], 1)
        self.assertEqual(rig.workflow.last_result["recovery"]["count"], 1)
        self.assertEqual(rig.workflow.last_result["recovery"]["distance_mm"], 51.175)
        self.assertEqual(session["run_id"], run_id)
        self.assertEqual(rig.configfile.saved, {})

    def test_threshold_attempt_and_overshoot_rejections_leave_candidate_and_budgets_unchanged(self):
        for fault in ("threshold", "attempts", "overshoot"):
            with self.subTest(fault=fault):
                rig = Rig({"calibration_max_threshold": 2.25} if fault == "threshold" else {})
                if fault == "attempts":
                    rig.workflow.max_attempts = 1
                rig.home_results.append({"travel_mm": 51.175,
                                         "halt_travel_mm_signed": -53.175001 if fault == "overshoot" else -51.175})
                with self.assertRaises(CommandError):
                    rig.calibrate(save=0, START_THRESHOLD=2.25, STEP=.25)
                self.assertEqual(len(rig.home_calls), 1)
                self.assertEqual(rig.toolhead.moves, [])
                result = rig.workflow.last_result
                self.assertEqual(result["candidate_g"], 2.25)
                self.assertEqual(result["restarts"], 0)
                self.assertEqual(result["recovery"]["count"], 0)
                self.assertEqual(result["recovery"]["distance_mm"], 0.)
                self.assertEqual(rig.disabled, [])
                self.assertEqual(rig.configfile.saved, {})

    def test_midtravel_cleanup_evidence_is_marked_before_capture_stream_or_wait_failures(self):
        for failure in ("capture-data", "capture-stop", "stream-stop", "post-home-wait"):
            with self.subTest(failure=failure):
                rig = Rig()
                other_limits, z_limits = (5., 195.), (20., 480.)
                rig.toolhead.kin.limits[2] = z_limits
                rig.on_home = lambda axis: rig.toolhead.kin.limits.__setitem__(1, other_limits)
                rig.home_results.append({"travel_mm": 51.175})
                target, method = {"capture-data": (rig.owner, "stop_capture"),
                                  "capture-stop": (rig.owner, "stop_capture"),
                                  "stream-stop": (rig.owner, "_end_session"),
                                  "post-home-wait": (rig.toolhead, "wait_moves")}[failure]
                original, checked = getattr(target, method), []

                def fail_after_halt(*args, **kwargs):
                    result = original(*args, **kwargs)
                    source = rig.owner.last.get("x")
                    if source is not None and not checked:
                        session = rig.workflow.session
                        self.assertTrue(session["early_return_cleanup"])
                        self.assertIs(session["early_halt"]["source"], source)
                        self.assertEqual(len(session["early_halt"]["steps"]), 2)
                        checked.append(True)
                        if failure == "capture-data":
                            result["samples"] = []
                        else:
                            raise CommandError("synthetic cleanup fault")
                    return result

                with mock.patch.object(target, method, side_effect=fail_after_halt), \
                        self.assertRaisesRegex(CommandError, "cleanup fault|empty or incomplete"):
                    rig.calibrate(save=0)
                self.assertEqual(checked, [True])
                self.assertEqual(len(rig.home_calls), 1)
                self.assertEqual(rig.toolhead.moves, [])
                self.assertEqual(rig.toolhead.kin.limits, [(1., -1.), other_limits, z_limits])
                self.assertEqual(rig.disabled, [])
                self.assertEqual(rig.workflow.last_result["recovery"]["count"], 0)
                self.assertEqual(rig.configfile.saved, {})
