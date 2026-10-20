"""Copy, verify, atomically link, and restore planned files."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

from .errors import OffloadError
from .filesystem import digest, expected_link, fsync_dir, identity, safe_path, unchanged
from .plan import fail, update


def run_rclone(args, *command):
    result = subprocess.run([args.rclone, *map(str, command), '--retries', '1', '--low-level-retries', '1'],
                            stdout=subprocess.PIPE, stderr=None)
    if result.returncode:
        raise OffloadError(f'rclone {command[0]} failed (exit {result.returncode}); check stderr for conflict, missing object or I/O error')


@contextmanager
def file_list(paths):
    with tempfile.TemporaryDirectory(prefix='pfs2bos-') as d:
        p = Path(d) / 'files.txt'
        p.write_text(''.join(x + '\n' for x in paths))
        yield p


def check_remote(args, plan, entry):
    with file_list([entry['path']]) as listing:
        run_rclone(args, 'check', plan['root'], plan['remote'], '--download', '--one-way',
                   '--files-from-raw', listing)


def switch_link(path, entry):
    # Writers must be stopped: POSIX has no compare-and-swap rename for a file.
    temp = path.parent / ('.pfs2bos-link-' + uuid.uuid4().hex)
    try:
        os.symlink(entry['target'], temp)
        unchanged(path, entry)
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if temp.is_symlink():
            temp.unlink()


def apply(args, planpath, plan):
    root = Path(plan['root'])
    ready = []
    failures = 0
    for e in plan['entries']:
        try:
            p = safe_path(root, e['path'])
            if expected_link(p, e):
                if e['state'] not in ('linking', 'linked', 'restoring') or not e.get('sha256'):
                    raise OffloadError('Unexpected symlink; refusing to adopt it')
                if digest(Path(e['target'])) != e['sha256']:
                    raise OffloadError('Existing link target is unreadable or changed')
                if e['state'] == 'restoring':
                    raise OffloadError('Restore interrupted; run restore again')
                update(planpath, plan, e, 'linked')
                continue
            if e['state'] in ('linked', 'restoring', 'restored'):
                raise OffloadError('Entry was linked/restored; use a new scan to offload again')
            unchanged(p, e)
            h = digest(p, nofollow=True)
            unchanged(p, e)
            if e.get('sha256') and h != e['sha256']:
                raise OffloadError('Source digest changed')
            update(planpath, plan, e, 'copying', sha256=h)
            ready.append(e)
        except (OSError, OffloadError) as error:
            fail(planpath, plan, e, error)
            failures += 1
    if ready:
        print(f'Copying/reusing {len(ready)} files ({args.transfers} transfers)', file=sys.stderr, flush=True)
        try:
            with file_list([e['path'] for e in ready]) as listing:
                run_rclone(args, 'copy', root, plan['remote'], '--files-from-raw', listing,
                           '--ignore-existing', '--no-traverse', '--transfers', args.transfers,
                           '--stats', '10s', '--stats-one-line')
        except (OSError, OffloadError) as error:
            for e in ready:
                fail(planpath, plan, e, error)
            return 1
    for e in ready:
        try:
            print(f"Verifying {e['path']}", file=sys.stderr, flush=True)
            p = safe_path(root, e['path'])
            unchanged(p, e)
            update(planpath, plan, e, 'copied')
            check_remote(args, plan, e)
            update(planpath, plan, e, 'verified')
            target = Path(e['target'])
            if target.resolve().is_relative_to(root) or os.path.samefile(p, target):
                raise OffloadError('Mount target aliases the local source; refusing self-referential link')
            if digest(target) != e['sha256']:
                raise OffloadError('BOS mount content differs from source')
            if digest(p, nofollow=True) != e['sha256']:
                raise OffloadError('Source content changed before replacement')
            unchanged(p, e)
            update(planpath, plan, e, 'linking')
            switch_link(p, e)
            update(planpath, plan, e, 'linked')
        except (OSError, OffloadError) as error:
            fail(planpath, plan, e, error)
            failures += 1
    return int(bool(failures))


def restore(args, planpath, plan):
    root = Path(plan['root'])
    failures = 0
    for e in plan['entries']:
        if e['state'] not in ('linking', 'linked', 'restoring', 'restored'):
            continue
        temp = None
        try:
            p = safe_path(root, e['path'])
            if not expected_link(p, e):
                # A crash after rename but before journal commit leaves a regular file.
                if e['state'] not in ('restoring', 'restored') or not e.get('restore_identity'):
                    raise OffloadError('Expected our symlink; refusing to replace local file')
                s = p.lstat()
                ri = e['restore_identity']
                if not stat.S_ISREG(s.st_mode) or any(getattr(s, 'st_' + k) != ri[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'mode')) or digest(p, True) != e['sha256']:
                    raise OffloadError('Restored local file changed')
                update(planpath, plan, e, 'restored')
                continue
            if not e.get('sha256'):
                raise OffloadError('Missing recorded content digest')
            update(planpath, plan, e, 'restoring')
            print(f"Restoring {e['path']}", file=sys.stderr, flush=True)
            # Stable staging name makes interrupted downloads reusable without litter.
            temp = p.parent / ('.pfs2bos-restore-' + plan['id'] + '-' + hashlib.sha256(e['path'].encode()).hexdigest()[:20])
            if os.path.lexists(temp):
                if not stat.S_ISREG(temp.lstat().st_mode) or temp.lstat().st_nlink != 1:
                    raise OffloadError('Unsafe restore staging file')
                temp.unlink()
            run_rclone(args, 'copyto', e['remote'], temp, '--ignore-times')
            if digest(temp, True) != e['sha256']:
                raise OffloadError('Downloaded restore content differs from recorded source')
            os.chmod(temp, stat.S_IMODE(e['identity']['mode']))
            os.utime(temp, ns=(e['identity']['mtime_ns'], e['identity']['mtime_ns']))
            with open(temp, 'rb') as f:
                os.fsync(f.fileno())
            update(planpath, plan, e, 'restoring', restore_identity=identity(temp.lstat()))
            if not expected_link(p, e):
                raise OffloadError('Source path changed during restore')
            os.replace(temp, p)
            fsync_dir(p.parent)
            update(planpath, plan, e, 'restored')
        except (OSError, OffloadError) as error:
            fail(planpath, plan, e, error)
            failures += 1
        finally:
            if temp is not None and temp.exists():
                temp.unlink()
    return int(bool(failures))
