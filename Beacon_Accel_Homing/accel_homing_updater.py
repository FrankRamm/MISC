"""Managed Moonraker git transport and narrowly owned installer configuration.

All commands are argv-only. The installer supplies idle/identity guards before
source/config writes. Importing this module never contacts or changes a host.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid


def _current_users():
    if not hasattr(os, "getuid"):
        return None
    import pwd
    uid = os.getuid()
    return {str(uid), pwd.getpwuid(uid).pw_name}

ORIGIN = "https://github.com/FrankRamm/MISC.git"
BRANCH = "codex/universal-accel-homing"
SECTION = "update_manager accel_homing"
INCLUDE = "accel_homing_update.cfg"
RECEIPT = ".accel_homing_updater.json"


def _fail(api, message):
    raise api.InstallError("Moonraker updater: " + message)


def instance_key(selected):
    value = str(selected["repo"]) + "\0" + str(selected["config"])
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def managed_path(selected):
    return Path.home() / ".local/share/accel-homing/managed" / instance_key(selected) / "repo"


def _moon_command(argv, cwd, api, pid=None, service=None, scope=None, start=None):
    indexes = [i for i, value in enumerate(argv) if Path(value).name == "moonraker.py"]
    indexes += [i + 1 for i, value in enumerate(argv[:-1]) if value == "-m" and argv[i + 1] == "moonraker"]
    if len(indexes) != 1:
        return None
    options, iterator = {}, iter(argv[indexes[0] + 1:])
    for value in iterator:
        name, separator, inline = value.partition("=")
        if name in ("-d", "--datapath", "-c", "--configfile"):
            options[name] = inline if separator else next(iterator, "")
    raw_data = options.get("-d", options.get("--datapath"))
    if not raw_data:
        return None
    data = api.canonical(raw_data, cwd)
    raw_config = options.get("-c", options.get("--configfile"))
    config = api.canonical(raw_config, cwd) if raw_config else data / "config/moonraker.conf"
    try:
        graph = api.ConfigGraph(config)
        server = graph.one("server")
        raw_socket = server.get("klippy_uds_address", str(data / "comms/klippy.sock"))
        if any(token in raw_socket for token in ("$", "%", "{", "}")):
            return None
        socket = Path(raw_socket).expanduser()
        if not socket.is_absolute():
            return None
    except (api.InstallError, OSError, ValueError):
        return None
    return dict(config=config, data=data, socket=socket.resolve(), pid=pid,
                service=service, service_scope=scope, start_time=start,
                executable=argv[0], argv=list(argv), cwd=str(cwd), graph=graph)


def moonraker_instances(api, proc_root=Path("/proc"), unit_directories=None):
    found = []
    if proc_root.is_dir():
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            try:
                if hasattr(os, "getuid") and process.stat().st_uid != os.getuid():
                    continue
                raw = (process / "cmdline").read_bytes()
                if len(raw) > 65536:
                    continue
                argv = [part.decode("utf-8") for part in raw.split(b"\0") if part]
                groups = (process / "cgroup").read_text() if (process / "cgroup").is_file() else ""
                services = re.findall(r"/([^/\n]+\.service)(?:/|$)", groups)
                fields = (process / "stat").read_text().rsplit(") ", 1)[1].split()
                item = _moon_command(argv, (process / "cwd").resolve(), api, int(process.name),
                                     services[-1] if services else None,
                                     "user" if "/user.slice/" in groups else "system", fields[19])
                if item:
                    found.append(item)
            except (OSError, UnicodeError, ValueError, IndexError):
                continue
    directories = unit_directories or (Path("/etc/systemd/system"), Path("/lib/systemd/system"),
                                       Path("/usr/lib/systemd/system"), Path.home() / ".config/systemd/user")
    seen = {item["service"] for item in found if item["service"]}
    for directory in directories:
        for unit in sorted(directory.glob("*.service")) if directory.is_dir() else ():
            if unit.name in seen:
                continue
            seen.add(unit.name)
            try:
                if any((directory / (unit.name + ".d")).glob("*.conf")):
                    continue
                text = re.sub(r"\\\r?\n\s*", " ", unit.read_text())
                users = re.findall(r"^\s*User\s*=\s*(\S+)\s*$", text, re.M)
                allowed_users = _current_users()
                if allowed_users is not None and (len(users) != 1 or users[0] not in allowed_users):
                    continue
                starts = re.findall(r"^\s*ExecStart\s*=\s*(.+)$", text, re.M)
                working = re.findall(r"^\s*WorkingDirectory\s*=\s*(.+)$", text, re.M)
                if len(starts) != 1 or "$" in starts[0] or "%" in starts[0]:
                    continue
                item = _moon_command(shlex.split(starts[0]), working[-1] if working else "/", api,
                                     service=unit.name,
                                     scope="user" if directory == Path.home() / ".config/systemd/user" else "system")
                if item:
                    found.append(item)
            except (OSError, UnicodeError, ValueError):
                continue
    return found


def discover_moonraker(selected, api, explicit=None, instances=None):
    if not selected.get("socket"):
        _fail(api, "the selected Klipper has no verified API socket; specify --klippy-socket or use --no-update-manager")
    instances = moonraker_instances(api) if instances is None else instances
    config = api.canonical(explicit) if explicit else None
    candidates = [item for item in instances if item["socket"] == selected["socket"]
                  and (config is None or item["config"] == config)]
    live = [item for item in candidates if item.get("pid")]
    if len(live) > 1:
        _fail(api, "multiple live Moonraker processes match this Klippy socket; no instance was guessed")
    candidates = live or candidates
    unique = {(item["config"], item.get("service")): item for item in candidates}
    if len(unique) != 1:
        _fail(api, "cannot select exactly one Moonraker for this Klippy socket (%d matches). "
              "Use --moonraker-config with an actual matching process/literal service, or --no-update-manager; "
              "no printer configuration was changed" % len(unique))
    return next(iter(unique.values()))


def _fingerprint(moon):
    return {key: str(moon[key]) if isinstance(moon[key], Path) else moon.get(key)
            for key in ("config", "data", "socket", "pid", "start_time", "service", "service_scope", "argv")}


def assert_identity(moon, selected, api):
    current = discover_moonraker(selected, api, moon["config"])
    if _fingerprint(current) != _fingerprint(moon):
        _fail(api, "selected Moonraker process/service/config/socket changed; stop and retry")
    if not current.get("pid"):
        systemctl = shutil.which("systemctl")
        if not systemctl or not current.get("service") or current.get("service_scope") != "system":
            _fail(api, "a stopped Moonraker requires a verifiable loaded system service even with --no-restart")
        service_identity(moon, selected, api, systemctl)
    for path, document in moon["graph"].documents.items():
        if api.snapshot(path).get("data") != base64.b64encode(document.original).decode():
            _fail(api, "Moonraker configuration changed after planning: %s" % path)


def stanza(repo):
    return ("# Managed by the accelerometer homing installer.\n"
            "[update_manager accel_homing]\n"
            "type: git_repo\nchannel: dev\npath: %s\norigin: %s\nprimary_branch: %s\n"
            "is_system_service: False\nmanaged_services: klipper\n" % (repo, ORIGIN, BRANCH)).encode()


def main_suffix(add_base, add_include):
    text = "\n# accel_homing managed Moonraker update registration\n"
    if add_base:
        text += "[update_manager]\n"
    if add_include:
        text += "[include %s]\n" % INCLUDE
    return text.encode() if add_base or add_include else b""


def activation_needed(selected, context, api):
    _records, active, _receipt = api.load_uninstaller().history(selected["config"], api.uninstall_api())
    for record in reversed(active):
        if record["manifest"].get("updater", {}).get("managed_repo") != context["managed_repo"]:
            continue
        path = record["directory"] / "moonraker-activation.json"
        if not path.exists():
            continue
        if path.is_symlink():
            _fail(api, "refusing symlink Moonraker activation receipt")
        value = json.loads(path.read_text())
        return not (value.get("status") == "restarted" and value.get("config") == context["moon_config"]
                    and value.get("instance") == instance_key(selected)
                    and value.get("registration_sha256") == hashlib.sha256(stanza(Path(context["managed_repo"]))).hexdigest())
    return True


def source_commit(source, api):
    marker = source / ".source.json"
    if marker.is_file():
        data = json.loads(marker.read_text())
        if data.get("repo") != "FrankRamm/MISC" or not re.fullmatch(r"[0-9a-f]{40}", data.get("commit", "")):
            _fail(api, "updater source must be the verified FrankRamm/MISC release")
        if not isinstance(data.get("sha256"), dict) or not set(api.MODULES).issubset(data["sha256"]):
            _fail(api, "verified source manifest is incomplete")
        for name, digest in data.get("sha256", {}).items():
            path = source / name
            try:
                path.resolve().relative_to(source.resolve())
            except ValueError:
                _fail(api, "verified payload path escapes its release")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                _fail(api, "verified release payload changed: %s" % path)
        return data["commit"]
    _fail(api, "no verified immutable source manifest; use the hosted bootstrap or --no-update-manager for local development")


def migration_args(selected, args, api):
    result = copy.copy(args)
    if args.replace_existing_links:
        return result
    _records, active, _receipt = api.load_uninstaller().history(selected["config"], api.uninstall_api())
    owned = {}
    for record in active:
        for entry in record["manifest"]["changes"]:
            path = Path(entry["path"])
            if path.parent == selected["repo"] / "klippy/extras" and path.name in api.MODULES:
                owned[path] = entry["after"]
    links = [selected["repo"] / "klippy/extras" / name for name in api.MODULES]
    if owned and all(not (path.exists() or path.is_symlink()) or
                     (path in owned and api.state_matches(path, owned[path])) for path in links):
        # Validate the complete journal, not just caller-supplied symlink text.
        api.load_uninstaller().make_plan(selected, api.uninstall_api())
        result.replace_existing_links = True
    return result


def make_plan(source, selected, args, api):
    moon = discover_moonraker(selected, api, getattr(args, "moonraker_config", None))
    assert_identity(moon, selected, api)
    repo = managed_path(selected)
    if any(token in str(repo) for token in ("\n", "\r", "#", ";")):
        _fail(api, "managed checkout path cannot be represented safely in Moonraker configuration")
    include = moon["config"].parent / INCLUDE
    graph, changes = moon["graph"], {}
    existing = graph.one(SECTION, False)
    if existing:
        if existing.document.path != include or include.read_bytes() != stanza(repo):
            _fail(api, "conflicting existing [%s]; preserve/reconcile it instead of overwriting" % SECTION)
    elif include.exists() or include.is_symlink():
        _fail(api, "%s exists without the unique managed updater stanza" % include)
    else:
        changes[include] = dict(kind="file", data=stanza(repo))
    matching = [pattern for pattern in graph.include_patterns if include.match(pattern)]
    if not existing and len(matching) > 1:
        _fail(api, "the generated updater would match multiple includes; reconcile overlapping wildcard patterns")
    add_include = not existing and not matching
    add_base = graph.one("update_manager", False) is None
    suffix = main_suffix(add_base, add_include)
    if suffix:
        original = moon["config"].read_bytes()
        changes[moon["config"]] = dict(kind="file", data=original + suffix)
    commit = source_commit(source, api)
    context = dict(moon_config=str(moon["config"]), moon_data=str(moon["data"]),
                   moon_socket=str(moon["socket"]), managed_repo=str(repo), origin=ORIGIN,
                   branch=BRANCH, include_file=str(include), append=base64.b64encode(suffix).decode())
    receipt = selected["config"].parent / RECEIPT
    encoded = (json.dumps(dict(version=1, instance=instance_key(selected), commit=commit,
                               **context), sort_keys=True, indent=2) + "\n").encode()
    if receipt.is_symlink():
        _fail(api, "refusing symlink updater receipt")
    if receipt.exists():
        old = json.loads(receipt.read_text())
        if old.get("instance") != instance_key(selected) or old.get("managed_repo") != str(repo):
            _fail(api, "existing updater receipt belongs to another source/instance")
        if not suffix:
            context["append"] = old.get("append", "")
            encoded = (json.dumps(dict(version=1, instance=instance_key(selected), commit=commit,
                                       **context), sort_keys=True, indent=2) + "\n").encode()
    if not receipt.exists() or receipt.read_bytes() != encoded:
        changes[receipt] = dict(kind="file", data=encoded)
    for path, change in changes.items():
        change["expected"] = api.snapshot(path)
    pending = activation_needed(selected, context, api)
    return dict(moon=moon, repo=repo, source=repo / "Beacon_Accel_Homing", commit=commit,
                changes=changes, context=context, source_input=source, published=False,
                restart_needed=any(path != receipt for path in changes) or pending, receipt=receipt)


def git(repo, arguments, api, timeout=120):
    command = ["git", "-C", str(repo)] + list(arguments)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(api, "git command failed: %s" % exc)
    if result.returncode:
        _fail(api, "git %s failed: %s" % (arguments[0], result.stderr[-1500:].strip()))
    return result.stdout.strip()


def repo_identity(repo, api):
    locks = [repo / ".git" / name for name in ("index.lock", "HEAD.lock", "config.lock", "packed-refs.lock")]
    locks += list((repo / ".git/refs").rglob("*.lock")) if (repo / ".git/refs").is_dir() else []
    if repo.is_symlink() or not (repo / ".git").is_dir() or any(path.exists() for path in locks):
        _fail(api, "managed source is not an unlocked dedicated checkout: %s" % repo)
    origin = git(repo, ["remote", "get-url", "origin"], api)
    branch = git(repo, ["symbolic-ref", "--short", "HEAD"], api)
    upstream = git(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"], api)
    if origin != ORIGIN or branch != BRANCH or upstream != "origin/" + BRANCH:
        _fail(api, "managed checkout origin/branch/upstream differs; nothing was reset or stashed")
    if git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], api):
        _fail(api, "managed checkout is dirty; preserve/reconcile those changes before updating")
    return dict(commit=git(repo, ["rev-parse", "HEAD"], api), origin=origin, branch=branch, upstream=upstream)


def prepare_source(plan, api, guard):
    repo, commit = plan["repo"], plan["commit"]
    guard()
    if repo.exists():
        plan["old_identity"] = repo_identity(repo, api)
        if plan["old_identity"]["commit"] == commit:
            plan["candidate"] = repo
            return plan["source"]
    repo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".candidate-", dir=str(repo.parent)))
    try:
        git(stage, ["clone", "--filter=blob:none", "--single-branch", "--branch", BRANCH,
                    "--sparse", ORIGIN, "."], api, timeout=300)
        git(stage, ["sparse-checkout", "set", "Beacon_Accel_Homing"], api)
        git(stage, ["merge-base", "--is-ancestor", commit, "origin/" + BRANCH], api)
        if plan.get("old_identity"):
            git(stage, ["merge-base", "--is-ancestor", plan["old_identity"]["commit"], commit], api)
        git(stage, ["checkout", "-B", BRANCH, commit], api)
        git(stage, ["branch", "--set-upstream-to", "origin/" + BRANCH, BRANCH], api)
        repo_identity(stage, api)
        plugin = stage / "Beacon_Accel_Homing"
        marker = json.loads((plan["source_input"] / ".source.json").read_text())
        for name, digest in marker["sha256"].items():
            if hashlib.sha256((plugin / name).read_bytes()).hexdigest() != digest:
                _fail(api, "candidate source differs from the verified immutable payload: %s" % name)
        # Private wheels are copied to the candidate only, never moved/deleted.
        previous = repo / "Beacon_Accel_Homing/.plot-deps" if repo.exists() else plan["source_input"] / ".plot-deps"
        if previous.is_dir() and not previous.is_symlink():
            shutil.copytree(previous, plugin / ".plot-deps", symlinks=True)
        plan["candidate"] = stage
        return plugin
    except Exception:
        shutil.rmtree(stage)
        raise


def publish_source(plan, api, guard):
    candidate, repo = plan["candidate"], plan["repo"]
    if candidate == repo:
        return
    guard()
    if repo.exists():
        if repo_identity(repo, api) != plan["old_identity"]:
            _fail(api, "managed source changed while preparing candidate; no publication occurred")
        backup = repo.parent / ("source-backup-" + uuid.uuid4().hex)
        os.rename(repo, backup)
        plan["source_backup"] = backup
    try:
        os.rename(candidate, repo)
        plan["published"] = True
    except Exception:
        if plan.get("source_backup"):
            os.rename(plan["source_backup"], repo)
        raise


def restore_publication(plan, api):
    if not plan.get("published"):
        return
    repo = plan["repo"]
    if repo_identity(repo, api)["commit"] != plan["commit"]:
        _fail(api, "source changed after publication; retained backups require manual recovery")
    retained = repo.parent / ("failed-source-" + uuid.uuid4().hex)
    os.rename(repo, retained)
    try:
        if plan.get("source_backup"):
            os.rename(plan["source_backup"], repo)
    except Exception:
        os.rename(retained, repo)
        raise
    plan["published"] = False


def source_metadata(plan):
    return dict(repo=str(plan["repo"]), commit=plan["commit"],
                old_commit=(plan.get("old_identity") or {}).get("commit"),
                backup=str(plan["source_backup"]) if plan.get("source_backup") else None,
                published=bool(plan.get("published")))


def validate_source_rollback(manifest, selected, api):
    value = manifest.get("source_update")
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"repo", "commit", "old_commit", "backup", "published"}
            or value["repo"] != str(managed_path(selected)) or type(value["published"]) is not bool
            or not re.fullmatch(r"[0-9a-f]{40}", value.get("commit", ""))):
        _fail(api, "invalid source-update rollback ownership")
    if not value["published"]:
        return None
    repo, backup = Path(value["repo"]), Path(value["backup"]) if value["backup"] else None
    if backup is not None:
        if (backup.parent != repo.parent or not re.fullmatch(r"source-backup-[0-9a-f]{32}", backup.name)
                or not re.fullmatch(r"[0-9a-f]{40}", value.get("old_commit") or "")
                or repo_identity(backup, api)["commit"] != value["old_commit"]):
            _fail(api, "retained source rollback backup is missing or changed")
    elif value["old_commit"] is not None:
        _fail(api, "source rollback is missing its original checkout")
    if repo_identity(repo, api)["commit"] != value["commit"]:
        _fail(api, "managed source changed after installation; preserve later UI updates instead of overwriting")
    return dict(repo=repo, commit=value["commit"], source_backup=backup, published=True)


def prepare_restart(moon, selected, api):
    systemctl, sudo = shutil.which("systemctl"), shutil.which("sudo")
    if not systemctl or not sudo or not moon.get("service") or moon.get("service_scope") != "system":
        _fail(api, "automatic activation needs the unique system Moonraker service; use --no-restart for manual activation")
    identity = service_identity(moon, selected, api, systemctl)
    plan = dict(systemctl=systemctl, sudo=sudo, identity=identity,
                service_command=[systemctl, "--system", "--no-ask-password", "restart", "--", moon["service"]])
    api.restart_permission(plan, acquire=True)
    return plan


def service_identity(moon, selected, api, systemctl):
    properties = api.read_service_unit(moon, systemctl)
    users = _current_users()
    if users is not None and properties.get("User") not in users:
        _fail(api, "Moonraker service is not owned by the installer/Klipper user")
    current = discover_moonraker(selected, api, moon["config"])
    active = properties.get("ActiveState")
    pid = int(properties.get("MainPID", "-1"))
    if current.get("pid"):
        if (active != "active" or properties.get("SubState") != "running"
                or current["pid"] != pid or current.get("service") != moon["service"]
                or current.get("service_scope") != "system"):
            _fail(api, "Moonraker service MainPID/process identity is ambiguous")
    elif active not in ("inactive", "failed") or pid != 0:
        _fail(api, "Moonraker is starting or cannot be established as stopped")
    commands = re.findall(r"(?:^|;\s*)argv\[\]=(.*?)\s*;\s*ignore_errors=", properties.get("ExecStart", ""))
    if len(commands) != 1 or any(token in commands[0] for token in ("%", "\\x")):
        _fail(api, "cannot establish one effective Moonraker ExecStart")
    argv = shlex.split(commands[0])
    variables = [arg for arg in argv if "$" in arg]
    if variables:
        if (not current.get("pid") or not argv or argv[0] != current["executable"]
                or any(not re.fullmatch(r"\$[A-Za-z_]\w*|\$\{[A-Za-z_]\w*\}", value) for value in variables)):
            _fail(api, "environment-based Moonraker unit needs a matching live main process")
    else:
        effective = _moon_command(argv, properties.get("WorkingDirectory") or "/", api)
        if effective is None or any(effective[key] != current[key] for key in ("config", "data", "socket", "executable")):
            _fail(api, "effective Moonraker service command differs from the selected instance")
    # Loaded definitions and live arguments are both retained; no env expansion.
    return dict(instance=_fingerprint(current), unit={key: properties.get(key, "") for key in
                ("Id", "ExecStart", "WorkingDirectory", "FragmentPath", "DropInPaths", "User", "Group",
                 "Environment", "EnvironmentFiles", "MainPID", "InvocationID", "ActiveState")})


def assert_restart_safe(moon, selected, restart, api, check_files=True):
    api.restart_permission(restart)
    if service_identity(moon, selected, api, restart["systemctl"]) != restart["identity"]:
        _fail(api, "Moonraker service/process identity changed before writes/restart")
    if check_files:
        assert_identity(moon, selected, api)
    print(api.assert_idle(selected, require_process_identity=bool(selected.get("pid"))))


def record_activation(plan, selected, api, backup, status, **extra):
    if backup is None:
        _records, active, _receipt = api.load_uninstaller().history(selected["config"], api.uninstall_api())
        if not active:
            _fail(api, "no owned installation transaction for the activation receipt")
        backup = active[-1]["directory"]
    path = backup / "moonraker-activation.json"
    data = dict(status=status, service=plan["moon"]["service"], config=str(plan["moon"]["config"]),
                instance=instance_key(selected), registration_sha256=hashlib.sha256(stanza(plan["repo"])).hexdigest(), **extra)
    api.write_state(path, dict(kind="file", mode=0o600,
                    data=base64.b64encode((json.dumps(data, indent=2) + "\n").encode()).decode()))
    return path


def print_activation_advice(context, selected, api):
    moon = discover_moonraker(selected, api, context["moon_config"])
    service = moon.get("service")
    if service and api.SERVICE_NAME.fullmatch(service):
        command = ("systemctl --user restart -- " if moon.get("service_scope") == "user"
                   else "sudo systemctl restart -- ") + shlex.quote(service)
        print("Moonraker registration changed. While this printer is idle, restart its detected Moonraker "
              "service from the UI or run: %s" % command)
    else:
        print("Moonraker registration changed in %s. Restart that actual Moonraker host process once; "
              "browser refresh alone does not reload configuration." % context["moon_config"])


def activate(plan, selected, restart, api, backup):
    path = record_activation(plan, selected, api, backup, "pending")
    try:
        assert_restart_safe(plan["moon"], selected, restart, api, check_files=False)
        for destination, expected in plan["changes"].items():
            state = dict(expected)
            state.pop("expected", None)
            if state["kind"] == "file":
                state["data"] = base64.b64encode(state["data"]).decode()
            if not api.state_matches(destination, state):
                _fail(api, "installed updater files changed before activation")
        result = api.service_command([restart["sudo"], "-n", "--"] + restart["service_command"], timeout=60)
        if result.returncode:
            _fail(api, "Moonraker restart command failed: %s" % result.stderr.strip())
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                current = service_identity(plan["moon"], selected, api, restart["systemctl"])
                old = restart["identity"]["instance"]
                if (current["instance"].get("pid") and current["instance"]["config"] == old["config"]
                        and current["instance"]["socket"] == old["socket"]
                        and (current["instance"]["pid"], current["instance"]["start_time"]) !=
                            (old.get("pid"), old.get("start_time"))):
                    record_activation(plan, selected, api, backup, "restarted", identity=current)
                    print("Moonraker updater registration activated: %s" % plan["moon"]["service"])
                    return
            except api.InstallError:
                pass
            time.sleep(0.2)
        _fail(api, "Moonraker did not return with a new verified process")
    except Exception as exc:
        record_activation(plan, selected, api, backup, "failed", error=str(exc))
        _fail(api, "files are installed but Moonraker updater activation remains pending; Klipper activation "
              "has not run. Backup/activation receipt: %s. Cause: %s" % (path, exc))


def validate_record(manifest, selected, api, verify_live=True):
    context = manifest.get("updater")
    if context is None:
        return {}
    keys = {"moon_config", "moon_data", "moon_socket", "managed_repo", "origin", "branch", "include_file", "append"}
    if not isinstance(context, dict) or set(context) != keys:
        _fail(api, "invalid updater journal context")
    repo = managed_path(selected)
    main, include = Path(context["moon_config"]), Path(context["include_file"])
    if (not main.is_absolute() or include != main.parent / INCLUDE
            or context["managed_repo"] != str(repo) or context["origin"] != ORIGIN or context["branch"] != BRANCH
            or context["moon_socket"] != str(selected["socket"])):
        _fail(api, "updater journal is outside selected instance ownership")
    if verify_live:
        moon = discover_moonraker(selected, api, main)
        if str(moon["data"]) != context["moon_data"]:
            _fail(api, "updater journal Moonraker data path differs from actual instance")
    try:
        suffix = base64.b64decode(context["append"], validate=True)
    except (ValueError, TypeError):
        _fail(api, "invalid updater include journal")
    if suffix not in {main_suffix(base, inc) for base in (False, True) for inc in (False, True)}:
        _fail(api, "unowned Moonraker main-config edit")
    allowed = {main: "moon_main", include: "moon_generated", selected["config"].parent / RECEIPT: "updater_receipt"}
    checked = {}
    for entry in manifest["changes"]:
        path = Path(entry["path"])
        if path not in allowed:
            continue
        before, after = entry["before"], entry["after"]
        if manifest.get("operation") == "uninstall":
            before, after = after, before
        if path == include:
            if before["kind"] != "absent" or after["kind"] != "file" or base64.b64decode(after["data"]) != stanza(repo):
                _fail(api, "unowned generated Moonraker updater edit")
        elif path == main:
            if before["kind"] != "file" or after["kind"] != "file":
                _fail(api, "unsafe Moonraker main configuration journal")
            old, new = base64.b64decode(before["data"]), base64.b64decode(after["data"])
            if manifest.get("operation") == "uninstall":
                if new.count(suffix) != 1 or new.replace(suffix, b"", 1) != old:
                    _fail(api, "uninstall contains unowned Moonraker text changes")
            elif new != old + suffix:
                _fail(api, "updater journal changes unrelated Moonraker settings")
        else:
            for state in (before, after):
                if state["kind"] == "absent":
                    continue
                if state["kind"] != "file":
                    _fail(api, "unsafe updater receipt journal")
                value = json.loads(base64.b64decode(state["data"]))
                expected = {"version", "instance", "commit"} | keys
                if (set(value) != expected or value["version"] != 1 or value["instance"] != instance_key(selected)
                        or value["managed_repo"] != str(repo) or value["origin"] != ORIGIN or value["branch"] != BRANCH
                        or not re.fullmatch(r"[0-9a-f]{40}", value.get("commit", ""))):
                    _fail(api, "unowned updater receipt contents")
        checked[path] = allowed[path]
    return checked
