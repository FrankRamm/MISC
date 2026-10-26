"""Synthetic accuracy trials; no hardware, service, network or motion access."""
import copy
import contextlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from harness import CommandError, arm_axis, batch, counts, make_homing
from test_workflows import GCmd, Rig, workflow_module


class AccuracyRig(Rig):
    """Reuse the stock-handler fixture with current per-trial sensor evidence."""

    def __init__(self, options=None):
        super().__init__(options)
        self.sensor_time = 0.0
        self.capture_mutator = None

    def stock_home(self, gcmd):
        self.sensor_time += 1.0
        super().stock_home(gcmd)
        if self.home_calls:
            axis = self.home_calls[-1]["axis"]
            record = self.owner.last[axis]
            record["arm_time_s"] = self.sensor_time + 0.001
            record["onset_time_s"] = self.sensor_time + 0.002

    def stop_capture(self):
        self.capture_active = False
        self.capture_stops += 1
        if self.bad_capture is not None:
            return self.bad_capture
        axis = self.home_calls[-1]["axis"] if self.home_calls else "x"
        record = self.owner.last.get(axis)
        if record is None:
            return {"samples": [], "dropped": 0}
        t0 = self.sensor_time
        capture = {"samples": [(t0, 0.0, 0.0, 1.0),
                               (t0 + 0.001, 0.1, 0.0, 1.0),
                               (t0 + 0.002, -4.0, -5.0, 1.0),
                               (t0 + 0.003, 0.0, 0.0, 1.0)],
                   "dropped": 0, "baseline_g": (0.0, 0.0, 1.0), "_record": record,
                   "detector": {"axis": axis, "attempt_count": 1,
                                "arm_time_s": record["arm_time_s"],
                                "onset_time_s": record["onset_time_s"],
                                "threshold_g": self.home_calls[-1]["threshold"],
                                "onset_g": record.get("onset_g"),
                                "impact_peak_g": record.get("impact_peak_g"),
                                "direction": record.get("direction", -1),
                                "homing_speed_mm_s": self.owner.rails_by_axis[axis].info.speed,
                                "homing_accel_mm_s2": self.owner.homing_accel}}
        if self.capture_mutator is not None:
            self.capture_mutator(capture)
        return capture

    def accuracy(self, axis="X", samples=2, **params):
        arguments = {"CENTERED": 1, "SAMPLES": samples}
        if axis is not None:
            arguments["AXIS"] = axis
        arguments.update(params)
        self.workflow.cmd_ACCURACY(GCmd(**arguments))


