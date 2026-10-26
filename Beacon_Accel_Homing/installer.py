#!/usr/bin/env python3
"""Transactional installer for an existing Klipper host. Never restarts or moves it."""
import argparse
import base64
import datetime
import glob
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import uuid

MODULES = ("accel_homing.py", "beacon_accel_homing.py", "accel_homing_backends.py",
           "accel_homing_workflows.py", "accel_homing_model.py", "accel_homing_charts.py")
SENSORS = {"beacon", "adxl345", "lis2dw", "lis3dh", "mpu9250", "icm20948"}
SAVE_MARKER = "#*# <---------------------- SAVE_CONFIG ---------------------->"
MANAGED = "accel_homing.cfg"
SECTION = re.compile(r"^\s*\[([^]]+)\]\s*(?:[#;].*)?$")
OPTION = re.compile(r"^(\s*)([\w]+)(\s*[:=]\s*)(.*?)(\r?\n)?$")


def load_motion_policy():
    # The backend module has only definitions/math imports at module scope.
    # Load the sibling file, never a possibly different installed Klipper copy.
    path = Path(__file__).resolve().with_name("accel_homing_backends.py")
    spec = importlib.util.spec_from_file_location("accel_homing_install_policy", path)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


class InstallError(Exception):
    pass


def canonical(value, base=None):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base or Path.cwd()) / path).resolve()


def command_instance(argv, cwd, origin, pid=None, service=None):
    index = next((i for i, arg in enumerate(argv) if Path(arg).name == "klippy.py"), None)
    if index is None:
        return None
    script = canonical(argv[index], cwd)
    if script.parent.name != "klippy":
        return None
    positional, api_socket = [], None
    values = {"-l", "-a", "-I", "-i", "-d", "-o", "--logfile", "--api-server", "--input-tty", "--dictionary"}
    arguments = iter(argv[index + 1:])
    for arg in arguments:
        if arg in values:
            value = next(arguments, None)
            if value is None:
                return None
            if arg in ("-a", "--api-server"):
                api_socket = canonical(value, cwd)
        elif arg.startswith("--api-server="):
            api_socket = canonical(arg.split("=", 1)[1], cwd)
        elif not arg.startswith("-"):
            positional.append(arg)
    if len(positional) != 1:
        return None
    return dict(repo=script.parent.parent, config=canonical(positional[0], cwd), socket=api_socket,
                pid=pid, service=service, origin=origin, executable=argv[0])


def proc_instances(proc_root=Path("/proc")):
    instances = []
    if not proc_root.is_dir():
        return instances
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = [p.decode("utf-8", "surrogateescape") for p in
                    (process / "cmdline").read_bytes().split(b"\0") if p]
            if not any(Path(arg).name == "klippy.py" for arg in argv):
                continue
            service = None
            if (process / "cgroup").is_file():
                names = re.findall(r"/([^/\n]+\.service)(?:/|$)", (process / "cgroup").read_text())
                service = names[-1] if names else None
            found = command_instance(argv, (process / "cwd").resolve(),
                                     "running process " + process.name, int(process.name), service)
            if found:
                instances.append(found)
        except (OSError, ValueError):
            continue
    return instances


def service_instances(home):
    directories = (Path("/etc/systemd/system"), Path("/lib/systemd/system"),
                   Path("/usr/lib/systemd/system"), home / ".config/systemd/user")
    instances, seen = [], set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for unit in sorted(directory.glob("*.service")):
            if unit.name in seen:
                continue
            seen.add(unit.name)
            try:
                text = unit.read_text()
                if "klippy.py" not in text:
                    continue
                # Drop-ins/environment expansion require actual live arguments, not guessing.
                if any((directory / (unit.name + ".d")).glob("*.conf")):
                    continue
                text = re.sub(r"\\\r?\n\s*", " ", text)
                command = re.findall(r"^\s*ExecStart\s*=\s*(.+)$", text, re.M)
                working = re.findall(r"^\s*WorkingDirectory\s*=\s*(.+)$", text, re.M)
                if len(command) != 1 or "$" in command[0] or "%" in command[0]:
                    continue
                found = command_instance(shlex.split(command[0]), working[-1].strip() if working else "/",
                                         str(unit), service=unit.name)
                if found:
                    instances.append(found)
            except (OSError, ValueError):
                continue
    return instances


