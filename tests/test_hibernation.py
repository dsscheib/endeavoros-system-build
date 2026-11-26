"""Hibernation regressions for EndeavourOS systemd-boot/dracut: fixture files and fake commands."""
import contextlib
import importlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
hibernation = importlib.import_module('configure_hibernation')
GIB = hibernation.GIB
MACHINE_ID = '0123456789abcdef0123456789abcdef'


class Fixture(hibernation.Hibernation):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.active = False
        self.offset = '123456'
        self.failure = None
        self.filesystem = 'btrfs'
        self.subvolume = '/@'
        self.devices = '1'
        self.version = 'v6.11'
        self.units = 'loaded'
        self.swap_type = 'swap'
        self.mount_uuid = 'aaaa-bbbb'
        self.mounted_swap = False
        self.fail_publish = False
        self.sleep_override = False
        self.suspend_then_hibernate_override = False
        self.dracut_listing = '\n'.join(
            f'-rwxr-xr-x   1 root     root        27232 Sep 16 14:55 {name}'
            for name in hibernation.DRACUT_RESUME_FILES
        )

    def run(self, *args):
        self.calls.append(args)
        if self.failure == 'findmnt_verify' and '--verify' in args:
            raise subprocess.CalledProcessError(1, args, stderr='bad fstab')
        if self.failure and args[0] == self.failure:
            raise subprocess.CalledProcessError(42, args, stderr='fixture failure')
        if args[0] == 'findmnt':
            if '--verify' in args:
                return ''
            point = args[-1]
            field = args[3]
            if field == 'TARGET':
                return '/swap' if self.mounted_swap else '/'
            if field == 'FSTYPE':
                return 'vfat' if point == hibernation.ESP_PATH else self.filesystem
            if field == 'FSROOT':
                return '/@swap' if point == '/swap' else self.subvolume
            return self.mount_uuid
        if args[:2] == ('btrfs', 'version'):
            return 'btrfs-progs ' + self.version
        if args[:3] == ('btrfs', 'filesystem', 'show'):
            return f'Total devices {self.devices} FS bytes used 123'
        if args[:3] == ('btrfs', 'filesystem', 'mkswapfile'):
            file = Path(args[-1])
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open('wb') as stream:
                stream.truncate(self.size)  # Sparse fixture; never allocate RAM-sized data.
            return ''
        if args[:3] == ('btrfs', 'inspect-internal', 'map-swapfile'):
            return self.offset
        if args[0] == 'blkid':
            return self.swap_type
        if args[0] == 'swapon':
            if '--show=NAME' in args:
                return str(self.path(hibernation.SWAP)) if self.active else ''
            self.active = True
            return ''
        if args[0] == 'systemctl' and args[1] == 'show':
            return self.units
        if args[0] == 'systemd-analyze':
            text = self.read(hibernation.SLEEP_CONF)
            return (text + ('AllowHibernation=no\n' if self.sleep_override else '')
                    + ('AllowSuspendThenHibernate=no\n' if self.suspend_then_hibernate_override else ''))
        if args[0] == 'lsinitrd':
            return self.dracut_listing
        if args[0] == 'reinstall-kernels' and not self.fail_publish:
            cmdline = self.read(hibernation.CMDLINE_PATH).strip()
            entry = self.path(f'{hibernation.ESP_PATH}/loader/entries/{MACHINE_ID}-6.12.1-arch1-1.conf')
            entry.write_text(
                f'linux /{MACHINE_ID}/6.12.1-arch1-1/linux\n'
                f'initrd /{MACHINE_ID}/6.12.1-arch1-1/initrd\n'
                f'options {cmdline}\n'
            )
        return ''

    def ensure_subvolume(self):
        self.calls.append(('ensure-subvolume',))
        swap_dir = self.path('/swap')
        existed = swap_dir.exists()
        swap_dir.mkdir(exist_ok=True)
        return not existed


class HibernationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eos-hibernate-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup = Fixture(self.root)
        files = {
            '/etc/os-release': 'ID=endeavouros\n',
            '/etc/machine-id': f'{MACHINE_ID}\n',
            '/sys/power/state': 'freeze mem disk\n',
            '/sys/power/disk': '[platform] shutdown reboot\n',
            '/sys/kernel/security/lockdown': '[none] integrity confidentiality\n',
            '/proc/meminfo': 'MemTotal:       16777216 kB\n',
            '/etc/fstab': '# keep\nUUID=aaaa-bbbb / btrfs subvol=@ 0 0\n'
                          '/oldswap none swap sw 0 0\n',
            '/etc/kernel/cmdline': 'quiet root=UUID=aaaa-bbbb rootflags=subvol=@ noresume resume=UUID=old resume_offset=7\n',
            f'/efi/loader/entries/{MACHINE_ID}-6.12.1-arch1-1.conf': 'options quiet\n',
            f'/efi/{MACHINE_ID}/6.12.1-arch1-1/linux': 'fixture kernel',
            f'/efi/{MACHINE_ID}/6.12.1-arch1-1/initrd': 'fixture initramfs',
            '/usr/lib/dracut/modules.d/74resume/module-setup.sh': '# dracut resume module\n',
        }
        for name, content in files.items():
            path = self.setup.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.commands = patch.object(hibernation.shutil, 'which', return_value='/fixture/tool')
        self.commands.start()
        self.addCleanup(self.commands.stop)
        self.space = patch.object(hibernation.shutil, 'disk_usage',
                                  return_value=type('Space', (), {'free': 100 * GIB})())
        self.space.start()
        self.addCleanup(self.space.stop)

    def test_size_includes_ram_and_headroom_rounded_up(self):
        self.assertEqual(hibernation.swap_size('MemTotal: 16777216 kB\n'), 20 * GIB)
        self.assertEqual(hibernation.swap_size('MemTotal: 1048576 kB\n'), 3 * GIB)
        self.assertEqual(hibernation.swap_size('MemTotal: 16777217 kB\n'), 21 * GIB)
        for text in ('', 'MemTotal: 0 kB\n'):
            with self.assertRaises(ValueError):
                hibernation.swap_size(text)

    def test_preflight_does_not_change_configuration(self):
        before = self.setup.read('/etc/fstab')
        self.setup.preflight()
        self.assertEqual(self.setup.read('/etc/fstab'), before)
        self.assertFalse(self.setup.path('/swap').exists())
        self.assertFalse(any(call[0] in ('mount', 'swapon', 'reinstall-kernels')
                             for call in self.setup.calls))

    def test_full_configuration_preserves_existing_swap_and_is_repeatable(self):
        lid = self.setup.path(hibernation.LID_CONF)
        lid.parent.mkdir(parents=True)
        lid.write_text('[Login]\nHandleLidSwitch=suspend\n')
        self.assertTrue(self.setup.configure())
        swap = self.setup.path(hibernation.SWAP)
        self.assertEqual(swap.stat().st_size, 20 * GIB)
        self.assertEqual(swap.stat().st_mode & 0o777, 0o600)
        fstab = self.setup.read('/etc/fstab')
        self.assertIn('/oldswap none swap sw 0 0', fstab)
        self.assertIn('subvol=@swap', fstab)
        self.assertEqual(self.setup.read(hibernation.DRACUT_CONF), 'add_dracutmodules+=" resume "\n')
        sleep_conf = self.setup.read(hibernation.SLEEP_CONF)
        self.assertIn('AllowHibernation=yes', sleep_conf)
        self.assertIn('AllowSuspendThenHibernate=yes', sleep_conf)
        self.assertIn('HibernateDelaySec=1h', sleep_conf)
        self.assertEqual(lid.read_text(), '[Login]\nHandleLidSwitch=suspend-then-hibernate\n'
                         'HandleLidSwitchExternalPower=suspend\nHandleLidSwitchDocked=ignore\n')
        lid_backup = lid.with_name(lid.name + '.pre-hibernation')
        self.assertEqual(lid_backup.read_text(), '[Login]\nHandleLidSwitch=suspend\n')
        cmdline = self.setup.read(hibernation.CMDLINE_PATH).strip()
        self.assertEqual(
            cmdline,
            'quiet root=UUID=aaaa-bbbb rootflags=subvol=@ resume=UUID=aaaa-bbbb resume_offset=123456',
        )
        commands = [call[0] for call in self.setup.calls]
        self.assertIn('reinstall-kernels', commands)
        self.assertIn(('lsinitrd', str(self.setup.path(f'/efi/{MACHINE_ID}/6.12.1-arch1-1/initrd'))), self.setup.calls)
        self.assertIn(('systemctl', 'daemon-reload'), self.setup.calls)
        self.assertNotIn('swapoff', commands)

        original_backup = self.setup.read('/etc/fstab.pre-hibernation')
        self.setup.calls.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            second_changed = self.setup.configure()
        self.assertFalse(second_changed)
        self.assertIn('Hibernation already configured:', buf.getvalue())
        self.assertEqual(self.setup.read('/etc/fstab'), fstab)
        self.assertEqual(self.setup.read('/etc/fstab.pre-hibernation'), original_backup)
        self.assertEqual(lid_backup.read_text(), '[Login]\nHandleLidSwitch=suspend\n')
        second_commands = [call[0] for call in self.setup.calls]
        self.assertNotIn('reinstall-kernels', second_commands)
        self.assertNotIn(('systemctl', 'daemon-reload'), self.setup.calls)
        self.assertEqual(sum(call[:3] == ('btrfs', 'filesystem', 'mkswapfile')
                             for call in self.setup.calls), 0)
        self.assertEqual(sum(call[0] == 'swapon' and '--priority' in call
                             for call in self.setup.calls), 0)

    def test_dracut_image_without_systemd_resume_stops_before_lid_policy(self):
        for missing in hibernation.DRACUT_RESUME_FILES:
            with self.subTest(missing=missing):
                self.setup.dracut_listing = '\n'.join(
                    f'-rw-r--r--   1 root root 666 Sep 16 14:55 {name}'
                    for name in hibernation.DRACUT_RESUME_FILES if name != missing
                )
                with self.assertRaisesRegex(ValueError, 'systemd-hibernate-resume'):
                    self.setup.configure()
                self.assertFalse(self.setup.path(hibernation.LID_CONF).exists())

    def test_requires_dracut_resume_module(self):
        shutil.rmtree(self.setup.path(hibernation.DRACUT_MODULES))
        with self.assertRaisesRegex(ValueError, 'Install dracut'):
            self.setup.preflight()
        self.setup.path(hibernation.DRACUT_MODULES + '/74resume').mkdir(parents=True)
        with patch.object(hibernation.shutil, 'which',
                          side_effect=lambda name: None if name == 'lsinitrd' else '/fixture/tool'):
            with self.assertRaisesRegex(ValueError, 'Required command missing: lsinitrd'):
                self.setup.preflight()
        self.assertFalse(self.setup.path('/swap').exists())

    def test_drifted_efi_entry_triggers_boot_refresh(self):
        self.assertTrue(self.setup.configure())
        self.setup.calls.clear()
        entry = self.setup.path(f'/efi/loader/entries/{MACHINE_ID}-6.12.1-arch1-1.conf')
        entry.write_text(
            f'linux /{MACHINE_ID}/6.12.1-arch1-1/linux\n'
            f'initrd /{MACHINE_ID}/6.12.1-arch1-1/initrd\n'
            'options root=UUID=aaaa-bbbb quiet resume=UUID=aaaa-bbbb\n'
        )
        self.assertTrue(self.setup.configure())
        commands = [call[0] for call in self.setup.calls]
        self.assertIn('reinstall-kernels', commands)

    def test_rejects_unsupported_systems_before_disk_mutation(self):
        cases = [
            ('/sys/power/state', 'freeze mem'),
            ('/sys/kernel/security/lockdown', 'none [integrity] confidentiality'),
            ('/etc/os-release', 'ID=ubuntu'),
            ('/sys/power/disk', 'reboot test_resume'),
            ('/etc/machine-id', 'not-a-machine-id\n'),
        ]
        for name, value in cases:
            with self.subTest(name=name):
                original = self.setup.read(name)
                self.setup.path(name).write_text(value)
                with self.assertRaises(ValueError):
                    self.setup.preflight()
                self.assertFalse(self.setup.path('/swap').exists())
                self.setup.path(name).write_text(original)
        for field, value in [('version', 'v5.16.2'), ('devices', '2'),
                             ('filesystem', 'ext4'), ('subvolume', '/'), ('units', 'masked')]:
            with self.subTest(field=field):
                original = getattr(self.setup, field)
                setattr(self.setup, field, value)
                with self.assertRaises(ValueError):
                    self.setup.preflight()
                setattr(self.setup, field, original)

    def test_rejects_insufficient_space(self):
        with patch.object(hibernation.shutil, 'disk_usage',
                          return_value=type('Space', (), {'free': 21 * GIB})()):
            with self.assertRaisesRegex(ValueError, 'free'):
                self.setup.preflight()

    def test_rejects_foreign_mount_and_preserves_fstab(self):
        original = 'UUID=other /swap btrfs subvol=@home 0 0\n'
        self.setup.path('/etc/fstab').write_text(original)
        with self.assertRaisesRegex(ValueError, 'another mount'):
            self.setup.preflight()
        self.assertEqual(self.setup.read('/etc/fstab'), original)

    def test_existing_small_swap_is_never_resized(self):
        path = self.setup.path(hibernation.SWAP)
        path.parent.mkdir()
        path.write_text('existing swap data')
        with self.assertRaisesRegex(ValueError, 'too small'):
            self.setup.configure()
        self.assertEqual(path.read_text(), 'existing swap data')

    def test_mapping_or_activation_failure_does_not_publish_resume(self):
        for failure in ('invalid-offset', 'swapon'):
            with self.subTest(failure=failure):
                self.setup.offset = 'not-an-offset' if failure == 'invalid-offset' else '123456'
                self.setup.failure = 'swapon' if failure == 'swapon' else None
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.setup.configure()
                self.assertFalse(self.setup.path(hibernation.DRACUT_CONF).exists())
                self.assertFalse(any(call[0] == 'reinstall-kernels' for call in self.setup.calls))

    def test_reinstall_kernels_failure_skips_lid_policy(self):
        self.setup.failure = 'reinstall-kernels'
        with self.assertRaises(subprocess.CalledProcessError):
            self.setup.configure()
        self.assertFalse(self.setup.path(hibernation.LID_CONF).exists())

    def test_verifies_published_boot_parameters(self):
        self.setup.fail_publish = True
        with self.assertRaisesRegex(ValueError, 'EFI boot entry'):
            self.setup.configure()

    def test_reports_conflicting_sleep_configuration(self):
        self.setup.sleep_override = True
        with self.assertRaisesRegex(ValueError, 'overrides AllowHibernation'):
            self.setup.configure()

    def test_reports_suspend_then_hibernate_disabled_elsewhere(self):
        self.setup.suspend_then_hibernate_override = True
        with self.assertRaisesRegex(ValueError, 'overrides AllowSuspendThenHibernate'):
            self.setup.configure()
        self.assertFalse(self.setup.path(hibernation.LID_CONF).exists())

    def test_failed_unmount_does_not_remove_mountpoint_contents(self):
        directory = self.root / 'top-level'
        directory.mkdir()
        important = directory / 'keep'
        important.write_text('filesystem data')
        self.setup.uuid = 'aaaa-bbbb'
        self.setup.failure = 'umount'
        with patch.object(hibernation.tempfile, 'mkdtemp', return_value=str(directory)):
            with self.assertRaises(subprocess.CalledProcessError):
                with self.setup.top_level():
                    pass
        self.assertEqual(important.read_text(), 'filesystem data')

    def test_mounted_swap_must_be_on_expected_filesystem(self):
        self.setup.path('/swap').mkdir()
        self.setup.mounted_swap = True
        self.setup.uuid = 'aaaa-bbbb'
        self.setup.mount_uuid = 'cccc-dddd'
        with self.assertRaisesRegex(ValueError, 'another subvolume'):
            hibernation.Hibernation.ensure_subvolume(self.setup)

    def test_unmounted_swap_directory_must_be_empty(self):
        self.setup.path('/swap').mkdir()
        self.setup.path('/swap/keep').touch()
        with self.assertRaisesRegex(ValueError, 'must be empty'):
            hibernation.Hibernation.ensure_subvolume(self.setup)

    def test_creates_and_mounts_swap_from_top_level(self):
        self.setup.uuid = 'aaaa-bbbb'
        directory = self.root / 'top-level'
        directory.mkdir()
        with patch.object(hibernation.tempfile, 'mkdtemp', return_value=str(directory)):
            hibernation.Hibernation.ensure_subvolume(self.setup)
        commands = self.setup.calls
        self.assertTrue(any(call[:3] == ('btrfs', 'subvolume', 'create')
                            and call[-1].endswith('/@swap') for call in commands))
        mounts = [call for call in commands if call[0] == 'mount']
        self.assertIn('subvolid=5', mounts[0])
        self.assertIn('subvol=@swap,noatime', mounts[1])
        self.assertTrue(any(call[0] == 'umount' for call in commands))

    def test_fstab_update_is_idempotent(self):
        text = hibernation.swap_fstab(self.setup.read('/etc/fstab'), 'aaaa-bbbb')
        self.assertEqual(hibernation.swap_fstab(text, 'aaaa-bbbb'), text)

        with_leading_slash = (
            'UUID=aaaa-bbbb\t/\tbtrfs\tdefaults,subvol=@\t0 0\n'
            'UUID=aaaa-bbbb\t/swap\tbtrfs\tdefaults,subvol=/@swap\t0 0\n'
            '/swap/hibernate.swap\tnone\tswap\tsw,pri=100\t0 0\n'
        )
        updated = hibernation.swap_fstab(with_leading_slash, 'aaaa-bbbb')
        self.assertIn('UUID=aaaa-bbbb\t/swap\tbtrfs\tdefaults,noatime,subvol=@swap\t0 0', updated)

    def test_failed_findmnt_verify_preserves_existing_fstab(self):
        original = self.setup.read('/etc/fstab')
        self.setup.failure = 'findmnt_verify'
        with self.assertRaises(subprocess.CalledProcessError):
            self.setup.configure()
        self.assertEqual(self.setup.read('/etc/fstab'), original)
        self.assertFalse(self.setup.path('/etc/fstab.pre-hibernation').exists())

    def test_resume_cmdline_preserves_options_and_quoted_values(self):
        cmdline = 'quiet rootflags=subvol=@ acpi_osi="Windows 2022" noresume resume=UUID=old resume_offset=1\n'
        self.assertEqual(
            hibernation.resume_cmdline(cmdline, 'aaaa', '42'),
            'quiet rootflags=subvol=@ acpi_osi="Windows 2022" resume=UUID=aaaa resume_offset=42\n',
        )


if __name__ == '__main__':
    unittest.main()