class AccuracyWorkflowTests(unittest.TestCase):
    def assert_restored(self, rig, thresholds=None):
        self.assertIsNone(rig.workflow.session)
        self.assertFalse(rig.workflow.in_home)
        self.assertFalse(rig.capture_active)
        self.assertEqual(rig.owner.threshold_g, thresholds or {"x": 8.0, "y": 9.0})
        self.assertEqual(rig.owner.back_off_dist, 5.0)
        self.assertEqual(rig.configfile.saved, {})
        self.assertNotIn("SAVE_CONFIG", rig.scripts)
        self.assertEqual(rig.dispatched_scripts, [])
        self.assertEqual(rig.reactor.timers[rig.workflow.timer], rig.reactor.NEVER)
        self.assertEqual(rig.toolhead.position[2:], [40.0, 6.0])
        self.assertTrue(all(coords[2] is None for coords, _speed in rig.toolhead.moves))

    def test_registration_and_invalid_parameters_have_no_effects(self):
        cases = ({"AXIS": "Z", "CENTERED": 1}, {"AXIS": "YX", "CENTERED": 1},
                 {"AXIS": "", "CENTERED": 1}, {"CENTERED": 0}, {},
                 {"CENTERED": "1.0"}, {"CENTERED": 1, "SAMPLES": 1},
                 {"CENTERED": 1, "SAMPLES": 51}, {"CENTERED": 1, "SAVE_CONFIG": 0},
                 {"CENTERED": 1, "SAVE_CONFIG": 1})
        for params in cases:
            with self.subTest(params=params):
                rig = AccuracyRig()
                self.assertIn("ACCEL_HOMING_ACCURACY", rig.gcode.ready_gcode_handlers)
                before = rig.effects()
                with self.assertRaises(CommandError):
                    rig.workflow.cmd_ACCURACY(GCmd(**params))
                self.assertEqual(before, rig.effects())

    def test_preflight_reuses_idle_unhomed_stock_policy_and_geometry_checks(self):
        setters = (lambda r: setattr(r, "print_state", "printing"),
                   lambda r: setattr(r, "paused", True),
                   lambda r: setattr(r, "sd_active", True),
                   lambda r: setattr(r, "idle_state", "Printing"),
                   lambda r: setattr(r, "shutdown", True),
                   lambda r: setattr(r.owner, "homing_active", True),
                   lambda r: setattr(r.owner, "attempt", object()),
                   lambda r: setattr(r.toolhead, "busy", (0.0, 10.0, False)),
                   lambda r: setattr(r, "policy_error", "speed too high"),
                   lambda r: r.toolhead.kin.limits.__setitem__(0, (0.0, 300.0)),
                   lambda r: setattr(r.owner.rails_by_axis["x"].info, "retract_dist", 1.0),
                   lambda r: r.objects.__setitem__("homing_override", object()),
                   lambda r: r.objects.__setitem__("gcode_macro G28", object()),
                   lambda r: r.gcode.gcode_handlers.__setitem__("G28", lambda g: None))
        for setter in setters:
            with self.subTest(setter=setter):
                rig = AccuracyRig()
                setter(rig)
                before = rig.effects()
                with self.assertRaises(CommandError):
                    rig.accuracy()
                self.assertEqual(before, rig.effects())

    def test_discovery_is_excluded_population_statistics_match_known_values(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            rig.home_results.extend({"travel_mm": value} for value in (145.0, 149.0, 150.0, 153.0))
            rig.accuracy(samples=3)
            result = rig.workflow.last_result
            self.assertEqual(result["result"], "complete")
            axis = result["axis_results"]["x"]
            self.assertEqual(axis["discovery"]["travel_mm"], 145.0)
            self.assertEqual([r["travel_mm"] for r in axis["trials"]], [149.0, 150.0, 153.0])
            stats = axis["statistics"]
            self.assertEqual(stats["count"], 3)
            self.assertEqual((stats["min_mm"], stats["max_mm"], stats["spread_mm"]), (149.0, 153.0, 4.0))
            self.assertAlmostEqual(stats["mean_mm"], 452.0 / 3)
            self.assertAlmostEqual(stats["variance_mm2"], 26.0 / 9)
            self.assertAlmostEqual(stats["stddev_mm"], math.sqrt(26.0 / 9))
            self.assertEqual(stats["variance_definition"], "population (divide by N)")
            self.assertEqual([r["workflow_phase"] for r in result["records"]],
                             ["discovery", "measurement", "measurement", "measurement"])
            self.assertEqual(len(rig.toolhead.moves), 4)
            self.assertEqual(rig.toolhead.moves[-1], ([5.0, None, None], 100.0))
            self.assertEqual(len(result["paths"]), 4)
            summary = json.loads(Path(result["summary_paths"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["result"], "complete")
            self.assertTrue(any("at or above" in line for line in rig.output))
            self.assertTrue(any("variance" in line and "mm^2" in line for line in rig.output))
            self.assert_restored(rig)

    def test_xy_default_and_explicit_order_recenter_x_before_y_preserve_z(self):
        for requested in (None, "XY"):
            for z_homed in (False, True):
                with self.subTest(requested=requested, z_homed=z_homed), tempfile.TemporaryDirectory() as temp:
                    rig = AccuracyRig({"charts_directory": temp})
                    if not z_homed:
                        rig.toolhead.kin.limits[2] = (1.0, -1.0)
                    safe = rig.use_safe_z_home()
                    rig.accuracy(axis=requested)
                    self.assertEqual(rig.scripts, ["G28 X"] * 3 + ["G28 Y"] * 3)
                    self.assertEqual(safe.calls, [])
                    self.assertEqual(len(rig.toolhead.moves), 6)
                    self.assertEqual(rig.toolhead.moves[-1], ([None, 5.0, None], 100.0))
                    self.assertEqual(rig.home_calls[3]["position"][0], 150.0)
                    self.assertEqual(rig.home_calls[3]["position"][1], 100.0)
                    result = rig.workflow.last_result
                    self.assertEqual(result["axes"], ["x", "y"])
                    self.assertEqual(result["axis_results"]["x"]["statistics"]["mean_mm"], 150.0)
                    self.assertEqual(result["axis_results"]["y"]["statistics"]["mean_mm"], 100.0)
                    self.assertEqual("z" in rig.toolhead.kin.get_status(0)["homed_axes"], z_homed)
                    self.assert_restored(rig)

    def test_single_y_and_exact_manual_threshold_are_used_without_tuning(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            rig.owner.threshold_g["y"] = 0.001
            rig.owner.rails_by_axis["y"].info.speed = 37.0
            rig.home_results.extend([{"threshold_g": 0.0, "onset_g": 0.0,
                                     "impact_peak_g": 0.0}] * 3)
            def exact_values(capture):
                capture["detector"].update(onset_g=-0.00125, impact_peak_g=0.00175)
            rig.capture_mutator = exact_values
            rig.accuracy(axis="Y")
            self.assertEqual([c["axis"] for c in rig.home_calls], ["y"] * 3)
            self.assertEqual([c["threshold"] for c in rig.home_calls], [0.001] * 3)
            for record in rig.workflow.last_result["records"]:
                self.assertEqual(record["threshold_g"], 0.001)
                self.assertEqual(record["onset_g"], -0.00125)
                self.assertEqual(record["impact_peak_g"], 0.00175)
                self.assertEqual(record["homing_speed_mm_s"], 37.0)
            self.assertEqual(rig.owner.last["y"]["threshold_g"], 0.0)
            self.assert_restored(rig, {"x": 8.0, "y": 0.001})

    def test_default_sample_count_and_maximum_xy_have_independent_exact_attempt_budget(self):
        for samples, expected in ((None, 22), (50, 102)):
            with self.subTest(samples=samples):
                rig = AccuracyRig({"calibration_max_attempts": 12})
                arguments = {"CENTERED": 1}
                if samples is not None:
                    arguments["SAMPLES"] = samples
                with mock.patch.object(workflow_module, "write_capture", return_value={}), \
                        mock.patch.object(workflow_module, "write_accuracy", return_value={}), \
                        mock.patch.object(workflow_module, "validate_accuracy", return_value={}):
                    rig.workflow.cmd_ACCURACY(GCmd(**arguments))
                self.assertEqual(len(rig.home_calls), expected)
                self.assertEqual(rig.capture_stops, expected)
                self.assertEqual(rig.workflow.last_result["attempts"], expected)
                self.assertEqual(rig.workflow.last_result["result"], "complete")
                self.assert_restored(rig)

    def test_bad_home_stops_without_retry_threshold_raise_or_unsafe_recenter(self):
        failures = ({"travel_mm": 2.0}, {"travel_mm": 119.0}, {"travel_mm": 181.0},
                    {"travel_mm": math.nan}, {"travel_mm": math.inf},
                    {"result": "pressed"}, {"result": "no_data"},
                    {"moving": False, "motion_seen": False})
        for preceding in (0, 2):
            for failure in failures:
                with self.subTest(preceding=preceding, failure=failure), tempfile.TemporaryDirectory() as temp:
                    rig = AccuracyRig({"charts_directory": temp})
                    rig.home_results.extend([{}] * preceding + [failure])
                    with self.assertRaisesRegex(CommandError, "no automatic move"):
                        rig.accuracy(axis="XY", samples=3)
                    self.assertEqual(len(rig.home_calls), preceding + 1)
                    self.assertEqual(len(rig.toolhead.moves), preceding)
                    result = rig.workflow.last_result
                    self.assertEqual(result["result"], "aborted")
                    self.assertEqual(len(result["axis_results"]["x"]["trials"]), max(0, preceding - 1))
                    self.assertIsNotNone(result["axis_results"]["x"]["rejected"])
                    self.assertEqual(result["axis_results"]["y"]["trials"], [])
                    self.assertIn("summary_paths", result)
                    if preceding == 2:
                        stats = result["axis_results"]["x"]["statistics"]
                        self.assertIsNone(stats["stddev_mm"])
                        self.assertIsNone(stats["variance_mm2"])
                    self.assert_restored(rig)

    def test_invalid_sensor_capture_or_provenance_aborts_and_closes_capture(self):
        def replace(capture, **values):
            capture.update(values)
        mutations = (lambda c: replace(c, samples=[]), lambda c: replace(c, dropped=1),
                     lambda c: c["samples"].__setitem__(2, (math.nan, 0, 0, 1)),
                     lambda c: c["samples"].__setitem__(2, c["samples"][1]),
                     lambda c: replace(c, baseline_g=None),
                     lambda c: replace(c, _record=copy.copy(c["_record"])),
                     lambda c: replace(c, detector=None),
                     lambda c: c["detector"].update(axis="y"),
                     lambda c: c["detector"].update(attempt_count=2),
                     lambda c: c["detector"].update(arm_time_s=0.001),
                     lambda c: c["detector"].update(threshold_g=8.01),
                     lambda c: replace(c, fault="stale_data"),
                     lambda c: replace(c, error="invalid_data"),
                     lambda c: c["detector"].update(direction=4),
                     lambda c: c["detector"].update(homing_speed_mm_s=math.nan),
                     lambda c: c["detector"].update(homing_accel_mm_s2=0),
                     lambda c: c["detector"].update(onset_time_s=12345.0),
                     lambda c: c["detector"].update(onset_g=math.nan),
                     lambda c: replace(c, samples=c["samples"][1:]))
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                rig = AccuracyRig({"charts_directory": temp})
                rig.capture_mutator = mutation
                with self.assertRaises(CommandError):
                    rig.accuracy()
                self.assertEqual(rig.scripts, ["G28 X"])
                self.assertEqual(rig.toolhead.moves, [])
                self.assertEqual(rig.workflow.last_result["result"], "aborted")
                self.assertEqual(rig.workflow.last_result["axis_results"]["x"]["trials"], [])
                self.assert_restored(rig)

    def test_prior_completed_axis_and_paths_survive_fault_on_next_axis(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            rig.home_results.extend([{}, {}, {}, CommandError("sensor fault")])
            with self.assertRaisesRegex(CommandError, "sensor fault"):
                rig.accuracy(axis="XY")
            result = rig.workflow.last_result
            self.assertEqual(result["result"], "aborted")
            self.assertTrue(result["axis_results"]["x"]["complete"])
            self.assertFalse(result["axis_results"]["y"]["complete"])
            self.assertEqual(len(result["paths"]), 3)
            self.assertTrue(all(Path(item["html"]).is_file() for item in result["paths"]))
            self.assertIn("summary_paths", result)
            self.assert_restored(rig)

    def test_stale_result_or_cross_trial_sensor_times_cannot_be_reused(self):
        for reuse in ("record", "clock"):
            with self.subTest(reuse=reuse), tempfile.TemporaryDirectory() as temp:
                rig = AccuracyRig({"charts_directory": temp})
                original = rig.stock_home
                def stale(gcmd):
                    if not rig.home_calls:
                        original(gcmd)
                    elif reuse == "clock":
                        rig.sensor_time -= 1.0
                        original(gcmd)
                rig.stock_home = stale
                with self.assertRaisesRegex(CommandError, "new accelerometer|current detector"):
                    rig.accuracy()
                self.assertEqual(rig.workflow.last_result["result"], "aborted")
                self.assertEqual(rig.workflow.last_result["axis_results"]["x"]["trials"], [])
                self.assertEqual(len(rig.toolhead.moves), 1)
                self.assert_restored(rig)

    def test_cancel_shutdown_print_change_and_recenter_failure_cannot_continue_or_complete(self):
        for ending in ("cancel", "shutdown", "printing", "recenter"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as temp:
                rig = AccuracyRig({"charts_directory": temp})
                original = rig.toolhead.wait_moves
                def interrupted_wait():
                    original()
                    if not rig.toolhead.moves:
                        return
                    if ending == "cancel":
                        rig.workflow.cmd_CANCEL(GCmd())
                    elif ending == "shutdown":
                        rig.shutdown = True
                        for callback in rig.handlers["klippy:shutdown"]:
                            callback()
                    elif ending == "printing":
                        rig.print_state = "printing"
                    else:
                        raise CommandError("synthetic recenter failure")
                rig.toolhead.wait_moves = interrupted_wait
                logs = self.assertLogs(level="ERROR") if ending == "recenter" else contextlib.nullcontext()
                with logs, self.assertRaises(CommandError):
                    rig.accuracy()
                self.assertEqual(rig.scripts, ["G28 X"])
                self.assertEqual(len(rig.toolhead.moves), 1)
                result = rig.workflow.last_result
                self.assertIn(result["result"], ("cancelled", "aborted"))
                self.assertFalse(result["axis_results"]["x"]["complete"])
                self.assert_restored(rig)

    def test_cancel_during_capture_setup_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            original = rig.start_capture
            def cancel_start():
                original()
                rig.workflow.cmd_CANCEL(GCmd())
            rig.owner.start_capture = cancel_start
            with self.assertRaisesRegex(CommandError, "cancelled or interrupted"):
                rig.accuracy()
            self.assertEqual(rig.scripts, [])
            self.assertEqual(rig.toolhead.moves, [])
            self.assertEqual(rig.workflow.last_result["result"], "cancelled")
            self.assert_restored(rig)

    def test_export_failure_preserves_trials_partial_status_and_existing_capture_paths(self):
        for target in ("capture", "summary"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                rig = AccuracyRig({"charts_directory": temp})
                writer = workflow_module.write_capture
                count = 0
                def fail_second(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        raise OSError("synthetic capture export failure")
                    return writer(*args, **kwargs)
                with mock.patch.object(workflow_module, "write_capture", side_effect=fail_second) \
                        if target == "capture" else mock.patch.object(
                            workflow_module, "write_accuracy", side_effect=OSError("synthetic summary export failure")):
                    logs = self.assertLogs(level="ERROR") if target == "summary" else contextlib.nullcontext()
                    with logs, self.assertRaises(Exception):
                        rig.accuracy()
                result = rig.workflow.last_result
                self.assertEqual(result["result"], "aborted")
                self.assertEqual(len(result["axis_results"]["x"]["trials"]), 1 if target == "capture" else 2)
                self.assertEqual(len(result["paths"]), 1 if target == "capture" else 3)
                self.assertTrue(all(Path(paths["json"]).is_file() for paths in result["paths"]))
                if target == "summary":
                    self.assertIn("chart_error", result)
                else:
                    self.assertIn("summary_paths", result)
                    self.assertEqual(result["axis_results"]["x"]["capture_paths"], [None])
                self.assert_restored(rig)

    def test_final_restoration_failure_is_aborted_before_summary_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            original = rig.owner._end_session
            def fail_final_restore():
                original()
                if rig.capture_stops == 3:
                    raise CommandError("synthetic final restoration failure")
            rig.owner._end_session = fail_final_restore
            with self.assertRaisesRegex(CommandError, "final restoration failure"):
                rig.accuracy()
            result = rig.workflow.last_result
            self.assertEqual(result["result"], "aborted")
            self.assertIn("cleanup failed", result["reason"])
            self.assertEqual(result["axis_results"]["x"]["statistics"]["count"], 2)
            self.assertTrue(result["axis_results"]["x"]["complete"])
            metadata = json.loads(Path(result["summary_paths"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["result"], "aborted")
            self.assert_restored(rig)

    def test_status_and_results_retain_metadata_without_raw_capture_arrays(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = AccuracyRig({"charts_directory": temp})
            active = []
            rig.on_home = lambda axis: active.append(rig.workflow.get_status(0)["active"])
            rig.accuracy()
            self.assertEqual(active[-1]["sample_index"], 2)
            self.assertEqual(active[-1]["fixed_thresholds_g"], {"x": 8.0})
            self.assertEqual(active[-1]["completed_samples"], {"x": 1})
            self.assertEqual(active[-1]["configured_homing_speeds_mm_s"], {"x": 100.0})
            self.assertNotIn("candidate", active[-1])
            self.assertNotIn("successes", active[-1])
            self.assertNotIn("repeatability_limit_mm", active[-1])
            encoded = json.dumps(rig.workflow.get_status(0), allow_nan=False)
            self.assertNotIn('"samples":', encoded)
            self.assertNotIn('"_record":', encoded)
            self.assertNotIn('"detector":', encoded)
            self.assertNotIn("action:prompt_", "\n".join(rig.output))


class DetectorCaptureProvenanceTests(unittest.TestCase):
    def test_previous_pass_cannot_supply_current_capture_reference(self):
        obj = make_homing()
        previous, _trigger = arm_axis(obj, "x", 100, 1000)
        previous.bases = (0.0, 0.0, counts(1))
        obj.attempt, obj.last_pass = None, previous
        obj.start_capture()
        obj._capture_batch(batch(150, [(counts(0.2), 0, counts(1))] * 8))
        current, _trigger = arm_axis(obj, "x", 200, 1000)
        current.bases = (counts(0.5), 0.0, counts(1))
        current.record = {"axis": "x"}
        obj._capture_batch(batch(200, [(counts(0.5), 0, counts(1))] * 8))
        captured = obj.stop_capture()
        self.assertEqual(captured["baseline_g"][0], 0.5)
        self.assertIs(captured["_record"], current.record)
        self.assertEqual(captured["detector"]["attempt_count"], 1)
        self.assertIsNone(obj.capture_attempt)
        self.assertEqual(obj.capture_attempts, 0)

    def test_negative_permuted_native_mapping_keeps_machine_frame_and_exact_values(self):
        obj = make_homing(axes_map=((1, -1), (0, 1), (2, 1)), threshold=0.001)
        obj.start_capture()
        current, _trigger = arm_axis(obj, "x", 200, 1000)
        current.bases = (0.0, 0.0, counts(1))
        current.baseline = 0.0
        current.effective_speed, current.effective_accel = 37.0, 4000.0
        params = batch(200, [(0, 1, counts(1))] * 8)
        obj._onset(current, params, [1] * 8, 8)
        current.record = {"axis": "x"}
        obj._capture_batch(params)
        capture = obj.stop_capture()
        detector = capture["detector"]
        self.assertEqual(capture["samples"][0][1], -0.0078125)
        self.assertEqual(detector["onset_g"], -0.0078125)
        self.assertEqual(detector["impact_peak_g"], 0.0078125)
        self.assertEqual(detector["direction"], -1)
        self.assertEqual(detector["threshold_g"], 0.001)
        self.assertEqual(detector["homing_speed_mm_s"], 37.0)
        self.assertIsNone(obj.capture_attempt)

    def test_multi_attempt_capture_is_explicit_and_pointer_is_released(self):
        obj = make_homing()
        obj.start_capture()
        first, _trigger = arm_axis(obj, "x", 100, 1000)
        first.bases = (0, 0, counts(1))
        second, _trigger = arm_axis(obj, "x", 200, 1000)
        second.bases = (0, 0, counts(1))
        obj._capture_batch(batch(200, [(0, 0, counts(1))] * 8))
        capture = obj.stop_capture()
        self.assertEqual(capture["detector"]["attempt_count"], 2)
        self.assertIsNone(obj.capture_attempt)


if __name__ == "__main__":
    unittest.main()
