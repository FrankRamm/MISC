# Portable chart output for accelerometer homing. GPLv3.
"""Write actual sensor samples without requiring matplotlib or numpy."""
import csv
import datetime
import html
import json
import math
import os
import re
import statistics
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def write_capture(directory, run_id, axis, capture, record, simulated=False):
    """Publish one complete capture directory atomically; preserve prior runs."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid capture run ID")
    if axis not in ("x", "y"):
        raise ValueError("Capture axis must be x or y")
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / ("accel_homing_%s_%s" % (run_id, axis))
    if os.path.lexists(published):
        raise ValueError("Capture output already exists for this run")
    with tempfile.TemporaryDirectory(prefix=".accel_homing_", dir=str(destination)) as temporary:
        staged = _write_capture(temporary, run_id, axis, capture, record, simulated)
        os.rename(temporary, published)
    return {extension: str(published / Path(path).name) for extension, path in staged.items()}


def _write_capture(directory, run_id, axis, capture, record, simulated=False):
    """Return absolute CSV/SVG/HTML paths for a single current-run capture.

    Samples use MCU-correlated print time and machine-mapped X/Y/Z g. They
    are measured acceleration, not motor-step acceleration or physical position.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid capture run ID")
    if axis not in ("x", "y"):
        raise ValueError("Capture axis must be x or y")
    samples = capture.get("samples", ())
    if capture.get("dropped", 0):
        raise ValueError("Capture buffer overflow; refusing an incomplete chart")
    if capture.get("fault") or capture.get("error"):
        raise ValueError("Sensor capture fault; refusing an invalid chart")
    if not samples:
        raise ValueError("No captured accelerometer samples for this home")
    previous = None
    for sample in samples:
        if len(sample) != 4 or not all(_finite(x) for x in sample):
            raise ValueError("Invalid accelerometer sample")
        if previous is not None and sample[0] <= previous:
            raise ValueError("Accelerometer sample time did not increase")
        previous = sample[0]
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = "accel_homing_%s_%s" % (run_id, axis.lower())
    paths = {ext: str(destination / (stem + "." + ext))
             for ext in ("csv", "svg", "html", "json")}
    # A run UUID is fresh for every command; never overwrite a prior capture.
    if any(os.path.lexists(p) for p in paths.values()):
        raise ValueError("Capture output already exists for this run")
    t0 = samples[0][0]
    baseline = capture.get("baseline_g")
    if baseline is None:
        arm = record.get("arm_time_s", t0)
        quiet = [row for row in samples if row[0] < arm]
        if not quiet:
            raise ValueError("No pre-arm baseline for measured-acceleration chart")
        baseline = tuple(sum(row[i] for row in quiet) / len(quiet) for i in (1, 2, 3))
    if len(baseline) != 3 or not all(_finite(value) for value in baseline):
        raise ValueError("Invalid accelerometer baseline")
    axis_index = {"x": 1, "y": 2}[axis]
    delta_samples = [(row[0],) + tuple(row[i + 1] - baseline[i] for i in range(3))
                     for row in samples]
    with open(paths["csv"], "x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("print_time_s", "elapsed_s", "raw_x_g", "raw_y_g", "raw_z_g",
                         "delta_x_g", "delta_y_g", "delta_z_g"))
        for stamp, x, y, z in samples:
            writer.writerow(("%.9f" % stamp, "%.9f" % (stamp - t0),
                             "%.7f" % x, "%.7f" % y, "%.7f" % z,
                             "%.7f" % (x - baseline[0]), "%.7f" % (y - baseline[1]),
                             "%.7f" % (z - baseline[2])))
    metadata = {"run_id": run_id, "axis": axis.upper(),
                "data_source": "SIMULATED - NOT PRINTER MEASUREMENT" if simulated
                else "captured accelerometer measurements",
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "sample_count": len(samples), "baseline_g": list(baseline),
                "capture": {k: v for k, v in capture.items() if k != "samples"}, "record": record}
    with open(paths["json"], "x", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, allow_nan=False)
    width, height = 1100, 440
    left, right, top, bottom = 80, 1040, 65, 365
    duration = max(samples[-1][0] - t0, 0.000001)
    threshold = record.get("threshold_g")
    threshold = threshold if _finite(threshold) and threshold > 0.0 else None
    peak = max(1.0, threshold or 0.0, max(abs(v) for row in delta_samples for v in row[1:])) * 1.05
    sx = lambda stamp: left + (stamp - t0) / duration * (right - left)
    sy = lambda value: (top + bottom) / 2.0 - value / peak * (bottom - top) / 2.0
    title = "%s axis homing: baseline-subtracted measured acceleration" % axis.upper()
    if simulated:
        title = "SIMULATED / NOT PRINTER MEASUREMENT: " + title
    subtitle = "onset %s g; impact peak %s g; run %s" % (
        record.get("onset_g", "n/a"), record.get("impact_peak_g", "n/a"), run_id)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
             'role="img" aria-label="%s">' % (width, height, html.escape(title, quote=True)),
             '<rect width="1100" height="440" fill="#fff"/>',
             '<g font-family="sans-serif" fill="#182331">',
             '<text x="80" y="25" font-size="18">%s</text>' % html.escape(title),
             '<text x="80" y="47" font-size="12">%s</text>' % html.escape(subtitle)]
    for tick in range(5):
        value = -peak + tick * peak / 2.0
        y = sy(value)
        parts.append('<path d="M80 %.3fH1040" stroke="#dce1e8"/>' % y)
        parts.append('<text x="70" y="%.3f" text-anchor="end" font-size="12">%.1f</text>'
                     % (y + 4, value))
    for tick in range(6):
        x = left + tick / 5.0 * (right - left)
        parts.append('<text x="%.3f" y="388" text-anchor="middle" font-size="12">%.3f</text>'
                     % (x, duration * tick / 5.0))
    parts.extend(['<text x="560" y="415" text-anchor="middle">Time since first sample (s)</text>',
                  '<text x="18" y="220" transform="rotate(-90 18 220)" '
                  'text-anchor="middle">Acceleration minus stationary baseline (g)</text>'])
    if threshold is not None:
        direction = record.get("direction", 0)
        limits = [threshold * direction] if direction in (-1, 1) else [-threshold, threshold]
        for limit in limits:
            y = sy(limit)
            # Preserve precise configured thresholds, including values below
            # 0.01 g, while retaining familiar two-decimal labels when exact.
            label = "%+.2f" % limit if round(limit, 2) == limit else "%+.9g" % limit
            parts.append('<path d="M80 %.3fH1040" stroke="#6b5090" stroke-dasharray="6 4"/>' % y)
            parts.append('<text x="1040" y="%.3f" text-anchor="end" font-size="12" '
                         'fill="#6b5090">%s g threshold (configured trigger)</text>' % (y - 3, label))
    # Bound SVG size while retaining each bucket's positive and negative peaks.
    # CSV always contains every recorded sample at its original resolution.
    stride = max(1, int(math.ceil(len(samples) / 4000.0)))
    colors = ("#1769aa", "#c54238", "#217746")
    for index, color in enumerate(colors, 1):
        points = []
        for offset in range(0, len(delta_samples), stride):
            bucket = delta_samples[offset:offset + stride]
            chosen = sorted({0, len(bucket) - 1,
                             min(range(len(bucket)), key=lambda i: bucket[i][index]),
                             max(range(len(bucket)), key=lambda i: bucket[i][index])})
            points.extend("%.3f,%.3f" % (sx(bucket[i][0]), sy(bucket[i][index]))
                          for i in chosen)
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"/>'
                     % (" ".join(points), color, "1.8" if index == axis_index else "0.7"))
        parts.append('<text x="%d" y="433" fill="%s" font-size="12">%s</text>'
                     % (440 + index * 60, color, "XYZ"[index - 1]))
    onset = record.get("onset_time_s", record.get("onset_time"))
    if _finite(onset) and t0 <= onset <= samples[-1][0]:
        x = sx(onset)
        parts.append('<path d="M%.3f %dV%d" stroke="#624c83" stroke-dasharray="4 3"/>'
                     % (x, top, bottom))
        parts.append('<text x="%.3f" y="60" text-anchor="end" font-size="11" '
                     'fill="#624c83">detected onset</text>' % min(right, max(left + 90, x)))
    parts.append('</g></svg>')
    svg = "\n".join(parts)
    with open(paths["svg"], "x", encoding="utf-8") as stream:
        stream.write(svg)
    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>body{font:16px sans-serif;margin:2rem;'
            'color:#182331}svg{width:100%%;max-width:1500px}code{overflow-wrap:anywhere}'
            '</style><h1>%s</h1><p>%s</p>%s<p>'
            '<a href="%s">Full-resolution CSV</a> · <a href="%s">Metadata JSON</a>'
            '</p><p>Travel is firmware-commanded step distance. It does not certify '
            'physical stop position or absence of skipped steps.</p></html>') % (
                html.escape(title), html.escape(title), html.escape(subtitle), svg,
                html.escape(Path(paths["csv"]).name, quote=True),
                html.escape(Path(paths["json"]).name, quote=True))
    with open(paths["html"], "x", encoding="utf-8") as stream:
        stream.write(page)
    return paths


