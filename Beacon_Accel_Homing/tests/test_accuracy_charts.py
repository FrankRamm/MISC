"""Offline report checks. Every sample in this module is SIMULATED."""
import copy
import csv
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import accel_homing_charts as charts


def simulated_axis(directory, run_id, axis="x", travels=(175.0, 175.2), requested=None,
                   attempt_start=1, threshold=1.005):
    """Publish unmistakably synthetic per-home captures in the production shape."""
    requested = len(travels) if requested is None else requested
    item = {"requested_samples": requested, "complete": len(travels) == requested,
            "discovery": None, "trials": [], "statistics": None, "capture_paths": [],
            "discovery_capture_paths": None, "rejected": None}
    for index, travel in enumerate((174.5,) + tuple(travels)):
        attempt = attempt_start + index
        record = {"run_id": run_id, "axis": axis, "workflow_attempt": attempt,
                  "workflow_phase": "discovery" if index == 0 else "measurement",
                  "accuracy_sample": index, "result": "impact", "travel_mm": travel,
                  "onset_g": -1.125, "impact_peak_g": 2.75, "threshold_g": threshold,
                  "workflow_threshold_g": threshold, "direction": -1, "onset_time_s": 1.002}
        impact = [[1.002, .2, -.1, 1.], [1.003, .2, -.1, 1.]]
        channel = 1 if axis == "x" else 2
        impact[0][channel] -= 1.125
        impact[1][channel] -= 2.75
        capture = {"samples": [(1.0, .2, -.1, 1.), (1.001, .2, -.1, 1.)] + impact,
                   "baseline_g": (.2, -.1, 1.), "dropped": 0}
        paths = charts.write_capture(directory, "%s_a%03d" % (run_id, attempt), axis,
                                     capture, record, simulated=True)
        if index == 0:
            item["discovery"], item["discovery_capture_paths"] = record, paths
        else:
            item["trials"].append(record)
            item["capture_paths"].append(paths)
    return item


