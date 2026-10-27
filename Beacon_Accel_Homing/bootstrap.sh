#!/usr/bin/env bash
# Buffer a successful complete download before bash; do not pipe curl to bash.
set -euo pipefail
accel_homing_require_complete() {
    local installer_source
    if [[ -n "${BASH_EXECUTION_STRING-}" ]]; then
        installer_source=$BASH_EXECUTION_STRING
    elif [[ -n "${BASH_SOURCE[0]-}" && -f "${BASH_SOURCE[0]}" ]]; then
        installer_source=$(<"${BASH_SOURCE[0]}") || return 1
    else
        printf '%s\n' 'Bootstrap requires a complete buffered command or file; use the documented installer command.' >&2
        return 1
    fi
    if [[ "$installer_source" != *$'\n# ACCEL_HOMING_BOOTSTRAP_COMPLETE_V1' ]]; then
        printf '%s\n' 'Incomplete bootstrap; installation was not started.' >&2
        return 1
    fi
}
accel_homing_bootstrap() {
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'Python 3.8 or newer is required.' >&2
        return 1
    fi
    python3 - "$@" <<'PY'
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

parser = argparse.ArgumentParser(description="Download a complete pinned accelerometer homing release.", add_help=False)
parser.add_argument("--repo", default="FrankRamm/MISC")
parser.add_argument("--ref", default="codex/universal-accel-homing")
args, remaining = parser.parse_known_args()
try:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise ValueError("--repo must be OWNER/REPOSITORY")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.ref):
        url = "https://api.github.com/repos/%s/commits/%s" % (args.repo, urllib.parse.quote(args.ref, safe=""))
        request = urllib.request.Request(url, headers={"User-Agent": "accel-homing-installer"})
        with urllib.request.urlopen(request, timeout=30) as response:
            args.ref = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.ref):
        raise ValueError("GitHub did not return a full commit SHA")
    meta_url = "https://api.github.com/repos/%s/contents/Beacon_Accel_Homing/bootstrap.py?ref=%s" % (args.repo, args.ref)
    request = urllib.request.Request(meta_url, headers={"User-Agent": "accel-homing-installer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        metadata = json.load(response)
    url = "https://raw.githubusercontent.com/%s/%s/Beacon_Accel_Homing/bootstrap.py" % (args.repo, args.ref)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        declared = response.headers.get("Content-Length")
    if declared is not None and int(declared) != len(data):
        raise ValueError("Incomplete bootstrap.py HTTP response")
    blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if metadata.get("type") != "file" or metadata.get("size") != len(data) or metadata.get("sha") != blob:
        raise ValueError("bootstrap.py does not match the pinned Git blob size/hash")
    compile(data, "bootstrap.py", "exec")
    with tempfile.TemporaryDirectory(prefix="accel-homing-bootstrap-") as directory:
        script = pathlib.Path(directory) / "bootstrap.py"
        script.write_bytes(data)
        result = subprocess.run([sys.executable, str(script), "--repo", args.repo, "--ref", args.ref] + remaining)
        sys.exit(result.returncode)
except (OSError, ValueError, KeyError, SyntaxError) as error:
    print("Bootstrap aborted before installation: %s" % error, file=sys.stderr)
    sys.exit(1)
PY
}
(
    accel_homing_require_complete || exit 1
    accel_homing_bootstrap "$@"
)
# ACCEL_HOMING_BOOTSTRAP_COMPLETE_V1
