# Portable chart output for accelerometer homing. GPLv3.
"""Write actual sensor samples without requiring matplotlib or numpy."""
import csv
import datetime
import html
import json
import math
import os
import re
import tempfile
from pathlib import Path


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


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
            parts.append('<path d="M80 %.3fH1040" stroke="#6b5090" stroke-dasharray="6 4"/>' % y)
            parts.append('<text x="1040" y="%.3f" text-anchor="end" font-size="12" '
                         'fill="#6b5090">%+.2f g threshold</text>' % (y - 3, limit))
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
