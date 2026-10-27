"""Offline PNG tests. Every accelerometer sample is SIMULATED."""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import accel_homing_charts as charts


def png_bytes(width=24, height=16):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = (b"\x00" + b"\x20\x50\x80\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class RecordingRenderer:
    reactor = None

    def __init__(self):
        self.payloads = []

    def render(self, payload, check_running=None):
        self.payloads.append(copy.deepcopy(payload))
        if check_running is not None:
            check_running()
        return png_bytes()


def simulated_axis(directory, run_id, axis="x", travels=(175.0, 175.2), requested=None,
                   attempt_start=1, threshold=1.005, renderer=None):
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
                                     capture, record, simulated=True, renderer=renderer or RecordingRenderer())
        if index == 0:
            item["discovery"], item["discovery_capture_paths"] = record, paths
        else:
            item["trials"].append(record)
            item["capture_paths"].append(paths)
    return item

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


class PortablePNGTests(unittest.TestCase):
    def test_capture_is_completed_png_only_with_precise_measurements_and_timestamp(self):
        capture = {"samples": [(1., .2, -.1, 1.), (1.001, .203, -.104, 1.002)],
                   "baseline_g": (.2, -.1, 1.), "dropped": 0}
        record = {"threshold_g": .001, "direction": 0, "onset_time_s": 1.001,
                  "onset_g": -.00125, "impact_peak_g": .004, "travel_mm": 175.}
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            paths = charts.write_capture(temp, "private_UUID_should_not_be_visible", "x", capture, record,
                                         simulated=True, renderer=renderer)
            self.assertEqual(set(paths), {"png"})
            image = Path(paths["png"])
            self.assertTrue(image.is_file())
            self.assertEqual(charts._check_png(image.read_bytes()), (24, 16))
            self.assertRegex(image.name, r"accel_homing_x_home_\d{8}_\d{6}_\d{6}UTC\.png")
            self.assertNotIn("UUID", image.name)
            self.assertEqual([path.suffix for path in Path(temp).rglob("*") if path.is_file()], [".png"])
            payload = renderer.payloads[0]
            self.assertEqual(payload["record"]["threshold_g"], .001)
            self.assertIn("UTC", payload["timestamp"])
            self.assertTrue(payload["created_utc"].endswith("+00:00"))
            self.assertIn(payload["filename_time"], image.name)
            self.assertEqual(paths.receipt.png_sha256, hashlib.sha256(image.read_bytes()).hexdigest())
            public = json.dumps(paths)
            self.assertNotIn("receipt", public)
            self.assertNotIn("trace", public)
            self.assertNotIn("record_json", public)
            with self.assertRaises(AttributeError):
                paths.receipt.png_path = "changed"

    def test_minmax_envelope_retains_every_axis_signed_peaks(self):
        samples = [(index / 10000., 0., 0., 1.) for index in range(20001)]
        samples[1234] = (.1234, 5., -7., 10.)
        samples[1235] = (.1235, -6., 8., -9.)
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            paths = charts.write_capture(temp, "extrema", "x",
                {"samples": samples, "baseline_g": (0., 0., 1.)}, {}, renderer=renderer)
            trace = renderer.payloads[0]["trace"]
            self.assertLessEqual(len(trace), 4096)
            for index in (1, 2, 3):
                expected = [row[index] - (1. if index == 3 else 0.) for row in samples]
                self.assertEqual(min(row[index] for row in trace), min(expected))
                self.assertEqual(max(row[index] for row in trace), max(expected))
                self.assertEqual(min(row[index] for row in paths.receipt.trace), min(expected))
                self.assertEqual(max(row[index] for row in paths.receipt.trace), max(expected))
            self.assertLessEqual(len(paths.receipt.trace), 512)

    def test_frozen_clock_collisions_never_overwrite_completed_png(self):
        clock = {"created_utc": "2026-10-26T23:00:00.123456+00:00",
                 "timestamp": "2026-10-26 23:00:00.123456 UTC",
                 "filename_time": "20261026_230000_123456UTC"}
        capture = {"samples": [(0., 0., 0., 1.), (.1, .1, 0., 1.)], "baseline_g": (0., 0., 1.)}
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(charts, "_clock", return_value=clock):
            images = [charts.write_capture(temp, "same", "x", capture, {}, renderer=RecordingRenderer())["png"]
                      for _index in range(3)]
            self.assertEqual(len(set(images)), 3)
            self.assertTrue(all(Path(image).read_bytes() == png_bytes() for image in images))
            self.assertEqual(len([path for path in Path(temp).rglob("*") if path.is_file()]), 3)

    def test_faults_nonfinite_baselines_and_unserializable_records_leave_no_chart(self):
        original = {"samples": [(1., 0., 0., 1.), (1.1, .2, .1, 1.)], "baseline_g": (0., 0., 1.)}
        with tempfile.TemporaryDirectory() as temp:
            for name in ("fault", "overflow", "time", "nan", "baseline", "record"):
                with self.subTest(name=name):
                    capture, record = copy.deepcopy(original), {}
                    if name == "fault": capture["fault"] = "sensor_error"
                    elif name == "overflow": capture["dropped"] = 1
                    elif name == "time": capture["samples"][1] = capture["samples"][0]
                    elif name == "nan": capture["samples"][1] = (1.1, float("nan"), 0., 1.)
                    elif name == "baseline": capture["baseline_g"] = (0., 0., float("inf"))
                    elif name == "record": record["bad"] = object()
                    with self.assertRaises((ValueError, TypeError)):
                        charts.write_capture(temp, name, "x", capture, record, renderer=RecordingRenderer())
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_cancellation_after_render_prevents_publication_and_cleans_staging(self):
        calls = []
        def check():
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("cancelled before publication")
        capture = {"samples": [(0., 0., 0., 1.), (.1, .1, 0., 1.)], "baseline_g": (0., 0., 1.)}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                charts.write_capture(temp, "cancel", "x", capture, {}, renderer=RecordingRenderer(),
                                     check_running=check)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_png_integrity_rejects_crc_trailing_duplicate_header_and_oversize(self):
        image = png_bytes()
        bad_crc = bytearray(image)
        bad_crc[45] ^= 1
        duplicate_header = image[:33] + image[8:]
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        oversized = image[:8] + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 5000, 8, 6, 0, 0, 0)) + image[33:]
        for bad in (bytes(bad_crc), image + b"tail", duplicate_header, oversized, image[:-4]):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                charts._check_png(bad)

    def test_real_isolated_worker_preflight_leaves_parent_plotting_modules_unloaded(self):
        before = set(sys.modules)
        self.assertTrue(charts.preflight_renderer())
        newly_loaded = set(sys.modules) - before
        self.assertFalse(any(name.split(".")[0] in ("matplotlib", "numpy", "PIL") for name in newly_loaded))

    def test_worker_timeout_is_bounded_and_cleans_all_private_temporary_files(self):
        folders = []
        temporary_directory = charts.tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            context = temporary_directory(*args, **kwargs)
            folders.append(Path(context.name))
            return context
        with mock.patch.object(charts.tempfile, "TemporaryDirectory", side_effect=temporary):
            with self.assertRaisesRegex(ValueError, "deadline"):
                charts.preflight_renderer(charts.PNGRenderer(timeout_s=.001))
        self.assertTrue(folders)
        self.assertTrue(all(not folder.exists() for folder in folders))

    def test_dependency_directory_is_specific_to_prefix_and_python_abi(self):
        one = charts.default_dependency_directory("first-python", "cpython-313")
        self.assertNotEqual(one, charts.default_dependency_directory("second-python", "cpython-313"))
        self.assertNotEqual(one, charts.default_dependency_directory("first-python", "cpython-312"))
        self.assertIn("cpython-313", one.name)
        self.assertEqual(one.parent.name, ".plot-deps")