class AccuracyChartTests(unittest.TestCase):
    def test_complete_report_contains_population_stats_and_embedded_actual_traces(self):
        with tempfile.TemporaryDirectory() as temp:
            axes = {"x": simulated_axis(temp, "simulated", travels=(175.0, 175.2)),
                    "y": simulated_axis(temp, "simulated", "y", (176.0, 176.4), attempt_start=4)}
            before = set(Path(temp).iterdir())
            checked = charts.validate_accuracy(temp, "simulated", axes, simulated=True)
            self.assertTrue(checked["x"]["complete"])
            self.assertEqual(set(Path(temp).iterdir()), before)
            paths = charts.write_accuracy(temp, "simulated", axes, "complete", simulated=True)
            metadata = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            stats = metadata["axes"]["x"]["statistics"]
            self.assertEqual(stats["count"], 2)
            self.assertEqual(stats["min_mm"], 175.)
            self.assertEqual(stats["max_mm"], 175.2)
            self.assertAlmostEqual(stats["mean_mm"], 175.1)
            self.assertAlmostEqual(stats["spread_mm"], .2)
            self.assertAlmostEqual(stats["stddev_mm"], .1)
            self.assertAlmostEqual(stats["variance_mm2"], .01)
            self.assertEqual(stats["variance_definition"], "population (divide by N)")
            self.assertFalse(metadata["qualifying_home_in_statistics"])
            self.assertEqual(metadata["travel_resolution_mm"], .001)
            self.assertEqual(metadata["data_source"], "SIMULATED - NOT PRINTER MEASUREMENT")
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertEqual(page.count('<svg '), 7)
            self.assertIn('value="trial-1" selected', page)
            self.assertIn('-1.005 g threshold (configured trigger)', page)
            self.assertIn('detected onset', page)
            self.assertIn('SIMULATED / NOT PRINTER MEASUREMENT', page)
            self.assertIn('not encoder-measured physical stop accuracy', page)
            self.assertIn('Variance (mm²)', page)
            self.assertNotIn('<script src=', page)
            self.assertNotIn('http://', page.replace('http://www.w3.org/2000/svg', ''))
            self.assertIn('document.getElementById("trial")', page)
            self.assertEqual(ET.parse(paths["svg"]).getroot().tag,
                             '{http://www.w3.org/2000/svg}svg')
            with open(paths["csv"], newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(row["included_in_statistics"] == "True" for row in rows), 4)
            self.assertEqual(rows[1]["configured_trigger_threshold_g"], "1.005")

    def test_aborted_single_home_has_no_variance_or_full_run_claim_and_escapes_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "partial", travels=(175.,), requested=3)
            item["capture_paths"][0] = None
            axes = {"x": item, "y": {"requested_samples": 3, "complete": False,
                    "discovery": None, "trials": [], "statistics": None, "capture_paths": []}}
            paths = charts.write_accuracy(temp, "partial", axes, "aborted",
                                          reason='<script>alert("failure")</script>', simulated=True)
            metadata = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            stats = metadata["axes"]["x"]["statistics"]
            self.assertIsNone(stats["stddev_mm"])
            self.assertIsNone(stats["variance_mm2"])
            self.assertIsNone(metadata["axes"]["y"]["statistics"])
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn('INCOMPLETE / ABORTED', page)
            self.assertIn('1 / 3', page)
            self.assertIn('unavailable', page)
            self.assertIn('capture unavailable', page)
            self.assertNotIn('COMPLETE: all requested', page)
            self.assertIn('&lt;script&gt;alert(', page)
            self.assertNotIn('<script>alert(', page)

    def test_complete_requires_all_captures_and_does_not_overwrite_a_previous_report(self):
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "run")
            before = set(Path(temp).iterdir())
            missing = copy.deepcopy(item)
            missing["capture_paths"][1] = None
            with self.assertRaisesRegex(ValueError, "every requested valid capture"):
                charts.write_accuracy(temp, "run", {"x": missing}, "complete", simulated=True)
            self.assertEqual(set(Path(temp).iterdir()), before)
            paths = charts.write_accuracy(temp, "run", {"x": item}, "complete", simulated=True)
            page = Path(paths["html"]).read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                charts.write_accuracy(temp, "run", {"x": item}, "complete", simulated=True)
            self.assertEqual(Path(paths["html"]).read_bytes(), page)

    def test_rejects_path_escape_foreign_metadata_nonfinite_and_false_statistics(self):
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "run")
            for mutation in ("path", "record", "nonfinite", "statistics", "threshold", "identity"):
                altered = copy.deepcopy(item)
                if mutation == "path":
                    altered["capture_paths"][0]["svg"] = str(Path(__file__).resolve())
                elif mutation == "record":
                    altered["trials"][0]["onset_g"] = -99.
                elif mutation == "nonfinite":
                    altered["trials"][0]["travel_mm"] = float("nan")
                elif mutation == "statistics":
                    altered["statistics"] = {"count": 100}
                elif mutation == "threshold":
                    altered["trials"][0]["threshold_g"] = 1.006
                else:
                    altered["trials"][0]["workflow_attempt"] = altered["discovery"]["workflow_attempt"]
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    charts.write_accuracy(temp, "run", {"x": altered}, "complete", simulated=True)
                self.assertFalse((Path(temp) / 'accel_homing_accuracy_run').exists())
            with self.assertRaisesRegex(ValueError, "Invalid accuracy run ID"):
                charts.write_accuracy(temp, "../escape", {"x": item}, "complete", simulated=True)

    def test_rejects_faulted_capture_or_fake_real_provenance_without_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "run")
            with self.assertRaisesRegex(ValueError, "metadata"):
                charts.write_accuracy(temp, "run", {"x": item}, "complete")
            path = Path(item["capture_paths"][0]["json"])
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata["capture"]["fault"] = "synthetic sensor error"
            path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata"):
                charts.write_accuracy(temp, "run", {"x": item}, "aborted", simulated=True)
            self.assertFalse((Path(temp) / 'accel_homing_accuracy_run').exists())

    def test_unsafe_svg_and_size_limit_fail_atomically_preserving_capture_files(self):
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "run")
            svg_path = Path(item["capture_paths"][0]["svg"])
            original = svg_path.read_text(encoding="utf-8")
            svg_path.write_text(original.replace('</svg>', '<script>alert(1)</script></svg>'), encoding="utf-8")
            before = set(Path(temp).iterdir())
            with self.assertRaisesRegex(ValueError, "unsupported SVG"):
                charts.write_accuracy(temp, "run", {"x": item}, "complete", simulated=True)
            self.assertEqual(set(Path(temp).iterdir()), before)
            svg_path.write_text(original, encoding="utf-8")
            with mock.patch.object(charts, '_MAX_REPORT_BYTES', 1000):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    charts.write_accuracy(temp, "run", {"x": item}, "complete", simulated=True)
            self.assertEqual(set(Path(temp).iterdir()), before)

    def test_embedded_trace_is_bounded_and_retains_recorded_positive_and_negative_extrema(self):
        with tempfile.TemporaryDirectory() as temp:
            samples = [(i * .0001, math.sin(i * .1), 0., 1.) for i in range(20000)]
            samples[1531] = (samples[1531][0], 11., 0., 1.)
            samples[14519] = (samples[14519][0], -13., 0., 1.)
            paths = charts.write_capture(temp, 'simulated', 'x',
                                         {'samples': samples, 'baseline_g': (0., 0., 1.), 'dropped': 0},
                                         {'threshold_g': .000001, 'direction': -1}, simulated=True)
            original = ET.parse(paths['svg']).getroot()
            reduced = ET.fromstring(charts._embedded_capture_svg(paths['svg']))
            ns = {'s': 'http://www.w3.org/2000/svg'}
            for full, small in zip(original.findall('.//s:polyline', ns), reduced.findall('.//s:polyline', ns)):
                full_values = [float(point.split(',')[1]) for point in full.get('points').split()]
                small_values = [float(point.split(',')[1]) for point in small.get('points').split()]
                self.assertLessEqual(len(small_values), 1024)
                self.assertEqual(min(small_values), min(full_values))
                self.assertEqual(max(small_values), max(full_values))
            self.assertIn('-1e-06 g threshold', Path(paths['svg']).read_text(encoding='utf-8'))

    def test_single_capture_refuses_sensor_fault(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "capture fault"):
                charts.write_capture(temp, 'fault', 'x', {'samples': [(1., 0., 0., 1.)],
                                     'baseline_g': (0., 0., 1.), 'fault': 'data_error'}, {})
            self.assertEqual(list(Path(temp).iterdir()), [])


