# PNG-only accelerometer homing charts. GPLv3.
"""Render completed PNGs in an isolated process; keep provenance in memory."""
import collections
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zlib


_MAX_PNG_BYTES = 32 * 1024 * 1024
_MAX_PNG_PIXELS = 12 * 1024 * 1024
_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_SAMPLES = 500000
_POPULATION = "population (divide by N)"
_NOISE_WINDOWS = 3
_NOISE_WINDOW_SECONDS = 1.0
_Receipt = collections.namedtuple("_Receipt", (
    "kind", "run_id", "axis", "iteration", "record_json", "metadata_json",
    "trace", "png_path", "png_sha256", "simulated"))


class CapturePaths(dict):
    """Public file paths plus a private immutable, non-JSON provenance receipt."""
    __slots__ = ("_receipt",)

    def __init__(self, paths, receipt):
        super().__init__(paths)
        self._receipt = receipt

    @property
    def receipt(self):
        return self._receipt


def default_dependency_directory(prefix=None, cache_tag=None):
    prefix = sys.prefix if prefix is None else prefix
    cache_tag = sys.implementation.cache_tag if cache_tag is None else cache_tag
    if not isinstance(cache_tag, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", cache_tag):
        raise ValueError("Invalid chart Python cache tag")
    identity = hashlib.sha256(os.path.abspath(prefix).encode("utf-8")).hexdigest()[:12]
    return Path(__file__).resolve().parent / ".plot-deps" / (cache_tag + "-" + identity)


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _check_running(check_running):
    if check_running is not None:
        check_running()


def _check_png(data, decode_pixels=True):
    """Check complete bounded, noninterlaced PNG bytes without importing Pillow."""
    if not isinstance(data, bytes) or not 45 <= len(data) <= _MAX_PNG_BYTES \
            or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid or oversized PNG chart")
    offset, header, ended, compressed = 8, None, False, []
    seen_idat = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated PNG chart")
        size, kind = struct.unpack(">I4s", data[offset:offset + 8])
        end = offset + 12 + size
        if end > len(data):
            raise ValueError("Truncated PNG chart chunk")
        chunk = data[offset + 8:offset + 8 + size]
        checksum = struct.unpack(">I", data[offset + 8 + size:end])[0]
        if zlib.crc32(kind + chunk) & 0xffffffff != checksum:
            raise ValueError("PNG chart CRC mismatch")
        if kind == b"IHDR":
            if header is not None or offset != 8 or size != 13:
                raise ValueError("Invalid PNG chart header")
            header = struct.unpack(">IIBBBBB", chunk)
            width, height, depth, color, compression, filtering, interlace = header
            if not width or not height or width * height > _MAX_PNG_PIXELS \
                    or depth != 8 or color not in (0, 2, 4, 6) \
                    or compression or filtering or interlace:
                raise ValueError("Unsupported or oversized PNG chart dimensions")
        elif kind == b"IDAT":
            if header is None:
                raise ValueError("PNG data precedes its header")
            seen_idat = True
            compressed.append(chunk)
        elif kind == b"IEND":
            if size or not seen_idat or end != len(data):
                raise ValueError("Invalid PNG chart ending")
            ended = True
        elif kind[0] & 32 == 0 and kind != b"PLTE":
            raise ValueError("Unsupported critical PNG chart chunk")
        offset = end
        if ended:
            break
    if header is None or not ended:
        raise ValueError("Incomplete PNG chart")
    if not decode_pixels:
        return header[0], header[1]
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[header[3]]
    row_bytes = header[0] * channels + 1
    expected = row_bytes * header[1]
    decoder = zlib.decompressobj()
    raw = decoder.decompress(b"".join(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail \
            or any(raw[index] > 4 for index in range(0, len(raw), row_bytes)):
        raise ValueError("Invalid PNG chart pixel stream")
    return header[0], header[1]


class PNGRenderer:
    """Run only the selected Python plotting worker while yielding to Klippy."""
    def __init__(self, python_executable=None, dependency_directory=None, reactor=None,
                 timeout_s=90.0):
        self.python_executable = str(python_executable or sys.executable)
        self.dependency_directory = str(dependency_directory or default_dependency_directory())
        self.reactor = reactor
        self.timeout_s = float(timeout_s)
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("Invalid PNG rendering deadline")

    def render(self, payload, check_running=None):
        encoded = _json(payload).encode("utf-8")
        if len(encoded) > _MAX_INPUT_BYTES:
            raise ValueError("PNG chart render input exceeds its size limit")
        _check_running(check_running)
        with tempfile.TemporaryDirectory(prefix="accel_homing_png_worker_") as temporary:
            folder = Path(temporary)
            source, target = folder / "input.json", folder / "output.png"
            source.write_bytes(encoded)
            with open(folder / "worker.log", "wb") as log:
                process = subprocess.Popen(
                    [self.python_executable, "-B", "-I", str(Path(__file__).resolve()),
                     "--render-worker", str(source), str(target), self.dependency_directory],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                deadline = time.monotonic() + self.timeout_s
                try:
                    while process.poll() is None:
                        _check_running(check_running)
                        if os.fstat(log.fileno()).st_size > 1024 * 1024:
                            raise ValueError("PNG renderer exceeded its diagnostic-log limit")
                        if time.monotonic() >= deadline:
                            raise ValueError("PNG renderer exceeded its deadline")
                        if self.reactor is None:
                            time.sleep(0.02)
                        else:
                            self.reactor.pause(self.reactor.monotonic() + 0.02)
                    _check_running(check_running)
                    if process.returncode:
                        log.flush()
                        with open(folder / "worker.log", "rb") as detail_stream:
                            detail = detail_stream.read(8000).decode("utf-8", "replace")
                        raise ValueError("PNG renderer failed; plotting dependencies must be available "
                                         "for the selected Klipper Python: " + detail.strip())
                    if not target.is_file() or target.stat().st_size > _MAX_PNG_BYTES:
                        raise ValueError("PNG renderer did not produce a bounded completed image")
                    data = target.read_bytes()
                    _check_png(data, decode_pixels=False)
                    _check_running(check_running)
                    return data
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2.0)


def preflight_renderer(renderer=None, check_running=None):
    """Exercise the actual worker before any chart-producing workflow moves."""
    data = (renderer or PNGRenderer()).render({"kind": "preflight"}, check_running=check_running)
    _check_png(data, decode_pixels=False)
    _check_running(check_running)
    return True


def _clock():
    instant = datetime.datetime.now(datetime.timezone.utc)
    return {"created_utc": instant.isoformat(),
            "timestamp": instant.strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
            "filename_time": instant.strftime("%Y%m%d_%H%M%S_%fUTC")}


def _identity(run_id, axis):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid capture run ID")
    if axis not in ("x", "y"):
        raise ValueError("Capture axis must be x or y")


def _envelope(samples, buckets=512):
    """Keep every bucket's first/last and all signed XYZ extrema."""
    stride = max(1, int(math.ceil(len(samples) / float(buckets))))
    result = []
    for offset in range(0, len(samples), stride):
        bucket = samples[offset:offset + stride]
        chosen = {0, len(bucket) - 1}
        for channel in (1, 2, 3):
            chosen.update((min(range(len(bucket)), key=lambda i: bucket[i][channel]),
                           max(range(len(bucket)), key=lambda i: bucket[i][channel])))
        result.extend(tuple(bucket[index]) for index in sorted(chosen))
    return tuple(result)


def _capture_data(capture, record):
    if not isinstance(capture, dict) or capture.get("dropped", 0) \
            or capture.get("fault") or capture.get("error"):
        raise ValueError("Sensor capture fault or buffer overflow; refusing an incomplete PNG")
    samples = capture.get("samples", ())
    if not isinstance(samples, (tuple, list)) or not 2 <= len(samples) <= _MAX_SAMPLES:
        raise ValueError("No bounded accelerometer capture for this home")
    previous = None
    for sample in samples:
        if not isinstance(sample, (tuple, list)) or len(sample) != 4 \
                or not all(_finite(value) for value in sample) \
                or (previous is not None and sample[0] <= previous):
            raise ValueError("Invalid or non-increasing accelerometer sample")
        previous = sample[0]
    baseline = capture.get("baseline_g")
    if baseline is None:
        arm = record.get("arm_time_s", samples[0][0])
        quiet = [row for row in samples if row[0] < arm]
        if not quiet:
            raise ValueError("No pre-arm baseline for measured acceleration")
        baseline = tuple(math.fsum(row[index] for row in quiet) / len(quiet) for index in (1, 2, 3))
    if not isinstance(baseline, (tuple, list)) or len(baseline) != 3 \
            or not all(_finite(value) for value in baseline):
        raise ValueError("Invalid accelerometer baseline")
    delta = [(row[0] - samples[0][0],) + tuple(row[i + 1] - baseline[i] for i in range(3))
             for row in samples]
    metadata = {"sample_count": len(samples), "baseline_g": list(baseline),
                "first_sample_time_s": samples[0][0], "last_sample_time_s": samples[-1][0],
                "absolute_peak_g": {axis: max(abs(row[index]) for row in delta)
                                    for index, axis in enumerate("xyz", 1)}}
    _json(record)
    return _envelope(delta), metadata


def _publish(directory, label, clock, data, check_running=None):
    _check_png(data, decode_pixels=False)
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = label + "_" + clock["filename_time"]
    with tempfile.TemporaryDirectory(prefix=".accel_homing_png_", dir=str(destination)) as temporary:
        staged = Path(temporary)
        for collision in range(100):
            name = stem if collision == 0 else stem + "_%02d" % collision
            published = destination / name
            if os.path.lexists(published):
                continue
            filename = name + ".png"
            candidate = staged / filename
            candidate.write_bytes(data)
            _check_running(check_running)
            try:
                os.rename(temporary, published)
            except OSError:
                if os.path.lexists(published):
                    candidate.unlink()
                    continue
                raise
            _check_running(check_running)
            return str(published / filename)
    raise ValueError("PNG chart filename collisions exceeded their bounded limit")


def write_capture(directory, run_id, axis, capture, record, simulated=False,
                  renderer=None, check_running=None):
    _identity(run_id, axis)
    trace, metadata = _capture_data(capture, record)
    clock = _clock()
    phase = record.get("workflow_phase")
    sample = record.get("accuracy_sample")
    suffix = ("qualifier" if phase == "discovery" else "home%02d" % sample
              if type(sample) is int and sample > 0 else "home")
    payload = {"kind": "capture", "axis": axis, "record": record, "trace": trace,
               "capture": metadata, "simulated": bool(simulated), **clock}
    data = (renderer or PNGRenderer()).render(payload, check_running=check_running)
    path = _publish(directory, "accel_homing_%s_%s" % (axis, suffix), clock, data, check_running)
    receipt = _Receipt("capture", run_id, axis, record.get("workflow_attempt"),
                       _json(record), _json(metadata), _envelope(trace, 64), path,
                       hashlib.sha256(data).hexdigest(), bool(simulated))
    _check_running(check_running)
    return CapturePaths({"png": path}, receipt)


def _capture_reference(directory, paths, run_id, axis, record, simulated, check_running=None):
    _check_running(check_running)
    if paths is None:
        return None
    expected_id = "%s_a%03d" % (run_id, record["workflow_attempt"])
    if not isinstance(paths, CapturePaths) or set(paths) != {"png"}:
        raise ValueError("Accuracy PNG requires its immutable current-run capture receipt")
    receipt = paths.receipt
    if not isinstance(receipt, _Receipt) or receipt.kind != "capture" \
            or receipt.run_id != expected_id or receipt.axis != axis \
            or receipt.iteration != record["workflow_attempt"] \
            or receipt.record_json != _json(record) or receipt.simulated != bool(simulated):
        raise ValueError("Accuracy PNG capture receipt does not match this current-run record")
    path = paths.get("png")
    if not isinstance(path, str) or path != receipt.png_path:
        raise ValueError("Accuracy PNG capture path does not match its receipt")
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file() or candidate.suffix.lower() != ".png" \
            or candidate.parent.parent != directory \
            or candidate.name != candidate.parent.name + ".png":
        raise ValueError("Accuracy PNG capture path does not belong to this output directory")
    if candidate.stat().st_size > _MAX_PNG_BYTES:
        raise ValueError("Accuracy PNG capture exceeds its size limit")
    data = candidate.read_bytes()
    if hashlib.sha256(data).hexdigest() != receipt.png_sha256:
        raise ValueError("Accuracy PNG capture bytes changed after export")
    _check_png(data, decode_pixels=False)
    _check_running(check_running)
    return paths


def validate_accuracy(directory, run_id, axis_results, result="complete", reason=None,
                      simulated=False, renderer=None, check_running=None):
    _identity(run_id, "x")
    def cooperate():
        _check_running(check_running)
        reactor = getattr(renderer, "reactor", None)
        if reactor is not None:
            reactor.pause(reactor.monotonic() + 0.001)
        _check_running(check_running)
    axes = _validate_accuracy(Path(directory).expanduser().resolve(), run_id, axis_results,
                              result, reason, simulated, check_running=cooperate)
    _check_running(check_running)
    return axes


def write_accuracy(directory, run_id, axis_results, result, reason=None, simulated=False,
                   renderer=None, check_running=None):
    axes = validate_accuracy(directory, run_id, axis_results, result, reason, simulated,
                             renderer=renderer, check_running=check_running)
    clock = _clock()
    paths = {}
    for axis, item in axes.items():
        traces = [reference.receipt.trace for reference in item["capture_paths"] if reference is not None]
        payload = {"kind": "accuracy", "axis": axis, "result": result, "reason": reason,
                   "requested": item["requested_samples"], "trials": item["trials"],
                   "statistics": item["statistics"], "traces": traces,
                   "discovery": item.get("discovery"), "simulated": bool(simulated), **clock}
        data = (renderer or PNGRenderer()).render(payload, check_running=check_running)
        paths[axis + "_png"] = _publish(directory, "accel_homing_accuracy_" + axis,
                                        clock, data, check_running)
    _check_running(check_running)
    return paths


def write_noise_capture(directory, run_id, iteration, capture, record, simulated=False,
                        renderer=None, check_running=None):
    """Freeze a validated window in memory; never create an intermediate file."""
    _noise_identity(run_id, iteration)
    samples, baseline, delta = _validate_noise_capture(run_id, iteration, capture, record)
    elapsed = [(row[0] - samples[0][0], *row[1:]) for row in delta]
    metadata = {"sample_count": len(samples), "baseline_g": list(baseline)}
    receipt = _Receipt("noise", run_id, None, iteration, _json(record), _json(metadata),
                       _envelope(elapsed), None, None, bool(simulated))
    _check_running(check_running)
    return CapturePaths({}, receipt)


def _noise_reference(run_id, iteration, item, simulated):
    record = item.get("record")
    _check_noise_record(run_id, iteration, record)
    paths = item.get("capture_paths")
    if not isinstance(paths, CapturePaths) or paths:
        raise ValueError("Stationary noise requires its in-memory capture receipt")
    receipt = paths.receipt
    if not isinstance(receipt, _Receipt) or receipt.kind != "noise" or receipt.run_id != run_id \
            or receipt.iteration != iteration or receipt.record_json != _json(record) \
            or receipt.simulated != bool(simulated):
        raise ValueError("Stationary noise capture receipt does not match its current-run window")
    return receipt


def write_noise(directory, run_id, iterations, recommendation, result, reason=None, simulated=False,
                renderer=None, check_running=None):
    _noise_identity(run_id, 1)
    if result not in ("complete", "aborted", "incomplete", "failed", "cancelled") \
            or not isinstance(iterations, list) or len(iterations) > 3 \
            or (reason is not None and (not isinstance(reason, str) or len(reason) > 4000)):
        raise ValueError("Invalid stationary noise PNG report")
    if result == "complete" and len(iterations) != 3:
        raise ValueError("A complete stationary noise report requires three valid windows")
    if result != "complete" and recommendation is not None:
        raise ValueError("Incomplete stationary noise cannot recommend a calibration candidate")
    receipts, context, previous_end = [], None, None
    for iteration, item in enumerate(iterations, 1):
        if not isinstance(item, dict):
            raise ValueError("Invalid stationary noise iteration")
        receipt = _noise_reference(run_id, iteration, item, simulated)
        record = item["record"]
        if previous_end is not None and record["first_sample_time_s"] <= previous_end:
            raise ValueError("Stationary noise windows overlap or repeat earlier samples")
        previous_end = record["last_sample_time_s"]
        current = _json({key: record.get(key) for key in
                         ("sensor", "backend", "axis_context", "full_scale_g", "sensor_context")})
        if context is not None and current != context:
            raise ValueError("Stationary noise sensor context changed between windows")
        context = current
        receipts.append(receipt)
    if result == "complete":
        _validate_noise_recommendation(recommendation, iterations)
    clock = _clock()
    payload = {"kind": "noise", "result": result, "reason": reason,
               "records": [item["record"] for item in iterations],
               "traces": [receipt.trace for receipt in receipts],
               "recommendation": recommendation, "simulated": bool(simulated), **clock}
    data = (renderer or PNGRenderer()).render(payload, check_running=check_running)
    path = _publish(directory, "accel_homing_stationary_noise", clock, data, check_running)
    _check_running(check_running)
    return {"png": path}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def _accuracy_statistics(trials):
    values = [trial["travel_mm"] for trial in trials]
    if not values:
        return None
    variance = statistics.pvariance(values) if len(values) >= 2 else None
    result = {"count": len(values), "min_mm": min(values), "max_mm": max(values),
              "mean_mm": statistics.mean(values), "spread_mm": max(values) - min(values),
              "stddev_mm": math.sqrt(variance) if variance is not None else None,
              "variance_mm2": variance,
              "variance_definition": _POPULATION if variance is not None else
              _POPULATION + "; at least 2 measured homes required"}
    if any(not _finite(value) for key, value in result.items()
           if key != "variance_definition" and value is not None):
        raise ValueError("Accuracy statistics are not finite")
    return result

def _check_accuracy_record(record, run_id, axis, phase, index, attempts, measured=True):
    if not isinstance(record, dict) or record.get("run_id") != run_id \
            or record.get("axis") != axis or record.get("workflow_phase") != phase \
            or record.get("accuracy_sample") != index:
        raise ValueError("Accuracy trial identity does not match this run")
    attempt = record.get("workflow_attempt")
    if type(attempt) is not int or attempt <= 0 or attempt in attempts:
        raise ValueError("Accuracy trial attempt is invalid or duplicated")
    attempts.add(attempt)
    if measured:
        if record.get("result") != "impact" or record.get("capture_valid") is False \
                or not _finite(record.get("travel_mm")) or record["travel_mm"] <= 0 \
                or not _finite(record.get("onset_g")) \
                or not _finite(record.get("impact_peak_g")) or record["impact_peak_g"] < 0 \
                or not _finite(record.get("threshold_g")) or record["threshold_g"] <= 0 \
                or record.get("direction") not in (-1, 0, 1):
            raise ValueError("Accuracy trial has invalid travel or acceleration measurements")

def _validate_accuracy(directory, run_id, axis_results, result, reason, simulated, check_running=None):
    if result not in ("complete", "aborted", "incomplete", "failed", "cancelled"):
        raise ValueError("Invalid accuracy report result")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
        raise ValueError("Invalid accuracy report reason")
    if not isinstance(axis_results, dict) or not axis_results \
            or any(axis not in ("x", "y") for axis in axis_results):
        raise ValueError("Accuracy report requires X and/or Y results")
    # Validate before opening any output; also reject NaN hidden in metadata.
    if len(json.dumps(axis_results, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Accuracy metadata exceeds its report size limit")
    checked, attempts = {}, set()
    for axis in (key for key in "xy" if key in axis_results):
        _check_running(check_running)
        item = axis_results[axis]
        if not isinstance(item, dict):
            raise ValueError("Invalid accuracy axis result")
        requested, trials = item.get("requested_samples"), item.get("trials")
        if type(requested) is not int or not 2 <= requested <= 50 \
                or not isinstance(trials, list) or len(trials) > requested \
                or type(item.get("complete")) is not bool \
                or item["complete"] != (len(trials) == requested):
            raise ValueError("Accuracy trial count does not match requested samples")
        capture_paths = item.get("capture_paths", [])
        if not isinstance(capture_paths, list) or len(capture_paths) != len(trials):
            raise ValueError("Accuracy capture paths are not aligned with measured trials")
        discovery = item.get("discovery")
        if discovery is not None:
            _check_accuracy_record(discovery, run_id, axis, "discovery", 0, attempts)
        if trials and discovery is None:
            raise ValueError("Measured accuracy homes require a separate qualifying home")
        refs = []
        for index, (record, paths) in enumerate(zip(trials, capture_paths), 1):
            _check_accuracy_record(record, run_id, axis, "measurement", index, attempts)
            if discovery is not None and (not math.isclose(record["threshold_g"], discovery["threshold_g"],
                                                           rel_tol=1e-12, abs_tol=1e-12)
                    or record.get("workflow_threshold_g") != discovery.get("workflow_threshold_g")):
                raise ValueError("Accuracy threshold changed during the measured run")
            refs.append(_capture_reference(directory, paths, run_id, axis, record, simulated, check_running))
        discovery_ref = _capture_reference(directory, item.get("discovery_capture_paths"),
                                           run_id, axis, discovery, simulated, check_running) \
            if discovery is not None else None
        rejected = item.get("rejected")
        rejection_ref = None
        if rejected is not None:
            if not isinstance(rejected, dict) or rejected.get("run_id") != run_id \
                    or rejected.get("axis") != axis \
                    or type(rejected.get("workflow_attempt")) is not int \
                    or rejected["workflow_attempt"] <= 0 \
                    or rejected["workflow_attempt"] in attempts:
                raise ValueError("Rejected accuracy trial identity is invalid")
            attempts.add(rejected["workflow_attempt"])
            rejection_ref = _capture_reference(directory, item.get("rejection_capture_paths"),
                                               run_id, axis, rejected, simulated, check_running)
        computed = _accuracy_statistics(trials)
        supplied = item.get("statistics")
        if supplied is not None:
            if computed is None or not isinstance(supplied, dict):
                raise ValueError("Accuracy statistics do not match measured trials")
            for key, value in computed.items():
                other = supplied.get(key)
                if value is None or isinstance(value, str):
                    equal = other == value
                else:
                    equal = _finite(other) and math.isclose(value, other, rel_tol=1e-9, abs_tol=1e-12)
                if not equal:
                    raise ValueError("Accuracy statistics do not match measured trials")
        if result == "complete" and (not item["complete"] or discovery_ref is None
                                      or any(ref is None for ref in refs) or rejected is not None):
            raise ValueError("A complete accuracy report requires every requested valid capture")
        checked[axis] = {**item, "statistics": computed, "capture_paths": refs,
                         "discovery_capture_paths": discovery_ref,
                         "rejection_capture_paths": rejection_ref}
    return checked

def _noise_identity(run_id, iteration):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid noise run ID")
    if type(iteration) is not int or not 1 <= iteration <= _NOISE_WINDOWS:
        raise ValueError("Invalid stationary noise iteration")
    return "accel_homing_noise_%s_i%02d" % (run_id, iteration)

def _noise_vector(value, label):
    if not isinstance(value, dict) or set(value) != set("xyz") \
            or not all(_finite(number) for number in value.values()):
        raise ValueError("Invalid stationary noise %s" % label)
    return value

def _same_noise_number(first, second):
    return _finite(first) and _finite(second) and math.isclose(
        first, second, rel_tol=1e-9, abs_tol=1e-12)

def _check_noise_record(run_id, iteration, record):
    _noise_identity(run_id, iteration)
    if not isinstance(record, dict) or record.get("run_id") != run_id \
            or type(record.get("noise_iteration")) is not int or record["noise_iteration"] != iteration \
            or type(record.get("sample_count")) is not int or record["sample_count"] < 50 \
            or not _finite(record.get("duration_s")) \
            or record["duration_s"] < _NOISE_WINDOW_SECONDS - 1e-9 \
            or not _finite(record.get("first_sample_time_s")) \
            or not _finite(record.get("last_sample_time_s")) \
            or not _same_noise_number(record["duration_s"],
                                      record["last_sample_time_s"] - record["first_sample_time_s"]):
        raise ValueError("Stationary noise record is not a complete current-run window")
    for key in ("baseline_g", "noise_peak_g", "noise_peak_to_peak_g", "noise_rms_g"):
        vector = _noise_vector(record.get(key), key)
        if key != "baseline_g" and any(value < 0 for value in vector.values()):
            raise ValueError("Stationary noise magnitudes must not be negative")
    for key in ("sensor", "backend", "axis_context"):
        if not isinstance(record.get(key), str) or not record[key] or len(record[key]) > 400:
            raise ValueError("Invalid stationary noise sensor context")
    full_scale = record.get("full_scale_g")
    if full_scale is not None and (not _finite(full_scale) or full_scale <= 0):
        raise ValueError("Invalid stationary noise sensor range")
    json.dumps(record, allow_nan=False)

def _validate_noise_capture(run_id, iteration, capture, record):
    _check_noise_record(run_id, iteration, record)
    if not isinstance(capture, dict) or capture.get("dropped", 0) \
            or capture.get("fault") or capture.get("error"):
        raise ValueError("Stationary noise sensor capture is incomplete or faulted")
    samples = capture.get("samples", ())
    if not isinstance(samples, (list, tuple)) or len(samples) != record["sample_count"]:
        raise ValueError("Stationary noise capture count does not match its record")
    previous = None
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) != 4 \
                or not all(_finite(value) for value in sample) \
                or (previous is not None and sample[0] <= previous):
            raise ValueError("Invalid stationary noise accelerometer samples")
        previous = sample[0]
    if not _same_noise_number(samples[0][0], record["first_sample_time_s"]) \
            or not _same_noise_number(samples[-1][0], record["last_sample_time_s"]):
        raise ValueError("Stationary noise sample times do not match their record")
    baseline = tuple(math.fsum(sample[index] for sample in samples) / len(samples)
                     for index in (1, 2, 3))
    supplied = capture.get("baseline_g")
    if not isinstance(supplied, (list, tuple)) or len(supplied) != 3 \
            or not all(_same_noise_number(value, supplied[index])
                       for index, value in enumerate(baseline)):
        raise ValueError("Stationary noise capture must use its full-window mean")
    delta = [(sample[0],) + tuple(sample[index + 1] - baseline[index] for index in range(3))
             for sample in samples]
    for index, axis in enumerate("xyz", 1):
        values = [sample[index] for sample in delta]
        computed = {"baseline_g": baseline[index - 1],
                    "noise_peak_g": max(abs(value) for value in values),
                    "noise_peak_to_peak_g": max(values) - min(values),
                    "noise_rms_g": math.sqrt(math.fsum(value * value for value in values) / len(values))}
        if any(not _same_noise_number(record[key][axis], value) for key, value in computed.items()):
            raise ValueError("Stationary noise statistics do not match measured samples")
    return samples, baseline, delta

def _validate_noise_recommendation(recommendation, iterations):
    if not isinstance(recommendation, dict) or recommendation.get("source") != "stationary_noise" \
            or recommendation.get("method") != "initial_candidate_heuristic" \
            or recommendation.get("margin_factor") != 1.2 or recommendation.get("margin_g") != 0.1 \
            or recommendation.get("clamped") is not False:
        raise ValueError("Invalid stationary noise initial-candidate heuristic")
    for key in ("candidate_step_g", "configured_start_g"):
        if not _finite(recommendation.get(key)) or recommendation[key] <= 0:
            raise ValueError("Invalid stationary noise candidate bounds")
    limit = recommendation.get("threshold_limit_g")
    if limit is not None and (not _finite(limit) or limit <= 0):
        raise ValueError("Invalid stationary noise candidate ceiling")
    observed = _noise_vector(recommendation.get("noise_peak_g"), "maximum peak")
    candidates = recommendation.get("start_threshold_g")
    if not isinstance(candidates, dict) or set(candidates) != set("xy"):
        raise ValueError("Invalid stationary noise axis candidates")
    for axis in "xyz":
        if not _same_noise_number(observed[axis], max(item["record"]["noise_peak_g"][axis]
                                                     for item in iterations)):
            raise ValueError("Stationary noise recommendation peak does not match its windows")
    if limit is None:
        if any(item["record"]["full_scale_g"] is not None for item in iterations) \
                or candidates != {"x": None, "y": None} \
                or recommendation.get("unavailable_axes") != ["x", "y"]:
            raise ValueError("Unknown sensor range cannot supply a stationary noise calibration candidate")
        json.dumps(recommendation, allow_nan=False)
        return
    unavailable = []
    start, step = recommendation["configured_start_g"], recommendation["candidate_step_g"]
    for axis in "xy":
        minimum = max(start, observed[axis] * 1.2, observed[axis] + 0.1)
        candidate = start + max(0, math.ceil((minimum - start) / step)) * step
        if not _finite(candidate) or candidate > limit:
            unavailable.append(axis)
            if candidates[axis] is not None:
                raise ValueError("Stationary noise candidate exceeds its ceiling; do not clamp it downward")
        elif not _same_noise_number(candidates[axis], candidate):
            raise ValueError("Stationary noise candidate is not rounded upward on the calibration grid")
    if recommendation.get("unavailable_axes") != unavailable:
        raise ValueError("Stationary noise unavailable candidates do not match the ceiling")
    json.dumps(recommendation, allow_nan=False)


def _worker_main(source, target, dependency_directory):
    # This function runs only as a separate selected-Python process. Plotting
    # packages and their font/config cache are never loaded into Klippy.
    private = Path(dependency_directory)
    if private.is_dir():
        sys.path.insert(0, str(private))
    os.environ["MPLCONFIGDIR"] = str(Path(source).parent / "matplotlib-cache")
    os.environ["MPLBACKEND"] = "Agg"
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib import rcParams
    rcParams["text.usetex"] = False
    if "text.parse_math" in rcParams:
        rcParams["text.parse_math"] = False
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    kind = payload["kind"]
    figure = Figure(figsize=(14, 7) if kind == "capture" else (14, 12), dpi=120)
    try:
        canvas = FigureCanvasAgg(figure)
        if kind == "preflight":
            figure.set_size_inches(2, 1)
            figure.subplots().plot([0, 1], [0, 1])
        elif kind == "capture":
            _plot_capture(figure, payload)
        elif kind == "accuracy":
            _plot_accuracy(figure, payload)
        elif kind == "noise":
            _plot_noise(figure, payload)
        else:
            raise ValueError("Unknown PNG chart render kind")
        canvas.print_png(target, metadata={
            "Software": "Beacon Accelerometer Homing",
            "Creation Time": payload.get("created_utc", ""),
            "Description": "SIMULATED - NOT PRINTER MEASUREMENT" if payload.get("simulated")
                           else "Captured accelerometer measurements"})
        _check_png(Path(target).read_bytes())
    finally:
        figure.clear()


def _title(figure, payload, title, note=""):
    status = payload.get("result", "complete")
    if status != "complete":
        title = "INCOMPLETE / %s - %s" % (status.upper(), title)
    if payload.get("simulated"):
        title = "SIMULATED / NOT PRINTER MEASUREMENT\n" + title
    figure.suptitle(title + "\n" + payload["timestamp"], fontsize=15, y=.975, va="top")
    if note:
        figure.text(.07, .88, note, fontsize=10, va="top", wrap=True)


def _axes_style(axis, ylabel="Acceleration minus stationary baseline (g)"):
    axis.grid(True, color="#dbe2ea", linewidth=.65)
    axis.set_ylabel(ylabel)
    axis.set_xlabel("Time since first sample (s)")
    axis.axhline(0, color="#637083", linewidth=.5)


def _traces(axis, trace, alpha=1.0, labels=True):
    for index, (letter, color) in enumerate(zip("XYZ", ("#1769aa", "#c54238", "#217746")), 1):
        axis.plot([row[0] for row in trace], [row[index] for row in trace], color=color,
                  linewidth=.9, alpha=alpha, label=letter if labels else None)


def _threshold(axis, record, label=True):
    threshold = record.get("threshold_g")
    if not _finite(threshold) or threshold <= 0:
        return
    direction = record.get("direction", 0)
    limits = [direction * threshold] if direction in (-1, 1) else [-threshold, threshold]
    for index, value in enumerate(limits):
        axis.axhline(value, color="#744b98", linestyle="--", linewidth=1.1,
                     label=("Configured trigger %+.9g g" % value) if label else None)


def _plot_capture(figure, payload):
    record, trace, captured = payload["record"], payload["trace"], payload["capture"]
    title = "%s homing - measured XYZ acceleration" % payload["axis"].upper()
    _title(figure, payload, title)
    axis = figure.subplots()
    figure.subplots_adjust(left=.09, right=.97, bottom=.24, top=.78)
    _axes_style(axis)
    _traces(axis, trace)
    _threshold(axis, record)
    onset = record.get("onset_time_s", record.get("onset_time"))
    if _finite(onset):
        elapsed = onset - captured["first_sample_time_s"]
        if trace[0][0] <= elapsed <= trace[-1][0]:
            axis.axvline(elapsed, color="#5e4c77", linestyle=":", linewidth=1.1,
                         label="Detected onset %.9g s" % elapsed)
    channel = {"x": 1, "y": 2}[payload["axis"]]
    peak = max(trace, key=lambda row: abs(row[channel]))
    axis.plot([peak[0]], [peak[channel]], "o", color="#192d45", markersize=4)
    axis.annotate("Captured |%s| peak %.9g g" % (payload["axis"].upper(), abs(peak[channel])),
                  (peak[0], peak[channel]), xytext=(8, 12), textcoords="offset points", fontsize=9)
    axis.legend(loc="best", fontsize=9)
    figure.text(.09, .07, "Samples: %d   |   detector onset: %s g   |   detector impact peak: %s g\n"
                "Commanded travel: %s mm; does not certify physical stop position. "
                "Signed XYZ traces retain positive and negative extrema." %
                (captured["sample_count"], record.get("onset_g", "unavailable"),
                 record.get("impact_peak_g", "unavailable"), record.get("travel_mm", "unavailable")),
                fontsize=10, va="bottom")


def _plot_accuracy(figure, payload):
    stats = payload.get("statistics") or {}
    trials, traces = payload["trials"], payload["traces"]
    summary = "Measured homes %d/%d; qualifier excluded. " % (len(trials), payload["requested"])
    if stats:
        summary += ("Min %.6g / max %.6g / mean %.6g / range %.6g mm; population stddev %s mm." %
                    (stats["min_mm"], stats["max_mm"], stats["mean_mm"], stats["spread_mm"],
                     "unavailable" if stats["stddev_mm"] is None else "%.6g" % stats["stddev_mm"]))
    _title(figure, payload, "%s repeated homing - commanded travel and measured acceleration" %
           payload["axis"].upper(), summary)
    axes = figure.subplots(4, 1)
    figure.subplots_adjust(left=.09, right=.97, bottom=.09, top=.81, hspace=.55)
    travel = axes[0]
    travel.set_ylabel("Commanded travel (mm)")
    travel.set_xlabel("Accepted measured home")
    travel.grid(True, color="#dbe2ea")
    travel.ticklabel_format(axis="y", style="plain", useOffset=False)
    if trials:
        travel.plot(range(1, len(trials) + 1), [row["travel_mm"] for row in trials], "o-",
                    color="#1769aa", markersize=4)
        travel.set_xlim(.5, max(1.5, len(trials) + .5))
    else:
        travel.text(.5, .5, "No accepted measured homes", ha="center", transform=travel.transAxes)
    for channel, (axis, color, letter) in enumerate(zip(axes[1:],
                    ("#1769aa", "#c54238", "#217746"), "XYZ"), 1):
        _axes_style(axis, "%s baseline-relative (g)" % letter)
        for index, trace in enumerate(traces):
            axis.plot([row[0] for row in trace], [row[channel] for row in trace], color=color,
                      alpha=.35 if len(traces) > 1 else 1., linewidth=.7,
                      label="%d accepted capture(s)" % len(traces) if index == 0 else None)
        if channel == {"x": 1, "y": 2}[payload["axis"]] and trials:
            _threshold(axis, trials[0])
        if traces:
            axis.legend(fontsize=8, loc="best")
    note = ("Population variance: %s mm². Per-home PNGs retain individual XYZ, threshold/onset and peak details. "
            "Commanded steps are not physical stop accuracy or a calibrated impact threshold." %
            ("unavailable" if stats.get("variance_mm2") is None else "%.9g" % stats["variance_mm2"]))
    if payload.get("reason"):
        note = str(payload["reason"])[:300] + "\n" + note
    figure.text(.09, .015, note, fontsize=9, va="bottom", wrap=True)


def _plot_noise(figure, payload):
    recommendation, records, traces = payload["recommendation"], payload["records"], payload["traces"]
    note = ""
    if recommendation:
        candidates = recommendation["start_threshold_g"]
        fmt = lambda value: "unavailable" if value is None else "%.3g g" % value
        note = "Starting candidates (not calibrated): X %s; Y %s. Max noise peaks XYZ: %s g." % (
            fmt(candidates["x"]), fmt(candidates["y"]),
            " / ".join("%.3g" % recommendation["noise_peak_g"][axis] for axis in "xyz"))
    else:
        note = "Incomplete measurement: no starting candidate."
    _title(figure, payload, "Stationary accelerometer vibration - %d of 3 windows" % len(records), note)
    axes = figure.subplots(3, 1)
    figure.subplots_adjust(left=.09, right=.97, bottom=.07, top=.81, hspace=.55)
    peak = max(.001, max((abs(value) for trace in traces for row in trace for value in row[1:]), default=0.)) * 1.1
    for index, axis in enumerate(axes):
        _axes_style(axis, "Acceleration minus window mean (g)")
        axis.set_ylim(-peak, peak)
        if index >= len(traces):
            axis.set_title("Iteration %d: no complete valid window" % (index + 1), fontsize=10)
            continue
        record = records[index]
        _traces(axis, traces[index])
        axis.set_title("Iteration %d - %.3g s | peak XYZ %s g | RMS XYZ %s g" %
                       (index + 1, record["duration_s"],
                        " / ".join("%.3g" % record["noise_peak_g"][key] for key in "xyz"),
                        " / ".join("%.3g" % record["noise_rms_g"][key] for key in "xyz")), fontsize=10)
        axis.legend(loc="best", fontsize=8)
    if payload.get("reason"):
        figure.text(.09, .015, str(payload["reason"])[:240], fontsize=9, va="bottom", wrap=True)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--render-worker":
        raise SystemExit("This module is a private PNG rendering worker")
    _worker_main(sys.argv[2], sys.argv[3], sys.argv[4])
