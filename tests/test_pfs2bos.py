import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pfs2bos import cli, filesystem, migrate, plan as plans
from pfs2bos.errors import OffloadError
from pfs2bos.status import status


@unittest.skipUnless(shutil.which('rclone'), 'rclone required for local-backend integration tests')
class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'source'
        self.remote = self.base / 'remote'
        self.mount = self.base / 'mount'
        self.root.mkdir()
        self.remote.mkdir()
        self.mount.symlink_to(self.remote, target_is_directory=True)
        self.envpath = self.base / '.env'
        self.envpath.write_text(
            f'PFS2BOS_ROOT={self.root}\nPFS2BOS_REMOTE={self.remote}\nPFS2BOS_MOUNT={self.mount}\n')
        self.planpath = self.base / 'plan.json'
        self.args = argparse.Namespace(rclone='rclone', transfers=4)
        self.write('project/data/a.parquet', b'model data\x00\xff')

    def write(self, rel, data=b'data'):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['scan', '--env-file', str(self.envpath), '--root', str(self.root), '--path', 'project', '--remote',
                         str(self.remote), '--mount', str(self.mount), '--out', str(self.planpath), *extra])
        self.assertEqual(rc, 0)
        return plans.load_plan(self.planpath)

    def reload(self):
        return plans.load_plan(self.planpath)

    def apply(self, plan):
        return migrate.apply(self.args, self.planpath, plan)

    def restore(self, plan):
        return migrate.restore(self.args, self.planpath, plan)

    def assert_local(self, rel='project/data/a.parquet'):
        p = self.root / rel
        self.assertFalse(p.is_symlink())
        self.assertTrue(p.is_file())

    def test_roundtrip_spaces_unicode_and_repeat(self):
        name = 'project/data/中文 空格 #.pt'
        self.write(name, b'weights')
        self.write('project/data/ # leading .pt', b'leading')
        self.write('project/data/empty.pt', b'')
        os.chmod(self.root / name, 0o640)
        plan = self.scan()
        original = {e['path']: (self.root / e['path']).read_bytes() for e in plan['entries']}
        self.assertEqual(self.apply(plan), 0)
        for e in plan['entries']:
            p = self.root / e['path']
            self.assertEqual(os.readlink(p), str(self.mount / e['path']))
            self.assertEqual(p.read_bytes(), original[e['path']])
            self.assertEqual(p.lstat().st_size, len(os.fsencode(e['target'])))
        self.assertEqual(self.apply(self.reload()), 0)
        self.assertEqual(self.restore(self.reload()), 0)
        self.assertEqual(self.restore(self.reload()), 0)
        for e in plan['entries']:
            self.assert_local(e['path'])
            self.assertEqual((self.root / e['path']).read_bytes(), original[e['path']])
            self.assertTrue((self.remote / e['path']).exists())
        self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o640)

    def test_selection_and_exclusions(self):
        for d in ('.git', '.snapshot', '.venv', 'miniforge3', '.cache', '.codex-20x', 'tools'):
            self.write(f'project/{d}/hidden.pt')
        # .git created above is not a repository; remove it and use a real one.
        shutil.rmtree(self.root / 'project/.git')
        subprocess.run(['git', 'init', '-q', str(self.root / 'project')], check=True)
        self.write('project/tracked.pt')
        subprocess.run(['git', '-C', str(self.root / 'project'), 'add', 'tracked.pt'], check=True)
        self.write('project/data/info.json')
        self.write('project/unknown.xyz')
        self.write('project/alternate-env/pyvenv.cfg')
        self.write('project/alternate-env/foo.pt')
        (self.root / 'project/existing.pt').symlink_to('data/a.parquet')
        (self.root / 'project/linkdir').symlink_to('data')
        self.write('project/hard.pt')
        os.link(self.root / 'project/hard.pt', self.root / 'project/hard2.pt')
        os.mkfifo(self.root / 'project/fifo.pt')
        plan = self.scan('--data-dir', 'project/data')
        self.assertEqual({e['path'] for e in plan['entries']}, {'project/data/a.parquet', 'project/data/info.json'})
        self.assertEqual({e['path'] for e in plan['pending']}, {'project/unknown.xyz'})
        self.assertEqual(plan['skipped']['git_tracked'], 1)

    def test_same_content_reused_different_content_conflict(self):
        plan = self.scan()
        remote = self.remote / 'project/data/a.parquet'
        remote.parent.mkdir(parents=True)
        conflicting = b'X' * (self.root / 'project/data/a.parquet').stat().st_size
        remote.write_bytes(conflicting)
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()
        self.assertEqual(remote.read_bytes(), conflicting)
        remote.write_bytes((self.root / 'project/data/a.parquet').read_bytes())
        previous_mtime = remote.stat().st_mtime_ns
        self.assertEqual(self.apply(self.reload()), 0)
        self.assertEqual(remote.stat().st_mtime_ns, previous_mtime)

    def test_upload_failure_preserves_source(self):
        plan = self.scan()
        with patch.object(migrate, 'run_rclone', side_effect=OffloadError('upload failed')):
            self.assertEqual(self.apply(plan), 1)
        self.assert_local()
        self.assertIn('upload failed', self.reload()['entries'][0]['error'])
        self.assertEqual(self.apply(self.reload()), 0)

    def test_check_failure_preserves_source(self):
        plan = self.scan()
        with patch.object(migrate, 'check_remote', side_effect=OffloadError('content mismatch')):
            self.assertEqual(self.apply(plan), 1)
        self.assert_local()

    def test_mount_missing_preserves_source(self):
        plan = self.scan()
        self.mount.unlink()
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()
        self.mount.symlink_to(self.remote, target_is_directory=True)
        self.assertEqual(self.apply(self.reload()), 0)

    def test_mount_wrong_content_preserves_source(self):
        plan = self.scan()
        self.mount.unlink()
        (self.mount / 'project/data').mkdir(parents=True)
        (self.mount / 'project/data/a.parquet').write_bytes(b'wrong')
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()

    def test_source_changed_before_apply(self):
        plan = self.scan()
        self.write('project/data/a.parquet', b'new content')
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()
        self.assertFalse((self.remote / 'project/data/a.parquet').exists())

    def test_source_changed_after_check(self):
        plan = self.scan()
        real_check = migrate.check_remote
        def change(*args):
            real_check(*args)
            self.write('project/data/a.parquet', b'changed')
        with patch.object(migrate, 'check_remote', side_effect=change):
            self.assertEqual(self.apply(plan), 1)
        self.assert_local()

    def test_source_inode_replaced_with_same_bytes(self):
        plan = self.scan()
        p = self.root / 'project/data/a.parquet'
        temp = self.write('project/replacement', p.read_bytes())
        os.replace(temp, p)
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()

    def test_interruption_before_switch(self):
        plan = self.scan()
        with patch.object(migrate, 'switch_link', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan)
        self.assert_local()
        self.assertEqual(self.reload()['entries'][0]['state'], 'linking')
        self.assertEqual(self.apply(self.reload()), 0)

    def test_interruption_after_switch(self):
        plan = self.scan()
        real_switch = migrate.switch_link
        def interrupted(*args):
            real_switch(*args)
            raise KeyboardInterrupt
        with patch.object(migrate, 'switch_link', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan)
        self.assertTrue((self.root / 'project/data/a.parquet').is_symlink())
        self.assertEqual(self.reload()['entries'][0]['state'], 'linking')
        self.assertEqual(self.apply(self.reload()), 0)
        self.assertEqual(self.restore(self.reload()), 0)

    def test_restore_download_failure_and_wrong_content(self):
        plan = self.scan()
        self.assertEqual(self.apply(plan), 0)
        with patch.object(migrate, 'run_rclone', side_effect=OffloadError('download failed')):
            self.assertEqual(self.restore(self.reload()), 1)
        self.assertTrue((self.root / 'project/data/a.parquet').is_symlink())
        remote = self.remote / 'project/data/a.parquet'
        original = remote.read_bytes()
        remote.write_bytes(b'bad')
        self.assertEqual(self.restore(self.reload()), 1)
        self.assertTrue((self.root / 'project/data/a.parquet').is_symlink())
        remote.write_bytes(original)
        self.assertEqual(self.restore(self.reload()), 0)

    def test_interruption_restore_before_and_after_rename(self):
        plan = self.scan()
        self.assertEqual(self.apply(plan), 0)
        real_replace = migrate.os.replace
        def before(src, dst):
            if Path(src).name.startswith('.pfs2bos-restore-'):
                raise KeyboardInterrupt
            return real_replace(src, dst)
        with patch.object(migrate.os, 'replace', side_effect=before):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(self.reload())
        self.assertTrue((self.root / 'project/data/a.parquet').is_symlink())
        def after(src, dst):
            result = real_replace(src, dst)
            if Path(src).name.startswith('.pfs2bos-restore-'):
                raise KeyboardInterrupt
            return result
        with patch.object(migrate.os, 'replace', side_effect=after):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(self.reload())
        self.assert_local()
        self.assertEqual(self.restore(self.reload()), 0)

    def test_restore_refuses_changed_local_and_foreign_link(self):
        plan = self.scan()
        self.assertEqual(self.apply(plan), 0)
        p = self.root / 'project/data/a.parquet'
        p.unlink()
        p.write_bytes(b'user replacement')
        self.assertEqual(self.restore(self.reload()), 1)
        self.assertEqual(p.read_bytes(), b'user replacement')
        p.unlink()
        p.symlink_to(self.base / 'unrelated')
        self.assertEqual(self.restore(self.reload()), 1)
        self.assertEqual(os.readlink(p), str(self.base / 'unrelated'))

    def test_parent_symlink_substitution(self):
        plan = self.scan()
        os.rename(self.root / 'project/data', self.root / 'project/moved')
        (self.root / 'project/data').symlink_to('moved')
        self.assertEqual(self.apply(plan), 1)
        self.assert_local('project/moved/a.parquet')

    def test_plan_does_not_expand(self):
        plan = self.scan()
        extra = self.write('project/data/new.pt')
        self.assertEqual(self.apply(plan), 0)
        self.assertFalse(extra.is_symlink())
        self.assertFalse((self.remote / 'project/data/new.pt').exists())

    def test_lock_and_plan_validation(self):
        self.scan()
        with filesystem.lock(self.root):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['apply', '--plan', str(self.planpath)]), 1)
        plan = self.reload()
        plan['entries'][0]['path'] = '../escape'
        plans.save(self.planpath, plan)
        with self.assertRaises(OffloadError):
            self.reload()

    def test_config_and_newline_pending(self):
        self.write('project/data/info.json')
        self.write('project/private/weights.pt')
        self.write('project/data/new\nline.pt')
        config = self.base / 'config.json'
        config.write_text(json.dumps({'data_dirs': ['project/data'], 'exclude_dirs': ['project/private']}))
        plan = self.scan('--config', str(config))
        self.assertEqual({e['path'] for e in plan['entries']}, {'project/data/a.parquet', 'project/data/info.json'})
        self.assertIn('project/data/new\nline.pt', {e['path'] for e in plan['pending']})
        self.assertEqual(self.apply(plan), 0)
        self.assert_local('project/data/new\nline.pt')

    def test_existing_plan_not_overwritten(self):
        self.scan()
        before = self.planpath.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['scan', '--env-file', str(self.envpath), '--root', str(self.root), '--out', str(self.planpath)]), 1)
        self.assertEqual(self.planpath.read_bytes(), before)

    def test_unknown_files_and_changes_after_scan_not_selected(self):
        self.write('project/metadata.json')
        plan = self.scan()
        self.assertEqual(len(plan['pending']), 1)
        self.assertEqual(self.apply(plan), 0)
        self.assert_local('project/metadata.json')

    def test_apply_refuses_offloading_restored_file_again(self):
        plan = self.scan()
        self.assertEqual(self.apply(plan), 0)
        self.assertEqual(self.restore(self.reload()), 0)
        self.assertEqual(self.apply(self.reload()), 1)
        self.assert_local()

    def test_mount_alias_of_source_is_rejected(self):
        self.scan()
        self.mount.unlink()
        self.mount.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OffloadError):
            self.reload()
        self.assert_local()

    def test_individual_mount_target_alias_is_rejected(self):
        plan = self.scan()
        self.mount.unlink()
        (self.mount / 'project/data').mkdir(parents=True)
        (self.mount / 'project/data/a.parquet').symlink_to(self.root / 'project/data/a.parquet')
        self.assertEqual(self.apply(plan), 1)
        self.assert_local()

    def test_physical_allocation_decreases(self):
        p = self.write('project/data/large.pt', b'W' * (4 * 1024 * 1024))
        # This PFS uses delayed allocation: flush before measuring st_blocks.
        with open(p, 'rb') as f:
            os.fsync(f.fileno())
        plan = self.scan()
        before = sum((self.root / e['path']).lstat().st_blocks * 512 for e in plan['entries'])
        self.assertEqual(self.apply(plan), 0)
        after = sum((self.root / e['path']).lstat().st_blocks * 512 for e in plan['entries'])
        self.assertLess(after, before)
        print(f'Allocation smoke test: {before} -> {after} local bytes')
        self.assertEqual((self.root / 'project/data/large.pt').read_bytes(), b'W' * (4 * 1024 * 1024))

    def test_cli_scan_apply_status_restore(self):
        executable = Path(sys.executable).with_name('pfs2bos')
        commands = [
            ['scan', '--env-file', str(self.envpath), '--root', str(self.root), '--path', 'project', '--remote', str(self.remote),
             '--mount', str(self.mount), '--out', str(self.planpath)],
            ['apply', '--plan', str(self.planpath)],
            ['status', '--plan', str(self.planpath), '--verify'],
            ['restore', '--plan', str(self.planpath)],
        ]
        for command in commands:
            result = subprocess.run([str(executable), *command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_local()

    def test_module_entrypoint_outside_source_directory(self):
        result = subprocess.run([sys.executable, '-m', 'pfs2bos', '--help'],
                                cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('{scan,apply,status,restore}', result.stdout)

    def test_status_verify_broken_link_and_mount_changes(self):
        plan = self.scan()
        self.assertEqual(self.apply(plan), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(status(argparse.Namespace(verify=True), self.reload()), 0)
            (self.remote / 'project/data/a.parquet').write_bytes(b'changed')
            self.assertEqual(status(argparse.Namespace(verify=True), self.reload()), 1)
            (self.remote / 'project/data/a.parquet').unlink()
            self.assertEqual(status(argparse.Namespace(verify=False), self.reload()), 1)


if __name__ == '__main__':
    unittest.main()