def simulated_noise_window(directory, run_id, iteration, peak=0.2, sensor="adxl345 toolhead", full_scale=16.0):
    """A one-second stationary fixture, with a separate mean on every window."""
    origin = 10.0 + iteration * 2.0
    resting = (.2 + iteration * .1, -.1 + iteration * .2, 1.0 + iteration * .01)
    samples = []
    for index in range(51):
        pulse = peak if index == 25 else -peak if index == 26 else 0.0
        samples.append((origin + index / 50.0, resting[0] + pulse,
                        resting[1] - pulse / 2.0, resting[2] + pulse / 4.0))
    baseline = tuple(math.fsum(row[index] for row in samples) / len(samples) for index in (1, 2, 3))
    values = {axis: [row[index] - baseline[index - 1] for row in samples]
              for index, axis in enumerate("xyz", 1)}
    record = {"run_id": run_id, "noise_iteration": iteration,
              "duration_s": samples[-1][0] - samples[0][0], "sample_count": len(samples),
              "first_sample_time_s": samples[0][0], "last_sample_time_s": samples[-1][0],
              "baseline_g": dict(zip("xyz", baseline)),
              "noise_peak_g": {axis: max(abs(value) for value in rows) for axis, rows in values.items()},
              "noise_peak_to_peak_g": {axis: max(rows) - min(rows) for axis, rows in values.items()},
              "noise_rms_g": {axis: math.sqrt(math.fsum(value * value for value in rows) / len(rows))
                              for axis, rows in values.items()},
              "sensor": sensor, "backend": "generic", "full_scale_g": full_scale,
              "axis_context": "machine-mapped XYZ after configured axes_map"}
    capture = {"samples": samples, "baseline_g": baseline, "dropped": 0, "fault": None}
    paths = charts.write_noise_capture(directory, run_id, iteration, capture, record, simulated=True)
    return {"record": record, "capture_paths": paths}, capture


