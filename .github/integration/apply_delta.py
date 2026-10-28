"""Transport locally tested edits with old/new Git blob verification."""
import base64
import hashlib
import json
import lzma
from pathlib import Path
import subprocess
import sys

root = Path.cwd()
encoded = b''.join((root / ('.github/integration/delta.%d' % i)).read_bytes() for i in range(4))
raw = lzma.decompress(base64.b64decode(encoded, validate=True))
assert hashlib.sha256(raw).hexdigest() == '084529c4aedfac396a31dfb6e33e71b4312ab4cc82d1c26ff396a8e6b11c9857'
changes = json.loads(raw)
assert len(changes) == 14

def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()

pending = {}
for name, change in changes.items():
    path = root / name
    assert name.startswith('Beacon_Accel_Homing/') and '..' not in Path(name).parts
    if '--verify' in sys.argv:
        assert path.is_file() and blob(path.read_bytes()) == change['new'], name
        continue
    if change['old'] is None:
        assert not path.exists(), name
        old = b''
    else:
        old = path.read_bytes()
        assert blob(old) == change['old'], name
    lines = old.decode('utf-8').splitlines(keepends=True)
    limit = len(lines)
    for first, last, text in reversed(change['edits']):
        assert 0 <= first <= last <= limit, name
        lines[first:last] = text.splitlines(keepends=True)
        limit = first
    new = ''.join(lines).encode('utf-8')
    assert blob(new) == change['new'], name
    pending[path] = new
for path, data in pending.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
subprocess.run(['git', 'diff', '--check'], check=True)
if '--stage' in sys.argv:
    subprocess.run(['git', 'add', '--', *changes], check=True)
print(json.dumps({name: change['new'] for name, change in changes.items()}, indent=2))