# A report holds only records and paths. Each SVG is read, checked and reduced
# individually; no collection of full-resolution sample arrays is reconstructed.
_MAX_CAPTURE_SVG_BYTES = 2 * 1024 * 1024
_MAX_CAPTURE_JSON_BYTES = 128 * 1024
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_POPULATION = "population (divide by N)"
_TRAVEL_DEFINITION = ("Repeated firmware-commanded travel, recorded to 0.001 mm; "
                      "not encoder-measured physical stop accuracy.")


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


def _read_limited(path, limit):
    with open(path, "rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Accuracy capture file exceeds its report size limit")
    return content.decode("utf-8")


def _capture_reference(directory, paths, run_id, axis, record, simulated):
    if paths is None:
        return None
    if not isinstance(paths, dict):
        raise ValueError("Invalid accuracy capture paths")
    capture_id = "%s_a%03d" % (run_id, record["workflow_attempt"])
    stem = "accel_homing_%s_%s" % (capture_id, axis)
    resolved = {}
    for extension in ("csv", "svg", "html", "json"):
        path = paths.get(extension)
        if not isinstance(path, str):
            raise ValueError("Accuracy capture is missing an output file")
        candidate = Path(path).expanduser().resolve(strict=True)
        expected = directory / stem / (stem + "." + extension)
        if candidate != expected or not candidate.is_file():
            raise ValueError("Accuracy capture path does not belong to this run")
        resolved[extension] = str(candidate)
    metadata = json.loads(_read_limited(resolved["json"], _MAX_CAPTURE_JSON_BYTES))
    source = ("SIMULATED - NOT PRINTER MEASUREMENT" if simulated else
              "captured accelerometer measurements")
    capture = metadata.get("capture", {})
    if metadata.get("run_id") != capture_id or metadata.get("axis") != axis.upper() \
            or metadata.get("record") != json.loads(json.dumps(record, allow_nan=False)) \
            or metadata.get("data_source") != source \
            or type(metadata.get("sample_count")) is not int or metadata["sample_count"] < 2 \
            or not isinstance(capture, dict) or capture.get("dropped", 0) \
            or capture.get("fault") or capture.get("error"):
        raise ValueError("Accuracy capture metadata does not match a complete valid trial")
    baseline = metadata.get("baseline_g")
    if not isinstance(baseline, list) or len(baseline) != 3 \
            or not all(_finite(value) for value in baseline):
        raise ValueError("Accuracy capture has an invalid detector baseline")
    return resolved


