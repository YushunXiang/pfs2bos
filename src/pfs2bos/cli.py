"""Manual scan/copy/verify/link/restore workflow."""
import argparse
from pathlib import Path
import sys

from .config import configure_scan
from .errors import OffloadError
from .filesystem import lock
from .migrate import apply, restore
from .plan import load_plan
from .scan import scan
from .status import status


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    s = commands.add_parser('scan', help='Read only: inventory artifacts and unknown files')
    s.add_argument('--root')
    s.add_argument('--env-file', help='Dotenv file (default: .env in the current directory)')
    s.add_argument('--path', action='append', help='Root-relative project directory; repeatable')
    s.add_argument('--out', required=True)
    s.add_argument('--remote')
    s.add_argument('--mount')
    s.add_argument('--data-dir', action='append', default=[], help='Explicit root-relative data directory; includes all companion files')
    s.add_argument('--config', help='JSON with data_dirs and exclude_dirs arrays')
    for name in ('apply', 'status', 'restore'):
        s = commands.add_parser(name)
        s.add_argument('--plan', required=True)
        if name == 'status':
            s.add_argument('--verify', action='store_true', help='Read and hash local files/link targets')
        else:
            s.add_argument('--rclone', default='rclone')
        if name == 'apply':
            s.add_argument('--transfers', type=int, default=4)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'scan':
            configure_scan(args)
            scan(args)
            return 0
        planpath = Path(args.plan).absolute()
        plan = load_plan(planpath)
        locked_root = plan['root']
        with lock(Path(locked_root)):
            # Read again after acquiring the root lock.
            plan = load_plan(planpath)
            if plan['root'] != locked_root:
                raise OffloadError('Plan root changed while acquiring lock')
            if args.command == 'status':
                return status(args, plan)
            if args.command == 'apply' and args.transfers < 1:
                raise OffloadError('--transfers must be positive')
            return {'apply': apply, 'restore': restore}[args.command](args, planpath, plan)
    except (OffloadError, OSError, ValueError, KeyError, TypeError) as error:
        print(f'pfs2bos: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; journal preserved. Rerun the same command to resume.', file=sys.stderr)
        return 130
