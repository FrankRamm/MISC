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
    axes = _validate_accuracy(destination, run_id, axis_results, result, reason, simulated)
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