def _validate_accuracy(directory, run_id, axis_results, result, reason, simulated):
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
            refs.append(_capture_reference(directory, paths, run_id, axis, record, simulated))
        discovery_ref = _capture_reference(directory, item.get("discovery_capture_paths"),
                                           run_id, axis, discovery, simulated) \
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
                                               run_id, axis, rejected, simulated)
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


def _embedded_capture_svg(path):
    """Permit only the passive SVG elements emitted by write_capture()."""
    root = ET.fromstring(_read_limited(path, _MAX_CAPTURE_SVG_BYTES))
    namespace = "{http://www.w3.org/2000/svg}"
    if root.tag != namespace + "svg":
        raise ValueError("Accuracy capture is not an SVG chart")
    tags = {"svg", "g", "rect", "text", "path", "polyline"}
    attrs = {"viewBox", "role", "aria-label", "width", "height", "fill", "stroke",
             "font-family", "font-size", "x", "y", "text-anchor", "d", "points",
             "stroke-dasharray", "stroke-width", "transform"}
    for element in root.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith(namespace) \
                or element.tag[len(namespace):] not in tags \
                or any(key not in attrs for key in element.attrib):
            raise ValueError("Accuracy capture contains unsupported SVG content")
        for key, value in element.attrib.items():
            if "url(" in value.lower() or (key in ("fill", "stroke") and
                    not re.fullmatch(r"none|#[0-9a-fA-F]{3,8}", value)):
                raise ValueError("Accuracy capture contains an unsafe SVG attribute")
        if element.tag == namespace + "polyline":
            numbers = re.split(r"[\s,]+", element.get("points", "").strip())
            if len(numbers) % 2 or not numbers or len(numbers) > 100000:
                raise ValueError("Invalid accuracy acceleration trace")
            values = [float(number) for number in numbers]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Invalid accuracy acceleration trace")
            points = list(zip(values[::2], values[1::2]))
            stride = max(1, int(math.ceil(len(points) / 256.0)))
            reduced = []
            for offset in range(0, len(points), stride):
                bucket = points[offset:offset + stride]
                chosen = sorted({0, len(bucket) - 1,
                                 min(range(len(bucket)), key=lambda i: bucket[i][1]),
                                 max(range(len(bucket)), key=lambda i: bucket[i][1])})
                reduced.extend(bucket[index] for index in chosen)
            element.set("points", " ".join("%.3f,%.3f" % point for point in reduced))
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def _accuracy_travel_svg(run_id, axes, complete, simulated):
    height = 90 + len(axes) * 240
    title = "Commanded travel by measured home"
    if not complete:
        title = "INCOMPLETE RUN: " + title
    if simulated:
        title = "SIMULATED / NOT PRINTER MEASUREMENT: " + title
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 %d" '
             'role="img" aria-label="%s">' % (height, html.escape(title, quote=True)),
             '<rect width="1100" height="%d" fill="#fff"/>' % height,
             '<g font-family="sans-serif" fill="#182331">',
             '<text x="80" y="27" font-size="18">%s</text>' % html.escape(title),
             '<text x="80" y="49" font-size="12">Qualifier excluded; run %s</text>' % run_id]
    for row, (axis, item) in enumerate(axes.items()):
        top, bottom, left, right = 90 + row * 240, 250 + row * 240, 110, 1040
        values = [record["travel_mm"] for record in item["trials"]]
        parts.append('<text x="80" y="%d" font-size="15">%s: %d/%d measured homes</text>'
                     % (top - 14, axis.upper(), len(values), item["requested_samples"]))
        if not values:
            parts.append('<text x="110" y="%d" font-size="13">No measured homes completed</text>'
                         % (top + 70))
            continue
        pad = max((max(values) - min(values)) * 0.15, 0.005)
        low, high = min(values) - pad, max(values) + pad
        sx = lambda index: left + (index - 1) / (item["requested_samples"] - 1) * (right - left)
        sy = lambda value: bottom - (value - low) / (high - low) * (bottom - top)
        for tick in range(5):
            value = low + tick / 4.0 * (high - low)
            y = sy(value)
            parts.append('<path d="M%d %.3fH%d" stroke="#dce1e8"/>' % (left, y, right))
            parts.append('<text x="100" y="%.3f" text-anchor="end" font-size="12">%.3f</text>'
                         % (y + 4, value))
        points = " ".join("%.3f,%.3f" % (sx(index), sy(value))
                          for index, value in enumerate(values, 1))
        color = "#1769aa" if axis == "x" else "#c54238"
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6"/>'
                     % (points, color))
        for index, value in enumerate(values, 1):
            x, y = sx(index), sy(value)
            parts.append('<path d="M%.3f %.3fh6m-3 -3v6" stroke="%s" stroke-width="2"/>'
                         % (x - 3, y, color))
            if len(values) <= 15 or index in (1, len(values)) or index % 5 == 0:
                parts.append('<text x="%.3f" y="%d" text-anchor="middle" font-size="12">%d</text>'
                             % (x, bottom + 19, index))
        parts.append('<text x="575" y="%d" text-anchor="middle" font-size="12">Measured home number</text>'
                     % (bottom + 39))
        parts.append('<text x="22" y="%d" transform="rotate(-90 22 %d)" '
                     'text-anchor="middle" font-size="12">Commanded travel (mm)</text>'
                     % ((top + bottom) // 2, (top + bottom) // 2))
    parts.append('</g></svg>')
    return "\n".join(parts)


def _display(value, digits=6):
    return "unavailable" if not _finite(value) else html.escape(("%%.%dg" % digits) % value)


def _trial_entries(axes):
    for axis, item in axes.items():
        if item.get("discovery") is not None:
            yield axis, "Qualifier (excluded)", item["discovery"], item["discovery_capture_paths"], False
        for index, (record, paths) in enumerate(zip(item["trials"], item["capture_paths"]), 1):
            yield axis, "Measured home %d" % index, record, paths, True
        if item.get("rejected") is not None:
            yield axis, "Rejected home (excluded)", item["rejected"], item["rejection_capture_paths"], False


def validate_accuracy(directory, run_id, axis_results, result="complete", reason=None, simulated=False):
    """Validate current-run capture completeness without publishing any output."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid accuracy run ID")
    destination = Path(directory).expanduser().resolve()
    axes = _validate_accuracy(destination, run_id, axis_results, result, reason, simulated)
    embedded_bytes = 0
    for _axis, _label, _record, reference, _included in _trial_entries(axes):
        if reference is not None:
            embedded_bytes += len(_embedded_capture_svg(reference["svg"]).encode("utf-8"))
            if embedded_bytes > _MAX_REPORT_BYTES:
                raise ValueError("Accuracy summary exceeds its report size limit")
    return axes


def write_accuracy(directory, run_id, axis_results, result, reason=None, simulated=False):
    """Atomically publish an accuracy summary using already published captures.

    axis_results is keyed by x/y and contains requested_samples, complete,
    discovery, trials, statistics and capture_paths aligned with trials. The
    separate discovery_capture_paths and rejection_capture_paths are optional
    on an aborted run. Every capture must be a current-run write_capture output.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid accuracy run ID")
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / ("accel_homing_accuracy_%s" % run_id)
    if os.path.lexists(published):
        raise ValueError("Accuracy output already exists for this run")
    axes = validate_accuracy(destination, run_id, axis_results, result, reason, simulated)
    with tempfile.TemporaryDirectory(prefix=".accel_homing_accuracy_", dir=str(destination)) as temporary:
        paths = _write_accuracy(Path(temporary), published.name, run_id, axes, result, reason, simulated)
        if sum(Path(path).stat().st_size for path in paths.values()) > _MAX_REPORT_BYTES:
            raise ValueError("Accuracy summary exceeds its report size limit")
        os.rename(temporary, published)
    return {extension: str(published / Path(path).name) for extension, path in paths.items()}


def _write_accuracy(directory, stem, run_id, axes, result, reason, simulated):
    paths = {extension: str(directory / (stem + "." + extension))
             for extension in ("csv", "svg", "html", "json")}
    title = "Accelerometer homing: repeated commanded travel"
    if result != "complete":
        title = "INCOMPLETE / %s: %s" % (result.upper(), title)
    if simulated:
        title = "SIMULATED / NOT PRINTER MEASUREMENT: " + title
    metadata = {"run_id": run_id, "result": result, "reason": reason,
                "data_source": "SIMULATED - NOT PRINTER MEASUREMENT" if simulated else
                "captured accelerometer measurements", "travel_definition": _TRAVEL_DEFINITION,
                "travel_resolution_mm": 0.001, "qualifying_home_in_statistics": False,
                "variance_definition": _POPULATION,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "axes": axes}
    with open(paths["json"], "x", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, allow_nan=False)
    with open(paths["csv"], "x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("axis", "phase", "sample", "workflow_attempt", "included_in_statistics",
                         "commanded_travel_mm", "measured_onset_g", "measured_peak_g",
                         "configured_trigger_threshold_g", "direction", "capture_available"))
        for axis, _label, record, ref, included in _trial_entries(axes):
            writer.writerow((axis.upper(), record.get("workflow_phase"), record.get("accuracy_sample"),
                             record.get("workflow_attempt"), included, record.get("travel_mm"),
                             record.get("onset_g"), record.get("impact_peak_g"), record.get("threshold_g"),
                             record.get("direction"), ref is not None))
    travel_svg = _accuracy_travel_svg(run_id, axes, result == "complete", simulated)
    with open(paths["svg"], "x", encoding="utf-8") as stream:
        stream.write(travel_svg)
    with open(paths["html"], "x", encoding="utf-8") as stream:
        stream.write('<!doctype html><html lang="en"><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width,initial-scale=1">'
                     '<title>%s</title><style>body{font:16px system-ui,sans-serif;margin:2rem;'
                     'color:#182331;max-width:1500px}h1{font-size:1.55rem}svg{width:100%%;height:auto}'
                     'table{border-collapse:collapse;width:100%%;margin:1rem 0}th,td{padding:.55rem;'
                     'border:1px solid #ccd3dc;text-align:right}th:first-child,td:first-child{text-align:left}'
                     '.scroll{overflow-x:auto}.status{padding:.8rem;background:%s;font-weight:600}'
                     'select{font:inherit;padding:.4rem;max-width:100%%}a{color:#155a97}'
                     '[hidden]{display:none!important}.note{font-size:.9rem;color:#465568}'
                     '</style><h1>%s</h1><p class="status">%s</p><p>%s</p>' % (
                         html.escape(title), "#edf5ed" if result == "complete" else "#fff0d8",
                         html.escape(title), "COMPLETE: all requested measured homes and captures exported."
                         if result == "complete" else "INCOMPLETE / %s: requested accuracy run did not finish."
                         % html.escape(result.upper()), html.escape(_TRAVEL_DEFINITION)))
        if reason:
            stream.write('<p>Reason: %s</p>' % html.escape(reason))
        stream.write('<p>One qualifying home per axis is excluded. Statistics use only accepted measured '
                     'homes. Standard deviation and variance use the population divisor N and require at '
                     'least two measured homes. This command does not qualify or save a calibration model.</p>'
                     '<div class="scroll"><table><thead><tr><th>Axis</th><th>Homes</th><th>Min (mm)</th>'
                     '<th>Max (mm)</th><th>Mean (mm)</th><th>Range (mm)</th><th>Stddev (mm)</th>'
                     '<th>Variance (mm²)</th></tr></thead><tbody>')
        for axis, item in axes.items():
            stats = item["statistics"] or {}
            stream.write('<tr><td>%s</td><td>%d / %d</td>%s</tr>' % (
                axis.upper(), len(item["trials"]), item["requested_samples"],
                "".join('<td>%s</td>' % _display(stats.get(key)) for key in
                        ("min_mm", "max_mm", "mean_mm", "spread_mm", "stddev_mm", "variance_mm2"))))
        stream.write('</tbody></table></div>%s<h2>Recorded acceleration and configured trigger threshold</h2>'
                     '<p>The lines are captured machine-axis X/Y/Z acceleration minus the detector\'s '
                     'stationary baseline. The signed threshold is the configured trigger threshold, '
                     'not a newly calculated requirement. Detected onset and measured peak are separate '
                     'readings. Select any home to inspect its trace.</p><label for="trial">Home: </label>'
                     '<select id="trial">' % travel_svg)
        entries = list(_trial_entries(axes))
        # Open with the first measured home visible; qualifiers remain inspectable.
        initial = next((i for i, entry in enumerate(entries) if entry[4] and entry[3]), 0)
        for index, (axis, label, _record, ref, _included) in enumerate(entries):
            stream.write('<option value="trial-%d"%s>%s — %s%s</option>' % (
                index, ' selected' if index == initial else '', axis.upper(), html.escape(label),
                " (capture unavailable)" if ref is None else ""))
        stream.write('</select>')
        if not entries:
            stream.write('<p>No home completed; no acceleration trace is available.</p>')
        for index, (axis, label, record, ref, _included) in enumerate(entries):
            stream.write('<section class="trial" id="trial-%d"%s><h3>%s — %s</h3>'
                         '<p>Commanded travel: %s mm · measured onset: %s g · measured peak: %s g · '
                         'configured trigger threshold: %s g</p>' % (
                             index, ' hidden' if index != initial else '', axis.upper(), html.escape(label),
                             *(_display(record.get(key), 9) for key in
                               ("travel_mm", "onset_g", "impact_peak_g", "threshold_g"))))
            if ref is None:
                stream.write('<p>Capture unavailable. This home has no acceleration plot in this report.</p>')
            else:
                stream.write(_embedded_capture_svg(ref["svg"]))
                stream.write('<p>%s</p>' % " · ".join('<a href="%s">%s</a>' % (
                    html.escape("../" + Path(ref[extension]).parent.name + "/" +
                                Path(ref[extension]).name, quote=True), label)
                    for extension, label in (("csv", "Full-resolution raw and baseline-subtracted CSV"),
                                             ("json", "Capture metadata JSON"), ("svg", "Capture SVG"))))
            stream.write('</section>')
            if stream.tell() > _MAX_REPORT_BYTES:
                raise ValueError("Accuracy summary exceeds its report size limit")
        stream.write('<p class="note">Embedded traces retain the extrema of each display bucket; '
                     'full-resolution captured samples remain in each home\'s CSV. All plots are embedded '
                     'and require no network connection.</p><h2>Homes and recorded values</h2>'
                     '<div class="scroll"><table><thead><tr><th>Axis / home</th><th>Commanded travel (mm)</th>'
                     '<th>Onset (g)</th><th>Peak (g)</th><th>Configured threshold (g)</th></tr></thead><tbody>')
        for axis, label, record, _ref, _included in entries:
            stream.write('<tr><td>%s — %s</td>%s</tr>' % (
                axis.upper(), html.escape(label), "".join('<td>%s</td>' % _display(record.get(key), 9)
                    for key in ("travel_mm", "onset_g", "impact_peak_g", "threshold_g"))))
        stream.write('</tbody></table></div><p><a href="%s">Summary CSV</a> · '
                     '<a href="%s">Summary JSON</a> · <a href="%s">Travel SVG</a></p>'
                     '<script>document.getElementById("trial").addEventListener("change",function(){'
                     'document.querySelectorAll(".trial").forEach(function(panel){'
                     'panel.hidden=panel.id!==document.getElementById("trial").value;});});</script></html>'
                     % tuple(html.escape(Path(paths[key]).name, quote=True) for key in ("csv", "json", "svg")))
    return paths


_NOISE_WINDOWS = 3
_NOISE_WINDOW_SECONDS = 1.0
_NOISE_DEFINITION = ("Stationary accelerometer vibration: raw machine-mapped XYZ g minus "
                     "each window's own mean; not sound or acoustic noise.")
_NOISE_CANDIDATE_NOTE = ("Initial calibration candidate only - NOT a calibrated impact threshold. "
                         "Stationary measurements do not predict motor-enable or motion transients, "
                         "guarantee filtering, or bypass homing's quiet-reference checks.")


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


def _noise_capture_svg(run_id, iteration, record, delta, simulated):
    top, bottom, left, right = 85, 365, 90, 1040
    t0, duration = delta[0][0], record["duration_s"]
    peak = max(0.001, max(abs(value) for sample in delta for value in sample[1:])) * 1.08
    sx = lambda stamp: left + (stamp - t0) / duration * (right - left)
    sy = lambda value: (top + bottom) / 2.0 - value / peak * (bottom - top) / 2.0
    title = "Stationary accelerometer vibration - iteration %d of 3" % iteration
    source = "SIMULATED / NOT PRINTER MEASUREMENT" if simulated else "Captured accelerometer measurements"
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 445" role="img" '
             'aria-label="%s">' % html.escape(title, quote=True),
             '<rect width="1100" height="445" fill="#fff"/>',
             '<g font-family="sans-serif" fill="#182331">',
             '<text x="90" y="25" font-size="18">%s</text>' % html.escape(title),
             '<text x="90" y="47" font-size="12">%s; %d samples over %.6g s</text>'
             % (source, record["sample_count"], duration),
             '<text x="90" y="66" font-size="12">Own-window mean removed; signed XYZ; run %s</text>'
             % html.escape(run_id)]
    for tick in range(5):
        value = -peak + tick * peak / 2.0
        y = sy(value)
        parts.append('<path d="M90 %.3fH1040" stroke="#dce1e8"/>' % y)
        parts.append('<text x="80" y="%.3f" text-anchor="end" font-size="12">%.4g</text>'
                     % (y + 4, value))
    for tick in range(6):
        parts.append('<text x="%.3f" y="388" text-anchor="middle" font-size="12">%.3f</text>'
                     % (left + tick / 5.0 * (right - left), duration * tick / 5.0))
    parts.extend(['<text x="565" y="415" text-anchor="middle">Time since first sample (s)</text>',
                  '<text x="20" y="225" transform="rotate(-90 20 225)" '
                  'text-anchor="middle">Acceleration minus window mean (g)</text>'])
    stride = max(1, int(math.ceil(len(delta) / 4000.0)))
    for index, color in enumerate(("#1769aa", "#c54238", "#217746"), 1):
        points = []
        for offset in range(0, len(delta), stride):
            bucket = delta[offset:offset + stride]
            chosen = sorted({0, len(bucket) - 1,
                             min(range(len(bucket)), key=lambda point: bucket[point][index]),
                             max(range(len(bucket)), key=lambda point: bucket[point][index])})
            points.extend("%.3f,%.3f" % (sx(bucket[point][0]), sy(bucket[point][index]))
                          for point in chosen)
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.2"/>'
                     % (" ".join(points), color))
        parts.append('<text x="%d" y="438" fill="%s" font-size="12">%s</text>'
                     % (460 + index * 60, color, "XYZ"[index - 1]))
    parts.append('</g></svg>')
    return "\n".join(parts)


