"""Fixture unit tests for scripts/ensure_snapshots_subvol.py (no real disk operations)."""
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
snapshots = importlib.import_module('ensure_snapshots_subvol')


class Fixture(snapshots.SnapshotsSubvol):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.filesystem = 'btrfs'
        self.root_subvol = '/@'
        self.root_uuid = 'aaaa-bbbb'
        self.snapshots_mounted = False
        self.snapshots_fsroot = '/@snapshots'
        self.snapshots_uuid = 'aaaa-bbbb'
        self.subvol_exists = False
        self.failure = None
        self.top_dir = root / 'run-top'

    def run(self, *args):
        self.calls.append(args)
        if self.failure and args[0] == self.failure:
            raise subprocess.CalledProcessError(42, args, stderr='fixture failure')
        if args[0] == 'findmnt':
            if '--verify' in args:
                assert self.path('/.snapshots').is_dir(), '/.snapshots must exist before findmnt --verify'
                return ''
            point = args[-1]
            field = args[3]
            if field == 'TARGET':
                return '/.snapshots' if self.snapshots_mounted else '/'
            if field == 'FSTYPE':
                return self.filesystem
            if field == 'FSROOT':
                return self.snapshots_fsroot if point == '/.snapshots' else self.root_subvol
            return self.snapshots_uuid if point == '/.snapshots' else self.root_uuid
        if args[:3] == ('btrfs', 'subvolume', 'show'):
            return 'subvolume info'
        if args[:3] == ('btrfs', 'subvolume', 'create'):
            Path(args[-1]).mkdir()
            return ''
        if args[0] == 'mount' and args[-1].endswith('/.snapshots'):
            self.snapshots_mounted = True
            os.chmod(self.path('/.snapshots'), 0o755)
            return ''
        if args[0] == 'umount':
            shutil.rmtree(Path(args[-1]) / '@snapshots', ignore_errors=True)
            return ''
        return ''


class EnsureSnapshotsSubvolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eos-snapshots-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup = Fixture(self.root)
        fstab = self.setup.path('/etc/fstab')
        fstab.parent.mkdir(parents=True)
        fstab.write_text('UUID=aaaa-bbbb\t/\tbtrfs\tdefaults,subvol=@\t0 0\n')
        self.commands = patch.object(snapshots.shutil, 'which', return_value='/fixture/tool')
        self.commands.start()
        self.addCleanup(self.commands.stop)

    def test_preflight_requires_btrfs_at_subvol_root(self):
        self.setup.root_subvol = '/'
        with self.assertRaisesRegex(ValueError, 'subvolume /@'):
            self.setup.preflight()

    def test_preflight_rejects_nonempty_unmounted_snapshots_directory(self):
        snap_dir = self.setup.path('/.snapshots')
        snap_dir.mkdir()
        (snap_dir / 'leftover').write_text('data')
        with self.assertRaisesRegex(ValueError, 'must be empty'):
            self.setup.preflight()

    def test_preflight_rejects_foreign_mounted_snapshots_or_fstab(self):
        self.setup.path('/.snapshots').mkdir()
        self.setup.snapshots_mounted = True
        self.setup.snapshots_uuid = 'other-uuid'
        with self.assertRaisesRegex(ValueError, 'another subvolume or filesystem'):
            self.setup.preflight()

        self.setup.path('/.snapshots').rmdir()
        self.setup.snapshots_mounted = False
        self.setup.snapshots_uuid = 'aaaa-bbbb'
        self.setup.path('/etc/fstab').write_text('UUID=other\t/.snapshots\tbtrfs\tsubvol=@snapshots\t0 0\n')
        with self.assertRaisesRegex(ValueError, 'belongs to another mount'):
            self.setup.preflight()

    def test_configure_creates_subvol_updates_fstab_and_is_idempotent(self):
        top_mount = self.root / 'top-level'
        top_mount.mkdir()
        with patch.object(snapshots.tempfile, 'mkdtemp', return_value=str(top_mount)):
            self.assertTrue(self.setup.configure())

        fstab = self.setup.read('/etc/fstab')
        self.assertIn('UUID=aaaa-bbbb\t/.snapshots\tbtrfs\tdefaults,noatime,subvol=@snapshots\t0 0', fstab)
        self.assertTrue(self.setup.path('/etc/fstab.pre-eos-snapshots').is_file())
        self.assertTrue(any(call[:3] == ('btrfs', 'subvolume', 'create') for call in self.setup.calls))
        self.assertIn(('systemctl', 'daemon-reload'), self.setup.calls)
        self.assertEqual(self.setup.path('/.snapshots').stat().st_mode & 0o777, 0o750)

        self.setup.calls.clear()
        self.assertFalse(self.setup.configure())
        self.assertFalse(any(call[0] in ('mount', 'btrfs', 'systemctl') for call in self.setup.calls))

    def test_configure_preserves_custom_valid_snapshots_fstab_options(self):
        self.setup.snapshots_mounted = True
        self.setup.path('/.snapshots').mkdir()
        original = (
            'UUID=aaaa-bbbb\t/\tbtrfs\tdefaults,subvol=@\t0 0\n'
            'UUID=aaaa-bbbb\t/.snapshots\tbtrfs\tdefaults,compress=zstd:1,subvol=/@snapshots\t0 0\n'
        )
        self.setup.path('/etc/fstab').write_text(original)
        self.assertFalse(self.setup.configure())
        self.assertEqual(self.setup.read('/etc/fstab'), original)

    def test_failed_findmnt_verify_preserves_existing_fstab(self):
        top_mount = self.root / 'top-level'
        top_mount.mkdir()
        original = self.setup.read('/etc/fstab')
        self.setup.failure = 'findmnt_verify'

        orig_run = self.setup.run

        def run_with_verify_fail(*args):
            if args[:2] == ('findmnt', '--verify'):
                raise subprocess.CalledProcessError(1, args, stderr='bad fstab')
            return orig_run(*args)

        with patch.object(self.setup, 'run', side_effect=run_with_verify_fail):
            with patch.object(snapshots.tempfile, 'mkdtemp', return_value=str(top_mount)):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.setup.configure()
        self.assertEqual(self.setup.read('/etc/fstab'), original)
        self.assertFalse(self.setup.path('/etc/fstab.pre-eos-snapshots').exists())

    def test_failed_umount_preserves_mountpoint_contents(self):
        top_mount = self.root / 'top-level'
        top_mount.mkdir()
        sentinel = top_mount / 'keep'
        sentinel.write_text('pool data')
        self.setup.uuid = 'aaaa-bbbb'
        self.setup.failure = 'umount'
        with patch.object(snapshots.tempfile, 'mkdtemp', return_value=str(top_mount)):
            with self.assertRaises(subprocess.CalledProcessError):
                with self.setup.top_level():
                    pass
        self.assertEqual(sentinel.read_text(), 'pool data')


if __name__ == '__main__':
    unittest.main()
