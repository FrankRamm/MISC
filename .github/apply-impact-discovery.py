"""Apply locally tested, hash-verified line deltas to the exact deployed source."""
import base64
import hashlib
import json
from pathlib import Path
import zlib


def blob(data):
    return hashlib.sha1(('blob %d\0' % len(data)).encode() + data).hexdigest()

raw = zlib.decompress(base64.b64decode(Path('.github/impact-discovery-delta.b64').read_bytes()))
assert hashlib.sha256(raw).hexdigest() == '26e652ab83f9426037078b11a2c462f955d9f49315170af0a22c9ceba9b32e47'
entries = json.loads(raw)
assert len(entries) == 11
manifest = {}
for entry in entries:
    path = Path(entry['path'])
    assert path.parts[0] == 'Beacon_Accel_Homing' and '..' not in path.parts
    assert str(path) not in manifest
    before = path.read_bytes()
    assert blob(before) == entry['before'], ('source mismatch', str(path))
    lines = before.decode('utf-8').splitlines(keepends=True)
    boundary = len(lines)
    for start, end, replacement in reversed(entry['changes']):
        assert 0 <= start <= end <= boundary
        boundary = start
        lines[start:end] = replacement.splitlines(keepends=True)
    after = ''.join(lines).encode('utf-8')
    assert blob(after) == entry['after'], ('result mismatch', str(path))
    path.write_bytes(after)
    manifest[str(path)] = entry['after']
Path('/tmp/impact-discovery-manifest.json').write_text(json.dumps(manifest, sort_keys=True))
print('Verified all 11 source changes against their locally tested blob hashes.')
