"""Inventory artifacts without modifying source files."""
from collections import Counter
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from .config import EXTENSIONS, excluded
from .errors import OffloadError
from .filesystem import identity, lock, relative, safe_path
from .plan import save


def git_files(repo):
    result = subprocess.run(['git', '-C', str(repo), 'ls-files', '--cached', '-z'], capture_output=True)
    if result.returncode:
        raise OffloadError(f'Cannot determine Git tracked files in {repo}: {result.stderr.decode(errors="replace")}')
    return {os.fsdecode(p) for p in result.stdout.split(b'\0') if p}


def scan(args):
    root = Path(args.root).absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise OffloadError('Root must be a canonical, real directory')
    out = Path(args.out).absolute()
    if out.exists():
        raise OffloadError('Plan already exists; choose a new --out')
    config = json.loads(Path(args.config).read_text()) if args.config else {}
    data_dirs = [str(relative(x)) for x in config.get('data_dirs', []) + args.data_dir]
    exclude_dirs = [str(relative(x)) for x in config.get('exclude_dirs', [])]
    paths = args.path or ['.']
    starts = []
    for rel in paths:
        p = root if rel == '.' else safe_path(root, rel)
        if p.is_symlink() or not p.is_dir():
            raise OffloadError(f'Scan path must be a real directory: {p}')
        # Do not bypass exclusions by choosing a directory inside them.
        if any(excluded(x) for x in p.relative_to(root).parts):
            raise OffloadError(f'Scan path is excluded: {p}')
        if any((a / 'pyvenv.cfg').exists() or (a / 'conda-meta').is_dir()
               for a in [p, *p.parents] if a == root or a.is_relative_to(root)):
            raise OffloadError(f'Scan path is inside an environment: {p}')
        starts.append(p)
    entries, pending, skipped, seen = [], [], Counter(), set()
    repositories = {}
    for start in starts:
        for ancestor in [start, *start.parents]:
            if (ancestor / '.git').exists():
                repositories.setdefault(ancestor, git_files(ancestor))
                break
    count = 0
    last = 0
    def walk_error(error):
        raise OffloadError(f'Scan failed: {error}')
    with lock(root):
        if out.exists():
            raise OffloadError('Plan already exists; choose a new --out')
        for start in starts:
            for directory, dirs, files in os.walk(start, followlinks=False, onerror=walk_error):
                parent = Path(directory)
                if (parent / '.git').exists() and parent not in repositories:
                    repositories[parent] = git_files(parent)
                kept = []
                for name in dirs:
                    p = parent / name
                    rel = p.relative_to(root).as_posix()
                    if excluded(name) or any(rel == x or rel.startswith(x + '/') for x in exclude_dirs):
                        skipped['excluded_directory'] += 1
                    elif p.is_symlink():
                        skipped['symlink'] += 1
                    elif (p / 'pyvenv.cfg').exists() or (p / 'conda-meta').is_dir():
                        skipped['environment'] += 1
                    else:
                        kept.append(name)
                dirs[:] = kept
                for name in files:
                    p = parent / name
                    rel = p.relative_to(root).as_posix()
                    if rel in seen:
                        continue
                    seen.add(rel)
                    count += 1
                    if time.monotonic() - last > 0.5:
                        print(f'\rScanned {count} files; selected {len(entries)}; pending {len(pending)}', end='', file=sys.stderr, flush=True)
                        last = time.monotonic()
                    if excluded(name) or p == out or any(rel == x or rel.startswith(x + '/') for x in exclude_dirs):
                        skipped['excluded_file'] += 1
                        continue
                    s = p.lstat()
                    if not stat.S_ISREG(s.st_mode) or s.st_nlink > 1:
                        skipped['symlink' if stat.S_ISLNK(s.st_mode) else 'special_or_hardlink'] += 1
                        continue
                    if any(p.is_relative_to(repo) and p.relative_to(repo).as_posix() in tracked for repo, tracked in repositories.items()):
                        skipped['git_tracked'] += 1
                        continue
                    reason = next((f'configured data directory: {d}' for d in data_dirs if rel == d or rel.startswith(d + '/')), None)
                    reason = reason or next((k for k, suffixes in EXTENSIONS.items() if p.suffix.lower() in suffixes), None)
                    if '\n' in rel or '\r' in rel:
                        reason = None
                    entry = {'path': rel, 'size': s.st_size, 'allocated_bytes': s.st_blocks * 512,
                             'identity': identity(s), 'reason': reason or 'unknown; review required',
                             'remote': args.remote.rstrip('/') + '/' + rel,
                             'target': str(Path(args.mount).absolute() / rel)}
                    if reason:
                        entry.update(state='planned', error=None)
                        entries.append(entry)
                    else:
                        pending.append(entry)
        plan = {'version': 1, 'id': uuid.uuid4().hex, 'root': str(root),
                'remote': args.remote.rstrip('/'), 'mount': str(Path(args.mount).absolute()),
                'created_at': time.time(), 'entries': entries, 'pending': pending,
                'skipped': dict(skipped), 'estimated_release_bytes': sum(x['allocated_bytes'] for x in entries)}
        save(out, plan)
    print(file=sys.stderr)
    print(json.dumps({'plan': str(out), 'selected': len(entries), 'pending': len(pending),
                      'estimated_release_bytes': plan['estimated_release_bytes'], 'skipped': dict(skipped)}, ensure_ascii=False))