class AccuracyPNGTests(unittest.TestCase):
    def test_complete_per_axis_pngs_retain_population_stats_and_actual_traces(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            axes = {"x": simulated_axis(temp, "accuracy", travels=(175., 175.2), renderer=renderer),
                    "y": simulated_axis(temp, "accuracy", "y", (176., 176.4), attempt_start=4,
                                        renderer=renderer)}
            before = set(Path(temp).iterdir())
            checked = charts.validate_accuracy(temp, "accuracy", axes, simulated=True)
            self.assertEqual(set(Path(temp).iterdir()), before)
            paths = charts.write_accuracy(temp, "accuracy", axes, "complete", simulated=True,
                                          renderer=renderer)
            self.assertEqual(set(paths), {"x_png", "y_png"})
            stats = checked["x"]["statistics"]
            self.assertAlmostEqual(stats["mean_mm"], 175.1)
            self.assertAlmostEqual(stats["stddev_mm"], .1)
            self.assertAlmostEqual(stats["variance_mm2"], .01)
            summaries = [item for item in renderer.payloads if item["kind"] == "accuracy"]
            self.assertEqual(len(summaries), 2)
            self.assertEqual(len(summaries[0]["traces"]), 2)
            self.assertEqual(summaries[0]["trials"][0]["threshold_g"], 1.005)
            self.assertNotIn("receipt", json.dumps(axes))
            self.assertTrue(all(path.suffix == ".png" for path in Path(temp).rglob("*") if path.is_file()))

    def test_partial_summary_has_honest_failure_and_no_unearned_variance(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "partial", travels=(175.,), requested=3, renderer=renderer)
            item["capture_paths"][0] = None
            paths = charts.write_accuracy(temp, "partial", {"x": item}, "aborted",
                                          reason="Synthetic export failure", simulated=True, renderer=renderer)
            payload = renderer.payloads[-1]
            self.assertEqual(payload["result"], "aborted")
            self.assertIsNone(payload["statistics"]["variance_mm2"])
            self.assertEqual(payload["traces"], [])
            self.assertTrue(Path(paths["x_png"]).is_file())

    def test_immutable_receipt_record_digest_png_bytes_and_path_are_checked(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "bound", renderer=renderer)
            for name in ("plain_dict", "record", "path", "bytes"):
                with self.subTest(name=name):
                    changed = copy.deepcopy(item)
                    original_png = Path(item["capture_paths"][0]["png"])
                    original_bytes = original_png.read_bytes()
                    if name == "plain_dict": changed["capture_paths"][0] = dict(changed["capture_paths"][0])
                    elif name == "record": changed["trials"][0]["threshold_g"] += .1
                    elif name == "path": changed["capture_paths"][0]["png"] = item["capture_paths"][1]["png"]
                    elif name == "bytes": original_png.write_bytes(original_bytes + b"changed")
                    try:
                        with self.assertRaises(ValueError):
                            charts.validate_accuracy(temp, "bound", {"x": changed}, simulated=True)
                    finally:
                        original_png.write_bytes(original_bytes)

    def test_accuracy_validation_yields_between_pngs_and_detects_cancel_promptly(self):
        renderer = RecordingRenderer()
        class Reactor:
            def __init__(self): self.pauses = []
            def monotonic(self): return 0.
            def pause(self, when): self.pauses.append(when)
        renderer.reactor = Reactor()
        with tempfile.TemporaryDirectory() as temp:
            item = simulated_axis(temp, "cancel", renderer=renderer)
            calls = []
            def check():
                calls.append(True)
                if len(calls) == 5: raise RuntimeError("cancelled while checking PNGs")
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                charts.validate_accuracy(temp, "cancel", {"x": item}, simulated=True,
                                         renderer=renderer, check_running=check)
            self.assertTrue(renderer.reactor.pauses)
            self.assertLessEqual(len(calls), 5)

    def test_maximum_xy_accuracy_keeps_individual_pngs_and_bounded_overviews(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            travels = tuple(175. + index * .001 for index in range(50))
            axes = {"x": simulated_axis(temp, "maximum", travels=travels, renderer=renderer),
                    "y": simulated_axis(temp, "maximum", "y", travels, attempt_start=52, renderer=renderer)}
            paths = charts.write_accuracy(temp, "maximum", axes, "complete", simulated=True, renderer=renderer)
            self.assertEqual(len(paths), 2)
            payloads = [item for item in renderer.payloads if item["kind"] == "accuracy"]
            self.assertTrue(all(len(item["traces"]) == 50 for item in payloads))
            self.assertTrue(all(len(trace) <= 512 for item in payloads for trace in item["traces"]))
            files = [path for path in Path(temp).rglob("*") if path.is_file()]
            self.assertEqual(len(files), 104)
            self.assertTrue(all(path.suffix == ".png" for path in files))


class NoisePNGTests(unittest.TestCase):
    def test_three_memory_only_windows_produce_one_png_with_separate_candidates(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "noise")
            self.assertEqual(list(Path(temp).iterdir()), [])
            self.assertTrue(all(isinstance(item["capture_paths"], charts.CapturePaths)
                                and not item["capture_paths"] for item in iterations))
            paths = charts.write_noise(temp, "noise", iterations, recommendation, "complete",
                                       simulated=True, renderer=renderer)
            self.assertEqual(set(paths), {"png"})
            self.assertEqual(len([path for path in Path(temp).rglob("*") if path.is_file()]), 1)
            payload = renderer.payloads[-1]
            self.assertEqual(len(payload["traces"]), 3)
            self.assertEqual(payload["recommendation"]["noise_peak_g"]["x"], 2.)
            self.assertEqual(payload["recommendation"]["start_threshold_g"], {"x": 2.5, "y": 1.5})
            for item in iterations:
                record = item["record"]
                self.assertAlmostEqual(record["noise_rms_g"]["x"], record["noise_peak_g"]["x"] * math.sqrt(2 / 51))
                self.assertNotEqual(record["baseline_g"]["z"], 0.)
            self.assertNotIn("trace", json.dumps(iterations))

    def test_over_ceiling_and_unknown_range_keep_one_png_without_seeds(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, ceiling, full_scale in (("ceiling", 1., 16.), ("unknown", None, None)):
                with self.subTest(name=name):
                    iterations, recommendation = simulated_noise_run(temp, name, ceiling=ceiling, full_scale=full_scale)
                    renderer = RecordingRenderer()
                    paths = charts.write_noise(temp, name, iterations, recommendation, "complete",
                                               simulated=True, renderer=renderer)
                    self.assertTrue(Path(paths["png"]).is_file())
                    self.assertEqual(renderer.payloads[-1]["recommendation"]["start_threshold_g"],
                                     {"x": None, "y": None})

    def test_incomplete_or_faulted_window_cannot_recommend_and_failure_is_png_only(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "partial", peaks=(.1, .2))
            with self.assertRaisesRegex(ValueError, "three valid"):
                charts.write_noise(temp, "partial", iterations, recommendation, "complete", renderer=renderer)
            with self.assertRaisesRegex(ValueError, "cannot recommend"):
                charts.write_noise(temp, "partial", iterations, recommendation, "failed", renderer=renderer)
            paths = charts.write_noise(temp, "partial", iterations, None, "failed",
                                       reason="Synthetic sensor fault", simulated=True, renderer=renderer)
            self.assertEqual(renderer.payloads[-1]["result"], "failed")
            self.assertIsNone(renderer.payloads[-1]["recommendation"])
            self.assertTrue(Path(paths["png"]).is_file())
            item, capture = simulated_noise_window(temp, "source", 1)
            capture["fault"] = "saturated"
            with self.assertRaises(ValueError):
                charts.write_noise_capture(temp, "source", 1, capture, item["record"], simulated=True)

    def test_noise_requires_immutable_identity_context_and_disjoint_windows(self):
        renderer = RecordingRenderer()
        with tempfile.TemporaryDirectory() as temp:
            iterations, recommendation = simulated_noise_run(temp, "identity")
            for name in ("plain_dict", "identity", "context", "overlap", "metric"):
                with self.subTest(name=name):
                    changed = copy.deepcopy(iterations)
                    if name == "plain_dict": changed[0]["capture_paths"] = {}
                    elif name == "identity": changed[1]["record"]["noise_iteration"] = 1
                    elif name == "context": changed[1]["record"]["backend"] = "changed"
                    elif name == "overlap":
                        changed[1]["record"]["first_sample_time_s"] = changed[0]["record"]["first_sample_time_s"]
                        changed[1]["record"]["last_sample_time_s"] = changed[0]["record"]["last_sample_time_s"]
                    elif name == "metric": changed[0]["record"]["noise_peak_g"]["x"] += .1
                    with self.assertRaises(ValueError):
                        charts.write_noise(temp, "identity", changed, recommendation, "complete",
                                           simulated=True, renderer=renderer)


if __name__ == "__main__":
    unittest.main()
