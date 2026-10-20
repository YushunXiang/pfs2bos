import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pfs2bos import cli, config


class Configuration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        cwd = patch.object(config.Path, 'cwd', return_value=self.base)
        cwd.start()
        self.addCleanup(cwd.stop)

    def resolve(self, *extra):
        args = cli.parser().parse_args(['scan', '--out', 'plan.json', *extra])
        config.configure_scan(args)
        return args

    def test_missing_default_file_uses_defaults(self):
        args = self.resolve()
        self.assertEqual((args.root, args.remote, args.mount),
                         (config.DEFAULT_ROOT, config.DEFAULT_REMOTE, config.DEFAULT_MOUNT))

    def test_dotenv_and_precedence_without_environment_mutation(self):
        (self.base / '.env').write_text(
            'PFS2BOS_ROOT="/source with spaces"\n'
            'PFS2BOS_REMOTE=bos:bucket/prefix\n'
            'PFS2BOS_MOUNT=/mount\n', encoding='utf-8')
        args = self.resolve()
        self.assertEqual((args.root, args.remote, args.mount),
                         ('/source with spaces', 'bos:bucket/prefix', '/mount'))
        self.assertNotIn('PFS2BOS_ROOT', os.environ)
        with patch.dict(os.environ, {'PFS2BOS_ROOT': '/environment'}):
            self.assertEqual(self.resolve().root, '/environment')
            self.assertEqual(self.resolve('--root', '/cli').root, '/cli')

    def test_explicit_file_and_literal_value(self):
        path = self.base / 'custom.env'
        path.write_text('PFS2BOS_ROOT=/source/${LITERAL}\n', encoding='utf-8')
        self.assertEqual(self.resolve('--env-file', str(path)).root, '/source/${LITERAL}')

    def test_empty_values_are_rejected_but_can_be_overridden(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                line = 'PFS2BOS_ROOT' if value is None else f'PFS2BOS_ROOT="{value}"'
                (self.base / '.env').write_text(line + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'PFS2BOS_ROOT must not be empty'):
                    self.resolve()
                self.assertEqual(self.resolve('--root', '/cli').root, '/cli')

    def test_missing_explicit_file_returns_error_without_scan(self):
        with patch.object(cli, 'scan') as scan, contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['scan', '--out', 'plan.json', '--env-file', str(self.base / 'missing')])
        self.assertEqual(rc, 1)
        scan.assert_not_called()

    def test_scan_receives_resolved_settings(self):
        (self.base / '.env').write_text('PFS2BOS_ROOT=/configured\n', encoding='utf-8')
        with patch.object(cli, 'scan') as scan:
            self.assertEqual(cli.main(['scan', '--out', 'plan.json']), 0)
        self.assertEqual(scan.call_args.args[0].root, '/configured')

    def test_status_does_not_resolve_environment(self):
        with patch.object(cli, 'configure_scan') as configure, \
                patch.object(cli, 'load_plan', side_effect=ValueError('stop')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['status', '--plan', 'plan.json']), 1)
        configure.assert_not_called()
