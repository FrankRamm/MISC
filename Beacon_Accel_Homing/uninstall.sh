#!/usr/bin/env bash
# Offline from a complete verified source package; standalone downloads always
# fetch/verify the immutable bootstrap and force its uninstall-only entry point.
set -euo pipefail
accel_homing_uninstall() {
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'Python 3.8 or newer is required.' >&2
        return 1
    fi
    local source_dir
    source_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    python3 - "$source_dir" "$@" <<'PY'
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

source = pathlib.Path(sys.argv[1]).resolve()
forwarded = sys.argv[2:]
parser = argparse.ArgumentParser(description="Restore the pre-install accelerometer homing state.", add_help=False)
parser.add_argument("--repo", default="FrankRamm/MISC")
parser.add_argument("--ref", default="codex/universal-accel-homing")
args, remaining = parser.parse_known_args(forwarded)
try:
    if any(option == "--rollback" or option.startswith("--rollback=") for option in remaining):
        raise ValueError("uninstall.sh only supports full uninstall; use install.sh --rollback for one transaction")
    # A random sibling /tmp/installer.py is never an offline entry point. A
    # verified cache owns every downloaded file; a checkout tracks the complete
    # source package. A plain downloaded script satisfies neither condition.
    required = ("installer.py", "uninstaller.py", "bootstrap.py", "install.sh", "uninstall.sh",
                "accel_homing.py", "beacon_accel_homing.py", "accel_homing_backends.py",
                "accel_homing_workflows.py", "accel_homing_model.py", "accel_homing_charts.py",
                "LICENSE", "NOTICE")
    complete = all((source / name).is_file() and not (source / name).is_symlink() for name in required)
    local_source = False
    if complete and (source / ".source.json").is_file():
        marker = json.loads((source / ".source.json").read_text())
        hashes = marker.get("sha256", {})
        if not set(required).issubset(hashes):
            raise ValueError("Incomplete verified source-cache ownership; choose a clean source directory")
        for name, digest in hashes.items():
            relative = pathlib.PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe source-cache path")
            if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Cached source changed: " + str(source / name))
        local_source = True
    elif complete:
        try:
            tracked = subprocess.run(["git", "-C", str(source), "ls-files", "--error-unmatch", "--"] + list(required),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            local_source = tracked.returncode == 0
        except OSError:
            pass
    if local_source:
        result = subprocess.run([sys.executable, str(source / "installer.py"), "--uninstall"] + remaining)
        sys.exit(result.returncode)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise ValueError("--repo must be OWNER/REPOSITORY")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.ref):
        url = "https://api.github.com/repos/%s/commits/%s" % (args.repo, urllib.parse.quote(args.ref, safe=""))
        request = urllib.request.Request(url, headers={"User-Agent": "accel-homing-uninstaller"})
        with urllib.request.urlopen(request, timeout=30) as response:
            args.ref = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.ref):
        raise ValueError("GitHub did not return a full commit SHA")
    meta_url = "https://api.github.com/repos/%s/contents/Beacon_Accel_Homing/bootstrap.py?ref=%s" % (args.repo, args.ref)
    request = urllib.request.Request(meta_url, headers={"User-Agent": "accel-homing-uninstaller"})
    with urllib.request.urlopen(request, timeout=30) as response:
        metadata = json.load(response)
    url = "https://raw.githubusercontent.com/%s/%s/Beacon_Accel_Homing/bootstrap.py" % (args.repo, args.ref)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        declared = response.headers.get("Content-Length")
    if not data or len(data) > 4 * 1024 * 1024:
        raise ValueError("Empty/oversized bootstrap.py response")
    if declared is not None and int(declared) != len(data):
        raise ValueError("Incomplete bootstrap.py HTTP response")
    blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if metadata.get("type") != "file" or metadata.get("size") != len(data) or metadata.get("sha") != blob:
        raise ValueError("bootstrap.py does not match the pinned Git blob size/hash")
    compile(data, "bootstrap.py", "exec")
    with tempfile.TemporaryDirectory(prefix="accel-homing-uninstall-bootstrap-") as directory:
        script = pathlib.Path(directory) / "bootstrap.py"
        script.write_bytes(data)
        result = subprocess.run([sys.executable, str(script), "--repo", args.repo, "--ref", args.ref,
                                 "--uninstall"] + remaining)
        sys.exit(result.returncode)
except (OSError, ValueError, KeyError, SyntaxError) as error:
    print("Uninstall bootstrap aborted before any installation/config writes: %s" % error, file=sys.stderr)
    sys.exit(1)
PY
}
(
    accel_homing_uninstall "$@"
)
