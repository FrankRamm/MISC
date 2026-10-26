#!/usr/bin/env bash
# Local offline entry point. Hosted downloads use bootstrap.sh.
set -euo pipefail
source_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.8 or newer is required. Install python3 and retry.' >&2
    exit 1
fi
if [[ ! -f "$source_dir/installer.py" ]]; then
    printf '%s\n' 'Extract the complete Beacon_Accel_Homing folder, or use bootstrap.sh.' >&2
    exit 1
fi
exec python3 "$source_dir/installer.py" "$@"