def write_noise_capture(directory, run_id, iteration, capture, record, simulated=False):
    """Atomically export one complete stationary window, preserving raw XYZ g."""
    stem = _noise_identity(run_id, iteration)
    samples, baseline, delta = _validate_noise_capture(run_id, iteration, capture, record)
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / stem
    if os.path.lexists(published):
        raise ValueError("Stationary noise output already exists for this iteration")
    with tempfile.TemporaryDirectory(prefix=".accel_homing_noise_", dir=str(destination)) as temporary:
        paths = {extension: str(Path(temporary) / (stem + "." + extension))
                 for extension in ("csv", "svg", "html", "json")}
        with open(paths["csv"], "x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("print_time_s", "elapsed_s", "raw_x_g", "raw_y_g", "raw_z_g",
                             "delta_x_g", "delta_y_g", "delta_z_g"))
            for sample, removed in zip(samples, delta):
                writer.writerow((sample[0], sample[0] - samples[0][0], *sample[1:], *removed[1:]))
        metadata = {"run_id": run_id, "noise_iteration": iteration,
                    "data_source": "SIMULATED - NOT PRINTER MEASUREMENT" if simulated else
                    "captured accelerometer measurements", "definition": _NOISE_DEFINITION,
                    "sample_count": len(samples), "baseline_g": list(baseline), "record": record,
                    "capture": {"dropped": 0, "fault": None},
                    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with open(paths["json"], "x", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, allow_nan=False)
        svg = _noise_capture_svg(run_id, iteration, record, delta, simulated)
        Path(paths["svg"]).write_text(svg, encoding="utf-8")
        title = "Stationary accelerometer vibration - iteration %d of 3" % iteration
        page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>%s</title><style>body{font:16px sans-serif;margin:2rem;color:#182331}'
                'svg{width:100%%;max-width:1500px;height:auto}</style><h1>%s</h1><p>%s</p>'
                '<p>Sensor: %s; backend: %s; %s; full scale: %s g.</p>%s'
                '<p><a href="%s">Full-resolution raw and baseline-subtracted CSV</a> · '
                '<a href="%s">Metadata JSON</a></p><p>%s</p></html>') % (
                    html.escape(title), html.escape(title), html.escape(_NOISE_DEFINITION),
                    html.escape(record["sensor"]), html.escape(record["backend"]),
                    html.escape(record["axis_context"]), _display(record["full_scale_g"]), svg,
                    html.escape(Path(paths["csv"]).name, quote=True),
                    html.escape(Path(paths["json"]).name, quote=True), html.escape(_NOISE_CANDIDATE_NOTE))
        Path(paths["html"]).write_text(page, encoding="utf-8")
        os.rename(temporary, published)
    return {extension: str(published / Path(path).name) for extension, path in paths.items()}


def _noise_capture_reference(directory, run_id, iteration, record, paths, simulated):
    stem = _noise_identity(run_id, iteration)
    if not isinstance(paths, dict):
        raise ValueError("Stationary noise window is missing capture paths")
    resolved = {}
    for extension in ("csv", "svg", "html", "json"):
        path = paths.get(extension)
        if not isinstance(path, str):
            raise ValueError("Stationary noise window is missing an output file")
        candidate = Path(path).expanduser().resolve(strict=True)
        if candidate != directory / stem / (stem + "." + extension) or not candidate.is_file():
            raise ValueError("Stationary noise capture path does not belong to this run")
        resolved[extension] = str(candidate)
    metadata = json.loads(_read_limited(resolved["json"], _MAX_CAPTURE_JSON_BYTES))
    source = "SIMULATED - NOT PRINTER MEASUREMENT" if simulated else "captured accelerometer measurements"
    if metadata.get("run_id") != run_id or metadata.get("noise_iteration") != iteration \
            or metadata.get("record") != json.loads(json.dumps(record, allow_nan=False)) \
            or metadata.get("data_source") != source or metadata.get("sample_count") != record["sample_count"] \
            or not isinstance(metadata.get("baseline_g"), list) or len(metadata["baseline_g"]) != 3 \
            or not all(_same_noise_number(metadata["baseline_g"][index], record["baseline_g"][axis])
                       for index, axis in enumerate("xyz")) \
            or metadata.get("capture") != {"dropped": 0, "fault": None}:
        raise ValueError("Stationary noise capture metadata does not match a complete valid window")
    return resolved


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


def write_noise(directory, run_id, iterations, recommendation, result, reason=None, simulated=False):
    """Publish three stationary-window charts, or an explicit incomplete report.

    iterations contains only {record, capture_paths} entries from this run's
    write_noise_capture outputs. A failed/incomplete run cannot recommend a seed.
    """
    _noise_identity(run_id, 1)
    if result not in ("complete", "aborted", "incomplete", "failed", "cancelled") \
            or (reason is not None and (not isinstance(reason, str) or len(reason) > 4000)) \
            or not isinstance(iterations, list) or len(iterations) > _NOISE_WINDOWS:
        raise ValueError("Invalid stationary noise report")
    if result == "complete" and len(iterations) != _NOISE_WINDOWS:
        raise ValueError("A complete stationary noise report requires three valid windows")
    if result != "complete" and recommendation is not None:
        raise ValueError("Incomplete stationary noise data cannot recommend a calibration candidate")
    destination = Path(directory).expanduser().resolve()
    checked, context, previous_end = [], None, None
    for iteration, item in enumerate(iterations, 1):
        if not isinstance(item, dict):
            raise ValueError("Invalid stationary noise report iteration")
        record = item.get("record")
        _check_noise_record(run_id, iteration, record)
        if previous_end is not None and record["first_sample_time_s"] <= previous_end:
            raise ValueError("Stationary noise windows overlap or repeat earlier samples")
        previous_end = record["last_sample_time_s"]
        current = tuple(record[key] for key in ("sensor", "backend", "axis_context", "full_scale_g")) + (
            json.dumps(record.get("sensor_context"), sort_keys=True, allow_nan=False),)
        if context is not None and current != context:
            raise ValueError("Stationary noise sensor context changed between windows")
        context = current
        refs = _noise_capture_reference(destination, run_id, iteration, record,
                                        item.get("capture_paths"), simulated)
        checked.append({"record": record, "capture_paths": refs})
    if result == "complete":
        _validate_noise_recommendation(recommendation, checked)
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / ("accel_homing_noise_%s" % run_id)
    if os.path.lexists(published):
        raise ValueError("Stationary noise summary already exists for this run")
    with tempfile.TemporaryDirectory(prefix=".accel_homing_noise_summary_", dir=str(destination)) as temporary:
        paths = _write_noise(Path(temporary), published.name, run_id, checked,
                             recommendation, result, reason, simulated)
        if sum(Path(path).stat().st_size for path in paths.values()) > _MAX_REPORT_BYTES:
            raise ValueError("Stationary noise summary exceeds its report size limit")
        os.rename(temporary, published)
    return {extension: str(published / Path(path).name) for extension, path in paths.items()}


def _write_noise(directory, stem, run_id, iterations, recommendation, result, reason, simulated):
    paths = {extension: str(directory / (stem + "." + extension))
             for extension in ("csv", "svg", "html", "json")}
    title = "Stationary accelerometer vibration - three-window measurement"
    status = "COMPLETE: all three stationary windows exported."
    if result != "complete":
        title = "INCOMPLETE / %s: %s" % (result.upper(), title)
        status = "INCOMPLETE / %s: no calibration candidate is recommended." % result.upper()
    if simulated:
        title = "SIMULATED / NOT PRINTER MEASUREMENT: " + title
    relative = lambda path: "../" + Path(path).parent.name + "/" + Path(path).name
    exported = [{"record": item["record"], "capture_paths": {
        extension: relative(path) for extension, path in item["capture_paths"].items()}}
        for item in iterations]
    metadata = {"run_id": run_id, "result": result, "reason": reason,
                "data_source": "SIMULATED - NOT PRINTER MEASUREMENT" if simulated else
                "captured accelerometer measurements", "definition": _NOISE_DEFINITION,
                "candidate_note": _NOISE_CANDIDATE_NOTE, "requested_windows": _NOISE_WINDOWS,
                "requested_window_duration_s": _NOISE_WINDOW_SECONDS, "iterations": exported,
                "recommendation": recommendation,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with open(paths["json"], "x", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, allow_nan=False)
    with open(paths["csv"], "x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("run_id", "iteration", "axis", "duration_s", "sample_count", "baseline_g",
                         "absolute_noise_peak_g", "noise_rms_g", "noise_peak_to_peak_g",
                         "initial_calibration_candidate_g", "candidate_is_calibrated_impact_threshold"))
        for item in iterations:
            record = item["record"]
            for axis in "xyz":
                candidate = recommendation["start_threshold_g"].get(axis) if recommendation else None
                writer.writerow((run_id, record["noise_iteration"], axis.upper(), record["duration_s"],
                                 record["sample_count"], record["baseline_g"][axis],
                                 record["noise_peak_g"][axis], record["noise_rms_g"][axis],
                                 record["noise_peak_to_peak_g"][axis], candidate, False))
    embedded = [_embedded_capture_svg(item["capture_paths"]["svg"]) for item in iterations]
    header_height = 185
    height = header_height + max(1, len(embedded)) * 445
    summary = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 %d" role="img" '
               'aria-label="Stationary accelerometer vibration windows">' % height,
               '<rect width="1100" height="%d" fill="#fff"/>' % height,
               '<g font-family="sans-serif" fill="#182331">',
               '<text x="90" y="28" font-size="18">Stationary accelerometer vibration - %d of 3 windows</text>'
               % len(iterations), '<text x="90" y="51" font-size="12">%s</text>' % html.escape(status),
               '<text x="90" y="73" font-size="12">%s</text>' % (
                   "SIMULATED / NOT PRINTER MEASUREMENT" if simulated else "Captured accelerometer measurements"),
               '<text x="90" y="95" font-size="12">%s</text>' % (
                   'Observed maximum absolute noise peaks XYZ: %s g' % ' / '.join(
                       _display(recommendation["noise_peak_g"][axis], 9) for axis in "xyz")
                   if recommendation else 'No calibration candidate is recommended from incomplete data.'),
               '<text x="90" y="117" font-size="12">%s</text>' % (
                   'Initial candidates X: %s g; Y: %s g - NOT calibrated impact thresholds' % tuple(
                       _display(recommendation["start_threshold_g"][axis], 9) for axis in "xy")
                   if recommendation else 'Initial candidates unavailable.'),
               '<text x="90" y="139" font-size="12">Peak + max(20% of peak, 0.1 g); round upward on start + n * step grid</text>',
               '<text x="90" y="161" font-size="12">Run %s; every window uses its own stationary mean</text>'
               % html.escape(run_id), '</g>']
    for index, svg in enumerate(embedded):
        panel = ET.fromstring(svg)
        panel.set("width", "1100")
        panel.set("height", "445")
        summary.append('<g transform="translate(0 %d)">%s</g>'
                       % (header_height + index * 445, ET.tostring(panel, encoding="unicode")))
    if not embedded:
        summary.append('<text x="90" y="235" font-family="sans-serif">No valid complete windows available.</text>')
    summary.append('</svg>')
    Path(paths["svg"]).write_text("\n".join(summary), encoding="utf-8")
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>%s</title><style>body{font:16px system-ui,sans-serif;margin:2rem;max-width:1500px;'
             'color:#182331}h1{font-size:1.55rem}svg{width:100%%;height:auto}table{border-collapse:collapse;'
             'width:100%%;margin:1rem 0}th,td{padding:.5rem;border:1px solid #ccd3dc;text-align:right}'
             'th:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}.status{padding:.8rem;'
             'background:#edf2f7}a{color:#155a97}details{margin:1rem 0}summary{cursor:pointer}</style>'
             '<h1>%s</h1><p class="status">%s</p><p>%s</p><p>%s</p>'
             % (html.escape(title), html.escape(title), html.escape(status),
                html.escape(_NOISE_DEFINITION), html.escape(_NOISE_CANDIDATE_NOTE))]
    if reason:
        parts.append('<p>Reason: %s</p>' % html.escape(reason))
    if recommendation:
        parts.append('<h2>Initial calibration candidate</h2><p>For each X/Y axis: take the largest absolute '
                     'noise peak across all three windows, add the greater of 20% or 0.1 g, then round upward '
                     'on the configured start + n × step grid. No downward clamp is applied. '
                     'Ordinary/manual trigger thresholds are unchanged.</p><ul>')
        for axis in "xy":
            candidate = recommendation["start_threshold_g"][axis]
            label = "%s g" % _display(candidate, 9) if candidate is not None else (
                "unavailable: sensor range unknown" if recommendation["threshold_limit_g"] is None
                else "unavailable: exceeds calibration ceiling")
            parts.append('<li>%s: observed absolute peak %s g; initial candidate %s.</li>'
                         % (axis.upper(), _display(recommendation["noise_peak_g"][axis], 9), label))
        parts.append('</ul><p>Configured start %s g; step %s g; ceiling %s g. '
                     'Use a usable seed only in a separate later ACCEL_HOMING_CALIBRATE command. '
                     'Seeds last until restart; remeasure before Y if saving X restarted Klipper.</p>'
                     % tuple(_display(recommendation[key], 9) for key in
                             ("configured_start_g", "candidate_step_g", "threshold_limit_g")))
    parts.append('<h2>Measured windows</h2><div class="scroll"><table><thead><tr>'
                 '<th>Iteration / axis</th><th>Duration (s)</th><th>Samples</th><th>Mean (g)</th>'
                 '<th>Absolute peak (g)</th><th>RMS (g)</th><th>Peak-to-peak (g)</th></tr></thead><tbody>')
    for item in iterations:
        record = item["record"]
        for axis in "xyz":
            parts.append('<tr><td>%d / %s</td><td>%s</td><td>%d</td>%s</tr>' % (
                record["noise_iteration"], axis.upper(), _display(record["duration_s"], 9), record["sample_count"],
                "".join('<td>%s</td>' % _display(record[key][axis], 9) for key in
                        ("baseline_g", "noise_peak_g", "noise_rms_g", "noise_peak_to_peak_g"))))
    parts.append('</tbody></table></div>')
    for item, svg in zip(iterations, embedded):
        record = item["record"]
        parts.append('<details open><summary>Iteration %d - signed XYZ acceleration trace</summary>'
                     '<p>Sensor: %s; backend: %s; %s; full scale: %s g.</p>%s<p>%s</p></details>' % (
                         record["noise_iteration"], html.escape(record["sensor"]),
                         html.escape(record["backend"]), html.escape(record["axis_context"]),
                         _display(record["full_scale_g"]), svg,
                         " · ".join('<a href="%s">%s</a>' % (
                             html.escape(relative(item["capture_paths"][extension]), quote=True), label)
                             for extension, label in (("csv", "Full-resolution raw and baseline-subtracted CSV"),
                                                      ("json", "Capture JSON"), ("svg", "Capture SVG")))))
    parts.append('<p>Display traces retain positive and negative extrema of each display bucket. '
                 'Each CSV keeps every original sample. Plots are embedded and need no network connection.</p>'
                 '<p><a href="%s">Summary CSV</a> · <a href="%s">Summary JSON</a> · '
                 '<a href="%s">All-window SVG</a></p></html>'
                 % tuple(html.escape(Path(paths[key]).name, quote=True) for key in ("csv", "json", "svg")))
    Path(paths["html"]).write_text("\n".join(parts), encoding="utf-8")
    return paths
