#!/usr/bin/env python3
"""Fetch every required file at one immutable revision before running installer.py."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

FILES = ("install.sh", "uninstall.sh", "bootstrap.py", "bootstrap.sh", "installer.py", "uninstaller.py", "accel_homing.py", "beacon_accel_homing.py",
         "accel_homing_backends.py", "accel_homing_workflows.py", "accel_homing_model.py",
         "accel_homing_charts.py", "LICENSE", "NOTICE", "README.md", "TESTING.md", "docs/INSTALLATION.md", "docs/COMPATIBILITY.md",
         "examples/accel_homing.cfg", "examples/beacon_accel_homing.cfg",
         "validation/VALIDATION.json", "validation/tests.txt")


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "accel-homing-installer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        declared = response.headers.get("Content-Length")
    if not data or len(data) > 16 * 1024 * 1024:
        raise ValueError("Empty/oversized download: " + url)
    if declared is not None and int(declared) != len(data):
        raise ValueError("Incomplete HTTP response: " + url)
    return data


def file_metadata(repo, sha, fetch):
    entries = {}
    # GitHub contents metadata supplies the expected Git blob SHA and byte size.
    # A digest of received bytes alone would merely trust a truncated download.
    for parent in sorted({str(Path(name).parent).replace("\\", "/") for name in FILES}):
        suffix = "" if parent == "." else "/" + parent
        url = "https://api.github.com/repos/%s/contents/Beacon_Accel_Homing%s?ref=%s" % (repo, suffix, sha)
        listing = json.loads(fetch(url))
        if not isinstance(listing, list):
            raise ValueError("Invalid GitHub contents metadata: " + url)
        for item in listing:
            name = (parent + "/" if parent != "." else "") + item.get("name", "")
            if name in FILES:
                if item.get("type") != "file" or not re.fullmatch(r"[0-9a-f]{40}", item.get("sha", "")):
                    raise ValueError("Expected a regular pinned Git blob: " + name)
                entries[name] = item
    if set(entries) != set(FILES):
        raise ValueError("Incomplete published package; metadata missing: " + ", ".join(sorted(set(FILES) - set(entries))))
    return entries


def resolve_ref(repo, ref, fetch=download):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo must be OWNER/REPOSITORY")
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    url = "https://api.github.com/repos/%s/commits/%s" % (repo, urllib.parse.quote(ref, safe=""))
    resolved = json.loads(fetch(url)).get("sha", "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", resolved):
        raise ValueError("GitHub did not return a full commit SHA")
    return resolved.lower()


def prepare_release(repo, ref, destination, fetch=download):
    sha = resolve_ref(repo, ref, fetch)
    destination = Path(destination).expanduser().resolve()
    release = destination / (repo.replace("/", "--") + "-" + sha)
    if release.exists():
        marker = json.loads((release / ".source.json").read_text())
        if marker.get("repo") != repo or marker.get("commit") != sha or set(marker.get("sha256", {})) != set(FILES):
            raise ValueError("Existing release has unknown ownership/content: " + str(release))
        for name, digest in marker["sha256"].items():
            if hashlib.sha256((release / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Cached source changed; preserve/review it and choose another --install-dir: " + str(release / name))
        return release
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".download-", dir=str(destination)))
    try:
        expected = file_metadata(repo, sha, fetch)
        hashes = {}
        for name in FILES:
            url = "https://raw.githubusercontent.com/%s/%s/Beacon_Accel_Homing/%s" % (repo, sha, name)
            data = fetch(url)  # Any HTTP/network failure aborts before invoking the installer.
            if not data:
                raise ValueError("Empty download: " + name)
            blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
            if expected[name].get("size") != len(data) or expected[name]["sha"] != blob:
                raise ValueError("Downloaded size/hash differs from pinned Git blob: " + name)
            if name.endswith(".py"):
                compile(data, name, "exec")
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        (stage / ".source.json").write_text(json.dumps({"repo": repo, "commit": sha, "sha256": hashes}, indent=2) + "\n")
        os.rename(stage, release)
    except Exception:
        shutil.rmtree(stage)
        raise
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="FrankRamm/MISC")
    parser.add_argument("--ref", default="codex/universal-accel-homing", help="branch/tag or preferably a full immutable commit SHA")
    parser.add_argument("--install-dir", default=str(Path.home() / ".local/share/accel-homing"), help="persistent versioned source directory")
    args, installer_args = parser.parse_known_args(argv)
    if ("--uninstall" in installer_args or "-u" in installer_args) and any(
            option == "--rollback" or option.startswith("--rollback=") for option in installer_args):
        parser.error("choose --uninstall or --rollback")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Run as the Klipper service user, not root")
    release = prepare_release(args.repo, args.ref, args.install_dir)
    print("Complete source downloaded/verified: %s" % release, flush=True)
    return subprocess.run([sys.executable, str(release / "installer.py")] + installer_args).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, SyntaxError) as error:
        print("Bootstrap aborted before installation: %s" % error, file=sys.stderr)
        sys.exit(1)