def simulated_noise_run(directory, run_id, peaks=(.1, .3, 2.0), ceiling=12.0, sensor="adxl345 toolhead", full_scale=16.0):
    iterations = [simulated_noise_window(directory, run_id, index, peak, sensor, full_scale)[0]
                  for index, peak in enumerate(peaks, 1)]
    largest = {axis: max(item["record"]["noise_peak_g"][axis] for item in iterations) for axis in "xyz"}
    candidates, unavailable = {}, []
    for axis in "xy":
        candidate = .5 + math.ceil((max(.5, largest[axis] * 1.2, largest[axis] + .1) - .5) / .5) * .5
        candidates[axis] = candidate if ceiling is not None and candidate <= ceiling else None
        if ceiling is None or candidate > ceiling:
            unavailable.append(axis)
    recommendation = {"source": "stationary_noise", "method": "initial_candidate_heuristic",
                      "noise_peak_g": largest, "start_threshold_g": candidates,
                      "margin_factor": 1.2, "margin_g": .1, "candidate_step_g": .5,
                      "configured_start_g": .5, "threshold_limit_g": ceiling,
                      "unavailable_axes": unavailable, "clamped": False}
    return iterations, recommendation


class NoiseChartTests(unittest.TestCase):
    def test_three_windows_preserve_raw_signed_xyz_metrics_and_distinct_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "noise")
            paths = charts.write_noise(temp, "noise", iterations, recommendation, "complete", simulated=True)
            metadata = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["result"], "complete")
            self.assertEqual(metadata["requested_windows"], 3)
            self.assertEqual(metadata["requested_window_duration_s"], 1.)
            self.assertEqual(metadata["recommendation"]["start_threshold_g"]["x"], 2.5)
            self.assertEqual(metadata["recommendation"]["noise_peak_g"]["x"], 2.)
            self.assertEqual(metadata["data_source"], "SIMULATED - NOT PRINTER MEASUREMENT")
            self.assertNotIn(str(Path(temp).resolve()), Path(paths["json"]).read_text(encoding="utf-8"))
            for iteration, item in enumerate(iterations, 1):
                record = metadata["iterations"][iteration - 1]["record"]
                self.assertEqual(record["noise_iteration"], iteration)
                self.assertEqual(record["sample_count"], 51)
                self.assertAlmostEqual(record["noise_rms_g"]["x"], record["noise_peak_g"]["x"] * math.sqrt(2 / 51))
                with open(item["capture_paths"]["csv"], newline="", encoding="utf-8") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 51)
                self.assertAlmostEqual(float(rows[25]["raw_z_g"]), record["baseline_g"]["z"] + record["noise_peak_g"]["z"])
                self.assertAlmostEqual(float(rows[25]["delta_x_g"]), record["noise_peak_g"]["x"])
                self.assertAlmostEqual(float(rows[26]["delta_x_g"]), -record["noise_peak_g"]["x"])
                self.assertNotIn("threshold (configured trigger)", Path(item["capture_paths"]["svg"]).read_text())
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertEqual(page.count('<svg '), 3)
            self.assertEqual(page.count('<details open>'), 3)
            self.assertIn("NOT a calibrated impact threshold", page)
            self.assertIn("Initial calibration candidate", page)
            self.assertIn("20% or 0.1 g", page)
            self.assertIn("SIMULATED / NOT PRINTER MEASUREMENT", page)
            self.assertIn("motor-enable or motion transients", page)
            self.assertIn("RMS (g)", page)
            self.assertNotIn('<script', page)
            self.assertNotIn('http://', page.replace('http://www.w3.org/2000/svg', ''))
            root = ET.parse(paths["svg"]).getroot()
            self.assertEqual(len(list(root.iter('{http://www.w3.org/2000/svg}polyline'))), 9)
            panels = list(root.iter('{http://www.w3.org/2000/svg}svg'))[1:]
            self.assertEqual(len(panels), 3)
            self.assertTrue(all(panel.get('width') == '1100' and panel.get('height') == '445'
                                for panel in panels))
            with open(paths["csv"], newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 9)
            self.assertEqual(rows[0]["initial_calibration_candidate_g"], "2.5")
            self.assertTrue(all(row["candidate_is_calibrated_impact_threshold"] == "False" for row in rows))

    def test_over_ceiling_keeps_all_three_charts_without_a_downward_clamped_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "loud", peaks=(.1, .3, 2.), ceiling=1.)
            paths = charts.write_noise(temp, "loud", iterations, recommendation, "complete", simulated=True)
            metadata = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertIsNone(metadata["recommendation"]["start_threshold_g"]["x"])
            self.assertIsNone(metadata["recommendation"]["start_threshold_g"]["y"])
            self.assertEqual(metadata["recommendation"]["unavailable_axes"], ["x", "y"])
            self.assertEqual(len(metadata["iterations"]), 3)
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertEqual(page.count('<svg '), 3)
            self.assertIn('unavailable: exceeds calibration ceiling', page)
            recommendation["start_threshold_g"]["x"] = 1.
            with self.assertRaisesRegex(ValueError, 'do not clamp'):
                charts.write_noise(temp, "loud", iterations, recommendation, "complete", simulated=True)

    def test_partial_or_faulted_run_is_explicit_and_cannot_recommend(self):
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "partial", peaks=(.1, .2))
            before = set(Path(temp).iterdir())
            with self.assertRaisesRegex(ValueError, 'three valid windows'):
                charts.write_noise(temp, "partial", iterations, recommendation, "complete", simulated=True)
            with self.assertRaisesRegex(ValueError, 'cannot recommend'):
                charts.write_noise(temp, "partial", iterations, recommendation, "failed", simulated=True)
            self.assertEqual(set(Path(temp).iterdir()), before)
            paths = charts.write_noise(temp, "partial", iterations, None, "failed",
                                       reason='<script>alert("sensor fault")</script>', simulated=True)
            self.assertIsNone(json.loads(Path(paths["json"]).read_text())["recommendation"])
            page = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn('INCOMPLETE / FAILED', page)
            self.assertIn('no calibration candidate is recommended', page)
            self.assertIn('&lt;script&gt;alert(', page)
            self.assertNotIn('<script>alert(', page)
            self.assertNotIn('COMPLETE: all three', page)

    def test_unknown_sensor_range_preserves_windows_without_an_unverifiable_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, 'unknown', ceiling=None, full_scale=None)
            paths = charts.write_noise(temp, 'unknown', iterations, recommendation, 'complete', simulated=True)
            metadata = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
            self.assertIsNone(metadata['recommendation']['threshold_limit_g'])
            self.assertEqual(metadata['recommendation']['start_threshold_g'], {'x': None, 'y': None})
            self.assertEqual(len(metadata['iterations']), 3)
            self.assertIn('unavailable: sensor range unknown', Path(paths['html']).read_text(encoding='utf-8'))
            recommendation['start_threshold_g']['x'] = .5
            with self.assertRaisesRegex(ValueError, 'Unknown sensor range'):
                charts.write_noise(temp, 'unknown', iterations, recommendation, 'complete', simulated=True)

    def test_rejects_bad_measurements_and_record_metadata_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            item, capture = simulated_noise_window(temp, "source", 1)
            for name in ('peak', 'rms', 'count', 'duration', 'fault', 'overflow', 'time', 'nan', 'baseline'):
                with self.subTest(name=name):
                    record, broken = copy.deepcopy(item["record"]), copy.deepcopy(capture)
                    record["run_id"] = name
                    if name == 'peak': record['noise_peak_g']['x'] += .01
                    elif name == 'rms': record['noise_rms_g']['z'] += .01
                    elif name == 'count': record['sample_count'] -= 1
                    elif name == 'duration': record['duration_s'] = .9
                    elif name == 'fault': broken['fault'] = 'saturated'
                    elif name == 'overflow': broken['dropped'] = 1
                    elif name == 'time': broken['samples'][1] = broken['samples'][0]
                    elif name == 'nan': broken['samples'][1] = (12.02, float('nan'), 0., 1.)
                    elif name == 'baseline': broken['baseline_g'] = (0., 0., 0.)
                    with self.assertRaises(ValueError):
                        charts.write_noise_capture(temp, name, 1, broken, record, simulated=True)
                    self.assertFalse((Path(temp) / ('accel_homing_noise_%s_i01' % name)).exists())
            with self.assertRaisesRegex(ValueError, 'already exists'):
                charts.write_noise_capture(temp, 'source', 1, capture, item['record'], simulated=True)

    def test_same_run_identity_sensor_context_and_passive_svg_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "identity", sensor='<img src=x onerror="bad">')
            changed = copy.deepcopy(iterations)
            changed[1]['record']['noise_iteration'] = 1
            with self.assertRaisesRegex(ValueError, 'complete current-run'):
                charts.write_noise(temp, 'identity', changed, recommendation, 'complete', simulated=True)
            changed = copy.deepcopy(iterations)
            changed[1]['record']['backend'] = 'different'
            with self.assertRaisesRegex(ValueError, 'context changed'):
                charts.write_noise(temp, 'identity', changed, recommendation, 'complete', simulated=True)
            changed = copy.deepcopy(iterations)
            changed[1]['record']['first_sample_time_s'] = changed[0]['record']['first_sample_time_s']
            changed[1]['record']['last_sample_time_s'] = changed[0]['record']['last_sample_time_s']
            with self.assertRaisesRegex(ValueError, 'overlap or repeat'):
                charts.write_noise(temp, 'identity', changed, recommendation, 'complete', simulated=True)
            changed = copy.deepcopy(iterations)
            changed[0]['capture_paths']['svg'] = iterations[1]['capture_paths']['svg']
            with self.assertRaisesRegex(ValueError, 'does not belong'):
                charts.write_noise(temp, 'identity', changed, recommendation, 'complete', simulated=True)
            paths = charts.write_noise(temp, 'identity', iterations, recommendation, 'complete', simulated=True)
            page = Path(paths['html']).read_text(encoding='utf-8')
            self.assertIn('&lt;img src=x onerror=', page)
            self.assertNotIn('<img src=', page)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                charts.write_noise(temp, 'identity', iterations, recommendation, 'complete', simulated=True)
            unsafe, _recommendation = simulated_noise_run(temp, 'unsafe')
            svg = Path(unsafe[0]['capture_paths']['svg'])
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unsupported SVG'):
                charts.write_noise(temp, 'unsafe', unsafe, None, 'failed', simulated=True)


if __name__ == '__main__':
    unittest.main()
