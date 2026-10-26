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


if __name__ == '__main__':
    unittest.main()
