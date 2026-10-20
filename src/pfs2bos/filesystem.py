"""Local file identity, hashing, locking, and path validation."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import PurePosixPath
import stat

from .errors import OffloadError


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or str(p) != value or '\x00' in value:
        raise OffloadError(f'Unsafe relative path: {value!r}')
    return p


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(root):
    with open(root / '.pfs2bos.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OffloadError(f'Another pfs2bos operation holds {f.name}')
        yield


def identity(s):
    return {k: getattr(s, 'st_' + k) for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns', 'nlink', 'mode')}


def safe_path(root, rel):
    p = root
    parts = relative(rel).parts
    for part in parts[:-1]:
        p = p / part
        if not stat.S_ISDIR(p.lstat().st_mode):
            raise OffloadError(f'Parent is not a real directory: {p}')
    return root.joinpath(*parts)


def unchanged(path, entry):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or identity(s) != entry['identity']:
        raise OffloadError('Source changed since scan; create a new plan')
    return s


def digest(path, nofollow=False):
    fd = os.open(path, os.O_RDONLY | (os.O_NOFOLLOW if nofollow else 0) | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OffloadError(f'Not a regular file: {path}')
        h = hashlib.sha256()
        while data := f.read(8 * 1024 * 1024):
            h.update(data)
        if identity(before) != identity(os.fstat(f.fileno())):
            raise OffloadError(f'File changed while reading: {path}')
    return h.hexdigest()


def expected_link(path, entry):
    return path.is_symlink() and os.readlink(path) == entry['target']
