"""Inspect the journal against the actual local paths."""
from collections import Counter
import json
from pathlib import Path

from .errors import OffloadError
from .filesystem import digest, expected_link, safe_path, unchanged


def status(args, plan):
    rows = []
    for e in plan['entries']:
        row = {'path': e['path'], 'state': e['state'], 'error': e.get('error')}
        try:
            p = safe_path(Path(plan['root']), e['path'])
            if expected_link(p, e):
                row['actual'] = 'linked' if p.exists() else 'broken_link'
                if args.verify:
                    row['content_ok'] = digest(p) == e.get('sha256')
            elif p.is_symlink():
                row['actual'] = 'foreign_symlink'
            elif p.exists():
                row['actual'] = 'local'
                if e['state'] != 'restored':
                    unchanged(p, e)
                if args.verify and e.get('sha256'):
                    row['content_ok'] = digest(p, True) == e['sha256']
            else:
                row['actual'] = 'missing'
        except (OSError, OffloadError) as error:
            row['actual'] = 'changed_or_unreadable'
            row['inspection_error'] = str(error)
        rows.append(row)
    print(json.dumps({'states': dict(Counter(e['state'] for e in plan['entries'])),
                      'actual': dict(Counter(e['actual'] for e in rows)),
                      'pending': len(plan['pending']), 'entries': rows}, ensure_ascii=False, indent=2))
    return int(any(e['actual'] in ('broken_link', 'foreign_symlink', 'missing', 'changed_or_unreadable') or e.get('content_ok') is False or e.get('error') for e in rows))