def active_service(service):
    if not service or shutil.which("systemctl") is None:
        return None
    result = subprocess.run(["systemctl", "show", service, "--property=ActiveState", "--value"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def describe(instances):
    return "\n".join("  %s: %s ; %s ; service=%s" %
                     (p["origin"], p["repo"], p["config"], p["service"] or "unknown") for p in instances)


def discover_target(args, home=None, processes=None, services=None):
    home = canonical(home or Path.home())
    processes = proc_instances() if processes is None else processes
    services = service_instances(home) if services is None else services
    repo = canonical(args.klipper_path) if args.klipper_path else None
    config = canonical(args.config) if args.config else None
    service = args.service
    if service and not service.endswith(".service"):
        service += ".service"

    def matches(item):
        return ((repo is None or item["repo"] == repo) and (config is None or item["config"] == config)
                and (service is None or item["service"] == service))

    running = [p for p in processes if matches(p)]
    if len(running) > 1:
        raise InstallError("Multiple running instances match. Set --klipper-path, --config and --service.\n" + describe(running))
    if running:
        selected = dict(running[0])
    else:
        configured = {(p["repo"], p["config"], p["service"]): p for p in services if matches(p)}
        if len(configured) > 1:
            raise InstallError("Multiple services match. Set --klipper-path, --config and --service.\n" + describe(configured.values()))
        if configured:
            selected = dict(next(iter(configured.values())))
        else:
            if service:
                raise InstallError("Cannot resolve service %s. Supply the actual instance arguments; "
                                   "environment-based units/drop-ins require a readable running /proc command." % service)
            repos = [repo] if repo else sorted({canonical(p) for pattern in ("klipper", "kalico", "klipper-*", "kalico-*")
                                               for p in home.glob(pattern) if (p / "klippy/klippy.py").is_file()})
            configs = [config] if config else sorted({canonical(p) for pattern in (
                "printer_data/config/printer.cfg", "printer_data*/config/printer.cfg", "printer_*/config/printer.cfg", "printer.cfg")
                for p in home.glob(pattern) if p.is_file()})
            if len(repos) != 1 or len(configs) != 1:
                raise InstallError("Cannot choose one checkout/main config. Set --klipper-path /path/to/klipper "
                                   "--config /path/to/printer.cfg (and --service on multi-instance hosts). "
                                   "Checkouts: %s; configs: %s" % (repos, configs))
            selected = dict(repo=repos[0], config=configs[0], socket=None, service=None, pid=None,
                            executable=None, origin="explicit paths/common directories (stopped host)")
        overlap = [p for p in processes if p["repo"] == selected["repo"] or p["config"] == selected["config"]]
        if overlap:
            raise InstallError("Selected paths overlap a live instance with different arguments/service. "
                               "Use exact --klipper-path, --config and --service.\n" + describe(overlap))
    if args.klippy_socket:
        selected["socket"] = canonical(args.klippy_socket)
    if not (selected["repo"] / "klippy/klippy.py").is_file() or not (selected["repo"] / "klippy/extras").is_dir():
        raise InstallError("Not a compatible checkout (requires klippy/klippy.py and klippy/extras): %s" % selected["repo"])
    if not selected["config"].is_file():
        raise InstallError("Main config not found: %s" % selected["config"])
    return selected


class Section:
    def __init__(self, document, name, start, end):
        self.document, self.name, self.start, self.end = document, name, start, end
        self.options = {}
        for index in range(start + 1, end):
            if document.lines[index].lstrip().startswith(("#", ";")):
                continue
            match = OPTION.match(document.lines[index])
            if match:
                self.options.setdefault(match.group(2).lower(), []).append((index, match))

    def get(self, name, default=None):
        options = self.options.get(name.lower(), [])
        if len(options) > 1:
            raise InstallError("Duplicate %s in [%s], %s; resolve before installation." % (name, self.name, self.document.path))
        return re.split(r"\s*[#;]", options[0][1].group(4), maxsplit=1)[0].strip() if options else default

    def replace(self, name, value):
        if self.get(name) == str(value):
            return
        entries = self.options.get(name.lower(), [])
        line = "%s: %s%s" % (name, value, self.document.newline)
        if entries:
            index, match = entries[0]
            old = self.document.lines[index].lstrip().rstrip("\r\n")
            self.document.replacements[index] = "# accel_homing previous: " + old + self.document.newline + match.group(1) + line
        else:
            self.document.insertions.setdefault(self.end, []).append(line)

    def disable(self, name):
        if self.get(name) is not None:
            index = self.options[name.lower()][0][0]
            self.document.replacements[index] = "# accel_homing previous: " + self.document.lines[index].lstrip()


class ConfigDocument:
    def __init__(self, path):
        self.path = canonical(path)
        self.original = self.path.read_bytes()
        self.text = self.original.decode("utf-8")
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        self.lines = self.text.splitlines(keepends=True)
        self.replacements, self.insertions, self.sections = {}, {}, []
        heads, self.active_end = [], len(self.lines)
        for index, line in enumerate(self.lines):
            if line.startswith(SAVE_MARKER) or line.startswith("#*# DO NOT EDIT"):
                self.active_end = index
                break
            match = SECTION.match(line)
            if match:
                heads.append((index, match.group(1).strip()))
        for i, (start, name) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else self.active_end
            self.sections.append(Section(self, name, start, end))

    def rendered(self):
        output = []
        for index in range(len(self.lines) + 1):
            additions = self.insertions.get(index, [])
            if additions and output and not output[-1].endswith(("\n", "\r")):
                output.append(self.newline)
            output.extend(additions)
            if index < len(self.lines):
                output.append(self.replacements.get(index, self.lines[index]))
        return "".join(output).encode("utf-8")


class ConfigGraph:
    def __init__(self, main):
        self.main, self.documents, self.sections, self.include_patterns = canonical(main), {}, {}, []
        self._visit(self.main, [])

    def _visit(self, path, stack):
        path = canonical(path)
        if path in stack:
            raise InstallError("Recursive config include: " + " -> ".join(map(str, stack + [path])))
        if path in self.documents:
            raise InstallError("Config included more than once: %s. Resolve duplicate includes." % path)
        try:
            document = ConfigDocument(path)
        except (OSError, UnicodeError) as exc:
            raise InstallError("Cannot read UTF-8 config %s: %s" % (path, exc))
        self.documents[path] = document
        for section in document.sections:
            if section.name.lower().startswith("include "):
                pattern = os.path.expanduser(section.name[8:].strip())
                if not Path(pattern).is_absolute():
                    pattern = str(path.parent / pattern)
                self.include_patterns.append(pattern)
                matches = sorted(glob.glob(pattern))
                if not matches:
                    raise InstallError("Include has no matches in %s: %s" % (path, section.name))
                for match in matches:
                    self._visit(match, stack + [path])
            else:
                self.sections.setdefault(section.name.lower(), []).append(section)

    def one(self, name, required=True):
        sections = self.sections.get(name.lower(), [])
        if not sections and not required:
            return None
        if len(sections) != 1:
            raise InstallError("Expected exactly one [%s], found %d. Resolve duplicate/missing sections." % (name, len(sections)))
        return sections[0]


def choose_sensor(graph, selected=None, existing=None):
    configured = [name for name in graph.sections if name.split()[0] in SENSORS]
    preferred = selected
    if preferred is None and existing:
        preferred = existing.get("accel_chip")
        if not preferred and existing.name.lower() == "beacon_accel_homing":
            preferred = existing.get("beacon", "beacon")
    if preferred and preferred.lower() != "auto":
        preferred = preferred.lower().strip()
        if preferred not in graph.sections:
            raise InstallError("Accelerometer [%s] is not configured. Configure its sensor/firmware first, "
                               "or select --accel-chip from: %s" % (preferred, ", ".join(configured) or "none"))
        return graph.one(preferred).name
    if len(configured) != 1:
        raise InstallError("Expected one configured accelerometer, found %d (%s). Use --accel-chip "
                           "'adxl345 toolhead' (full existing section). A Cartographer/scanner probe alone "
                           "is not an accelerometer; configure its separate [adxl345 ...] first." %
                           (len(configured), ", ".join(configured) or "none"))
    return graph.one(configured[0]).name


def make_plan(source, selected, args):
    source = canonical(source)
    for module in MODULES:
        if not (source / module).is_file():
            raise InstallError("Incomplete package: missing %s in %s" % (module, source))
    graph = ConfigGraph(selected["config"])
    overrides = [name for name in graph.sections if re.match(r"^(homing_override|safe_z_home|gcode_macro\s+g28)$", name, re.I)]
    if overrides and not args.acknowledge_homing_override:
        raise InstallError("Custom homing path: %s. No files changed. Review/adapt its G28, current "
                           "changes and moves; only then use --acknowledge-homing-override. The "
                           "installer cannot validate macro behavior, and calibration/test/chart "
                           "workflows require stock G28 even after this acknowledgement." % ", ".join(overrides))
    plugins = [graph.one(name, False) for name in ("accel_homing", "beacon_accel_homing")]
    plugins = [section for section in plugins if section]
    if len(plugins) > 1:
        raise InstallError("Use either [accel_homing] or [beacon_accel_homing], not both.")
    existing = plugins[0] if plugins else None
    sensor = choose_sensor(graph, args.accel_chip, existing)
    family = sensor.split()[0].lower()
    if not any((selected["repo"] / "klippy" / location / (family + ".py")).is_file()
               or (selected["repo"] / "klippy" / location / family / "__init__.py").is_file()
               for location in ("extras", "plugins")):
        raise InstallError("Configured [%s] has no installed driver in klippy/extras or klippy/plugins. "
                           "Install its normal driver first." % sensor)
    changes, messages = {}, []
    prefix = "beacon_accel" if existing and existing.name.lower() == "beacon_accel_homing" else "accel_homing"
    if existing:
        existing.replace("accel_chip", sensor)
    else:
        managed = selected["config"].parent / MANAGED
        if managed.exists() or managed.is_symlink():
            raise InstallError("%s exists but is not included; review/include or rename it. It will not be overwritten." % managed)
        content = ("# Managed by the accelerometer homing installer. Edit this file normally.\n"
                   "# Independent overrides; leave commented to use SAVE_CONFIG calibration models.\n"
                   "[accel_homing]\naccel_chip: " + sensor + "\n"
                   "#x_threshold: 8.0\n#y_threshold: 8.0\n#model: default\n")
        changes[managed] = {"kind": "file", "data": content.encode("utf-8")}
        if not any(managed.match(pattern) for pattern in graph.include_patterns):
            main = graph.documents[selected["config"]]
            main.insertions.setdefault(main.active_end, []).append(main.newline + "[include " + MANAGED + "]" + main.newline + main.newline)
    if family != "beacon":
        policy = load_motion_policy()
        printer = graph.one("printer")
        try:
            velocity = float(printer.get("max_velocity", "nan"))
            acceleration = float(printer.get("max_accel", "nan"))
            homing_accel = float(existing.get("homing_accel", "5000")) if existing else 5000.0
        except ValueError:
            raise InstallError("Generic homing requires numeric printer max_velocity/max_accel and plugin homing_accel.")
        if not all(math.isfinite(v) and v > 0 for v in (velocity, acceleration)) or not math.isfinite(homing_accel) or homing_accel < 0:
            raise InstallError("Generic homing requires finite positive printer max_velocity/max_accel and nonnegative homing_accel.")
        if homing_accel:
            acceleration = min(acceleration, homing_accel)
        age = float(existing.get("max_sample_age", "0.020")) if existing else 0.020
        travel = float(existing.get("max_report_travel", "1.0")) if existing else 1.0
        interval = float(existing.get("stream_interval", "0.005")) if existing else 0.005
        if (not all(math.isfinite(v) for v in (age, travel, interval))
                or not 0.005 <= age <= 0.050 or not 0.1 <= travel <= 2.0
                or not 0.001 <= interval <= 0.020 or interval >= age):
            raise InstallError("Generic settings require max_sample_age 0.005..0.050s, "
                               "max_report_travel 0.1..2mm, stream_interval 0.001..0.020s "
                               "and stream_interval < max_sample_age.")
        cap = min(50.0, travel / age)
    for axis in args.axes:
        rail = graph.one("stepper_" + axis)
        rail.replace("endstop_pin", prefix + ":" + axis)
        rail.replace("homing_retract_dist", "0")
        for key in ("homing_accel", "min_home_dist"):
            rail.disable(key)
        original_speed = float(rail.get("homing_speed", "5"))
        speed = args.homing_speed if args.homing_speed is not None else original_speed
        if not math.isfinite(speed) or speed <= 0:
            raise InstallError("Homing speeds must be finite and positive.")
        if family != "beacon":
            effective = min(speed, velocity)
            if args.homing_speed is not None and effective > cap:
                raise InstallError("--homing-speed exceeds generic sample-age/travel limit (%.3g mm/s)." % cap)
            chosen = min(effective, cap)
            budget = policy.motion_startup_request_travel(chosen, acceleration, age, interval)
            if not math.isfinite(budget):
                raise InstallError("Configured velocity/acceleration cannot produce a finite startup travel budget.")
            if budget > policy.STARTUP_REQUEST_TRAVEL_LIMIT:
                if args.homing_speed is not None:
                    raise InstallError("--homing-speed needs %.3g mm before the startup stop request, "
                                       "exceeding the fixed %.1f mm policy at effective acceleration %.3g. "
                                       "Choose a lower speed; no files changed." %
                                       (budget, policy.STARTUP_REQUEST_TRAVEL_LIMIT, acceleration))
                low, high = 0.0, chosen
                for unused in range(64):
                    middle = (low + high) / 2.0
                    if policy.motion_startup_request_travel(middle, acceleration, age, interval) <= policy.STARTUP_REQUEST_TRAVEL_LIMIT - 0.000001:
                        low = middle
                    else:
                        high = middle
                chosen = low
                if chosen <= 0:
                    raise InstallError("Configured acceleration cannot produce a positive compatible homing speed.")
            if chosen < effective:
                speed = chosen
                messages.append("Capped %s homing_speed %.6g -> %.6g mm/s for generic report/startup "
                                "policies at %.6g mm/s²; this is not a certified stopping distance." %
                                (axis.upper(), original_speed, speed, acceleration))
        if args.homing_speed is not None or speed != original_speed:
            rail.replace("homing_speed", "%.12g" % speed)
        actual_speed = min(speed, velocity) if family != "beacon" else speed
        if actual_speed < 50:
            messages.append("%s speed %.3g mm/s may produce soft, indistinguishable impacts. "
                            "Supervised calibration is required." % (axis.upper(), actual_speed))
        for name in graph.sections:
            if re.match(r"^tmc\w+ stepper_" + axis + r"\d*$", name):
                driver = graph.one(name)
                for key in ("diag_pin", "diag0_pin", "diag1_pin", "home_current"):
                    driver.disable(key)
    for document in graph.documents.values():
        data = document.rendered()
        if data != document.original:
            changes[document.path] = {"kind": "file", "data": data}
    for module in MODULES:
        link, wanted = selected["repo"] / "klippy/extras" / module, str(source / module)
        if link.is_symlink():
            if canonical(link) == canonical(wanted):
                continue
            if not args.replace_existing_links:
                raise InstallError("%s links to %s. Review it, then --replace-existing-links to back up/replace." % (link, os.readlink(link)))
        elif link.exists():
            raise InstallError("Refusing to overwrite non-symlink plugin %s. Resolve manually." % link)
        changes[link] = {"kind": "symlink", "target": wanted}
    exclude = selected["repo"] / ".git/info/exclude"
    if exclude.parent.is_dir():
        before = exclude.read_bytes() if exclude.exists() else b""
        text = before.decode("utf-8")
        missing = ["klippy/extras/" + name for name in MODULES if "klippy/extras/" + name not in text.splitlines()]
        if missing:
            text += ("\n" if text and not text.endswith("\n") else "") + "\n".join(missing) + "\n"
            changes[exclude] = {"kind": "file", "data": text.encode("utf-8"),
                                "expected": snapshot(exclude)}
            if changes[exclude]["expected"].get("kind") == "file":
                changes[exclude]["expected"]["data"] = base64.b64encode(before).decode("ascii")
    for path, change in changes.items():
        change.setdefault("expected", snapshot(path))
        if path in graph.documents:
            change["expected"]["data"] = base64.b64encode(graph.documents[path].original).decode("ascii")
    return changes, messages, sensor


def webhook_query(api_path, method, params=None):
    request = {"id": 1, "method": method, "params": params or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3.0)
        client.connect(str(api_path))
        client.sendall(json.dumps(request).encode("utf-8") + b"\x03")
        data = b""
        while len(data) < 1024 * 1024:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
            while b"\x03" in data:
                packet, data = data.split(b"\x03", 1)
                response = json.loads(packet)
                if response.get("id") == 1:
                    if "error" in response:
                        raise InstallError("Klipper status query failed: %s" % response["error"])
                    return response.get("result", {})
    raise InstallError("No complete reply from Klipper API socket %s" % api_path)


def assert_idle(selected, query=webhook_query, service_state=active_service):
    state = service_state(selected["service"])
    active = selected["pid"] is not None or state in ("active", "activating", "reloading")
    if not active:
        if selected["service"] and state is None:
            raise InstallError("Cannot verify %s is stopped. Inspect/stop it and retry with readable systemd status." % selected["service"])
        return "Selected Klipper is stopped (no live process/active service detected)."
    if not selected["socket"]:
        raise InstallError("Running Klipper has no known API socket. Set --klippy-socket /path/from/-a "
                           "or stop the selected service. Config writes require idle evidence.")
    try:
        info = query(selected["socket"], "info")
        if not info.get("config_file") or canonical(info["config_file"]) != selected["config"]:
            raise InstallError("API socket belongs to a different/unknown config (%s). "
                               "Use the selected instance's --klippy-socket; no files changed." % info.get("config_file"))
        if info.get("klipper_path") and canonical(info["klipper_path"]) != selected["repo"]:
            raise InstallError("API socket Klipper checkout does not match selected --klipper-path.")
        if info.get("state") != "ready":
            raise InstallError("Klipper is not ready/idle (%s); stop it before repairing config." % info.get("state", "unknown"))
        objects = query(selected["socket"], "objects/list").get("objects", [])
        names = [name for name in ("idle_timeout", "print_stats", "pause_resume") if name in objects]
        if "idle_timeout" not in names:
            raise InstallError("No idle_timeout evidence; stop the selected service first.")
        status = query(selected["socket"], "objects/query", {"objects": {name: None for name in names}}).get("status", {})
        idle = status.get("idle_timeout", {}).get("state")
        printing = status.get("print_stats", {}).get("state", "standby")
        paused = status.get("pause_resume", {}).get("is_paused", False)
        if idle not in ("Idle", "Ready") or printing not in ("standby", "complete", "cancelled", "error") or paused:
            raise InstallError("Refusing writes: printing/paused/unknown state (idle_timeout=%s, print_stats=%s, paused=%s)." % (idle, printing, paused))
    except (OSError, ValueError) as exc:
        raise InstallError("Cannot obtain read-only idle evidence from %s: %s. Stop the selected service and retry." % (selected["socket"], exc))
    return "Selected live Klipper reports ready/idle (read-only API query)."


def snapshot(path):
    path = Path(path)
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.exists():
        if not path.is_file():
            raise InstallError("Expected file/symlink, found directory/device: %s" % path)
        return {"kind": "file", "data": base64.b64encode(path.read_bytes()).decode("ascii"), "mode": stat.S_IMODE(path.stat().st_mode)}
    return {"kind": "absent"}


def state_matches(path, expected):
    return {k: v for k, v in snapshot(path).items() if k != "mode"} == {k: v for k, v in expected.items() if k != "mode"}


def write_state(path, state):
    path = Path(path)
    if state["kind"] == "absent":
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    temporary = path.parent / ("." + path.name + ".accel-" + uuid.uuid4().hex)
    try:
        if state["kind"] == "symlink":
            os.symlink(state["target"], temporary)
        elif state["kind"] == "file":
            with open(temporary, "xb") as stream:
                os.chmod(temporary, state.get("mode", 0o600))
                stream.write(base64.b64decode(state["data"]))
                stream.flush()
                os.fsync(stream.fileno())
        else:
            raise InstallError("Invalid transaction state")
        os.replace(temporary, path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def save_manifest(directory, manifest):
    data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    write_state(directory / "manifest.json", {"kind": "file", "mode": 0o600, "data": base64.b64encode(data).decode("ascii")})


def apply_plan(changes, config, writer=write_state):
    if not changes:
        return None
    entries = []
    for path, change in changes.items():
        before, after = snapshot(path), dict(change)
        expected = after.pop("expected", before)
        if not state_matches(path, expected):
            raise InstallError("%s changed after planning. Retry after editor/SAVE_CONFIG finishes." % path)
        if not os.access(path.parent, os.W_OK):
            raise InstallError("No write access to %s. Run as Klipper service user; no sudo is used." % path.parent)
        if after["kind"] == "file":
            after["data"], after["mode"] = base64.b64encode(after["data"]).decode("ascii"), before.get("mode", 0o600)
        entries.append(dict(path=str(path), before=before, after=after))
    parent = config.parent / ".accel_homing_backups"
    parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = parent / (stamp + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    manifest = dict(version=1, config=str(config), status="prepared", created_utc=stamp, changes=entries)
    save_manifest(backup, manifest)
    applied = []
    try:
        for entry in entries:
            path = Path(entry["path"])
            if not state_matches(path, entry["before"]):
                raise InstallError("%s changed during installation. Retry after editor/SAVE_CONFIG finishes." % path)
            writer(path, entry["after"])
            applied.append(entry)
        manifest["status"] = "installed"
        save_manifest(backup, manifest)
    except Exception as exc:
        errors = []
        for entry in reversed(applied):
            try:
                if not state_matches(Path(entry["path"]), entry["after"]):
                    raise InstallError("changed concurrently; preserving current contents")
                write_state(Path(entry["path"]), entry["before"])
            except Exception as undo:
                errors.append("%s: %s" % (entry["path"], undo))
        manifest.update(status="rollback_incomplete" if errors else "rolled_back_after_failure", failure=str(exc), rollback_errors=errors)
        save_manifest(backup, manifest)
        raise InstallError("Installation failed; %s. Backup %s. Cause: %s%s" %
                           ("rollback needs repair" if errors else "applied changes restored", backup, exc, "; ".join(errors)))
    return backup


def rollback(backup, dry_run=False, selected=None):
    backup = canonical(backup)
    if backup.is_file():
        backup = backup.parent
    manifest = json.loads((backup / "manifest.json").read_text())
    if manifest.get("version") != 1 or manifest.get("status") != "installed":
        raise InstallError("Not an installed transaction: %s" % backup)
    if selected and canonical(manifest["config"]) != selected["config"]:
        raise InstallError("Backup belongs to a different config: %s" % manifest["config"])
    for entry in manifest["changes"]:
        if not state_matches(Path(entry["path"]), entry["after"]):
            raise InstallError("%s changed after installation. Refusing to overwrite later edits; restore selected "
                               "before states from %s manually." % (entry["path"], backup / "manifest.json"))
    if dry_run:
        return
    done = []
    try:
        for entry in reversed(manifest["changes"]):
            if not state_matches(Path(entry["path"]), entry["after"]):
                raise InstallError("%s changed during rollback; preserving current contents" % entry["path"])
            write_state(Path(entry["path"]), entry["before"])
            done.append(entry)
    except Exception as exc:
        errors = []
        for entry in reversed(done):
            try:
                if not state_matches(Path(entry["path"]), entry["before"]):
                    raise InstallError("changed concurrently; preserving current contents")
                write_state(Path(entry["path"]), entry["after"])
            except Exception as repair:
                errors.append("%s: %s" % (entry["path"], repair))
        intact = all(state_matches(Path(e["path"]), e["after"]) for e in manifest["changes"])
        manifest.update(status="installed" if intact else "rollback_incomplete", failure=str(exc), rollback_errors=errors)
        save_manifest(backup, manifest)
        raise InstallError("Rollback failed; concurrent changes were preserved. Inspect backup %s. Cause: %s%s" %
                           (backup, exc, "; ".join(errors)))
    manifest["status"] = "rolled_back"
    save_manifest(backup, manifest)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klipper-path", default=os.environ.get("KLIPPER_PATH"), help="Klipper/Kalico/compatible checkout")
    parser.add_argument("--config", default=os.environ.get("PRINTER_CONFIG"), help="main printer.cfg")
    parser.add_argument("--service", default=os.environ.get("KLIPPER_SERVICE"), help="exact systemd instance")
    parser.add_argument("--klippy-socket", default=os.environ.get("KLIPPY_SOCKET"), help="read-only Klipper API socket (-a)")
    parser.add_argument("--accel-chip", help="full configured section, e.g. 'adxl345 toolhead'")
    parser.add_argument("--axes", default="x,y", help="x,y (default), x or y")
    parser.add_argument("--homing-speed", type=float, help="explicit speed; generic cap still applies")
    parser.add_argument("--acknowledge-homing-override", action="store_true", help="you reviewed custom G28/homing_override behavior")
    parser.add_argument("--replace-existing-links", action="store_true", help="back up/replace plugin links from another source")
    parser.add_argument("--dry-run", action="store_true", help="validate/print plan, no writes")
    parser.add_argument("--rollback", metavar="BACKUP_DIRECTORY", help="restore one unchanged installation transaction")
    parser.add_argument("-u", "--uninstall", action="store_true", help="restore latest installer transaction")
    args = parser.parse_args(argv)
    args.axes = [axis.strip().lower() for axis in args.axes.split(",")]
    if not args.axes or len(set(args.axes)) != len(args.axes) or any(axis not in ("x", "y") for axis in args.axes):
        parser.error("--axes must be x,y, x or y")
    if args.rollback and args.uninstall:
        parser.error("choose --rollback or --uninstall")
    return args


def main(argv=None):
    args = arguments(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise InstallError("Run as the Klipper service user, not root. The installer never uses sudo.")
    selected = discover_target(args)
    print("Checkout: %s\nMain config: %s\nDetection: %s\nService: %s\nExecutable: %s" %
          (selected["repo"], selected["config"], selected["origin"], selected["service"] or "none", selected["executable"] or "stopped host"))
    if args.uninstall:
        candidates = []
        for file in (selected["config"].parent / ".accel_homing_backups").glob("*/manifest.json"):
            saved = json.loads(file.read_text())
            if saved.get("status") == "installed" and canonical(saved["config"]) == selected["config"]:
                candidates.append(file.parent)
        if not candidates:
            raise InstallError("No installed backup found. Use --rollback BACKUP_DIRECTORY or restore your original config manually.")
        args.rollback = str(sorted(candidates)[-1])
    if args.rollback:
        rollback(args.rollback, True, selected)
        if not args.dry_run:
            print(assert_idle(selected))
            rollback(args.rollback, False, selected)
        print(("Would restore: " if args.dry_run else "Restored: ") + args.rollback)
        return 0
    changes, messages, sensor = make_plan(Path(__file__).resolve().parent, selected, args)
    print("Accelerometer: [%s] (physical mounting/orientation not inferred)" % sensor)
    for message in messages:
        print("Note: " + message)
    for path, change in changes.items():
        print(("Link " if change["kind"] == "symlink" else "Edit ") + str(path))
    if args.dry_run:
        print("Dry run: no writes, restart, or movement.")
        return 0
    if not changes:
        print("Already installed; no changes, restart, or movement.")
        return 0
    print(assert_idle(selected))
    backup = apply_plan(changes, selected["config"])
    print("Installed. Backup: %s" % backup)
    print("Review the config and sensor mounting/orientation. When idle, run RESTART yourself. "
          "No restart, homing, firmware flashing, or calibration was performed.")
    print("Restore: bash %s --config %s --klipper-path %s --rollback %s" %
          tuple(shlex.quote(str(v)) for v in (Path(__file__).resolve().parent / "install.sh", selected["config"], selected["repo"], backup)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (InstallError, OSError, ValueError) as error:
        print("Installation aborted: %s" % error, file=sys.stderr)
        sys.exit(1)
