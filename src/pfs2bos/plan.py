"""Validate plans and durably record per-file migration stages."""
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile

from .config import excluded
from .errors import OffloadError
from .filesystem import fsync_dir, relative


def save(path, value):
    """Persist intent before destructive transitions, including directory entry."""
    fd, name = tempfile.mkstemp(prefix='.pfs2bos-json-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=True, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_plan(path):
    plan = json.loads(path.read_text())
    if plan.get('version') != 1:
        raise OffloadError('Unsupported plan version')
    if len(plan['id']) != 32 or any(c not in '0123456789abcdef' for c in plan['id']):
        raise OffloadError('Invalid plan ID')
    root, mount = Path(plan['root']), Path(plan['mount'])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise OffloadError('Invalid or redirected root')
    if not mount.is_absolute() or mount == root or root.is_relative_to(mount) or mount.is_relative_to(root):
        raise OffloadError('Mount must be absolute and separate from source tree')
    resolved_mount = mount.resolve()
    if resolved_mount == root or root.is_relative_to(resolved_mount) or resolved_mount.is_relative_to(root):
        raise OffloadError('Mount resolves into or contains the source tree')
    if not plan['remote'] or plan['remote'].startswith('-'):
        raise OffloadError('Invalid remote')
    seen = set()
    for e in plan['entries']:
        rel = str(relative(e['path']))
        if rel in seen:
            raise OffloadError('Duplicate plan entry')
        seen.add(rel)
        if e['target'] != str(mount / rel) or e['remote'] != plan['remote'] + '/' + rel:
            raise OffloadError('Entry destination does not match plan roots')
        if any(excluded(x) for x in PurePosixPath(rel).parts):
            raise OffloadError(f'Excluded path in plan: {rel}')
        # rclone raw lists cannot represent these safely. scan retains them for review.
        if '\n' in rel or '\r' in rel:
            raise OffloadError(f'Newline filenames are unsupported: {rel!r}')
    return plan


def update(planpath, plan, entry, state, **kwargs):
    entry.update(state=state, error=None, **kwargs)
    save(planpath, plan)


def fail(planpath, plan, entry, error):
    entry['error'] = str(error)
    save(planpath, plan)
    print(f"{entry['path']}: {error}", file=sys.stderr)
