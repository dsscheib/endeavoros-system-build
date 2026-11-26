#!/usr/bin/env python3
"""Ensure Btrfs @snapshots subvolume exists and is mounted at /.snapshots."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from st_assets import atomic_text


def snapshots_fstab(text, uuid):
    """Ensure /.snapshots mounts subvol=@snapshots on `uuid`, rejecting foreign entries."""
    lines = []
    has_valid_entry = False
    for line in text.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith('#'):
            if len(fields) < 4:
                raise ValueError('Malformed fstab entry')
            if fields[1] == '/.snapshots':
                opts = fields[3].split(',')
                valid_subvol = 'subvol=@snapshots' in opts or 'subvol=/@snapshots' in opts
                if fields[0] != f'UUID={uuid}' or fields[2] != 'btrfs' or not valid_subvol:
                    raise ValueError('Existing /.snapshots fstab entry belongs to another mount')
                has_valid_entry = True
        lines.append(line)
    if not has_valid_entry:
        lines.append(f'UUID={uuid}\t/.snapshots\tbtrfs\tdefaults,noatime,subvol=@snapshots\t0 0')
    return '\n'.join(lines) + '\n'


class SnapshotsSubvol:
    def __init__(self, root=Path('/')):
        # A fixture root is for unit tests only; the CLI always uses the real OS.
        self.root = Path(root)
        self.uuid = ''
        self.mounted = False
        self.fstab = ''

    def path(self, name):
        return self.root / name.lstrip('/')

    def read(self, name):
        return self.path(name).read_text(encoding='utf-8')

    def run(self, *args):
        return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()

    def mount_info(self, point, field):
        return self.run('findmnt', '-n', '-o', field, '--mountpoint', point)

    def preflight(self):
        for command in ('btrfs', 'findmnt', 'mount', 'umount', 'systemctl'):
            if not shutil.which(command):
                raise ValueError(f'Required command missing: {command}')
        if self.mount_info('/', 'FSTYPE') != 'btrfs' or self.mount_info('/', 'FSROOT') != '/@':
            raise ValueError('Root must mount Btrfs subvolume /@ before configuring Snapper.')
        self.uuid = self.mount_info('/', 'UUID')
        if not re.fullmatch(r'[a-fA-F0-9-]+', self.uuid):
            raise ValueError('Invalid Btrfs root filesystem UUID')
        for name in ('/etc/fstab', '/.snapshots'):
            if self.path(name).is_symlink():
                raise ValueError(f'Refusing a symlink at {name}')
        fstab_before = self.read('/etc/fstab')
        self.fstab = snapshots_fstab(fstab_before, self.uuid)

        snapshots_dir = self.path('/.snapshots')
        mounted_target = (
            self.run('findmnt', '-n', '-o', 'TARGET', '--target', str(snapshots_dir))
            if snapshots_dir.exists()
            else ''
        )
        if mounted_target == '/.snapshots':
            if (
                self.mount_info('/.snapshots', 'FSTYPE') != 'btrfs'
                or self.mount_info('/.snapshots', 'FSROOT') != '/@snapshots'
                or self.mount_info('/.snapshots', 'UUID') != self.uuid
            ):
                raise ValueError('/.snapshots is mounted from another subvolume or filesystem')
            self.mounted = True
        else:
            if snapshots_dir.exists() and (not snapshots_dir.is_dir() or any(snapshots_dir.iterdir())):
                raise ValueError('The unmounted /.snapshots directory must be empty')
            self.mounted = False

        needs_setup = (not self.mounted) or (self.fstab != fstab_before)
        if needs_setup:
            print('Needs setup: @snapshots mount or /etc/fstab entry missing.')
        else:
            print('Snapshots subvolume already configured.')
        return needs_setup

    @contextmanager
    def top_level(self):
        # Keep mounted top-level volume under /run so recursive temp cleanup can never touch data.
        directory = Path(tempfile.mkdtemp(prefix='eos-snapshots-top-', dir='/run'))
        mounted = False
        body_failed = False
        try:
            self.run('mount', '-t', 'btrfs', '-o', 'subvolid=5', f'UUID={self.uuid}', str(directory))
            mounted = True
            yield directory
        except BaseException:
            body_failed = True
            raise
        finally:
            if mounted:
                try:
                    self.run('umount', str(directory))
                except (OSError, subprocess.CalledProcessError) as exc:
                    if not body_failed:
                        raise
                    print(f'WARNING: failed to unmount {directory}: {exc}', file=sys.stderr)
            try:
                directory.rmdir()
            except OSError:
                if not body_failed and not mounted:
                    raise

    def ensure_subvolume(self):
        with self.top_level() as top:
            subvolume = top / '@snapshots'
            if subvolume.is_symlink():
                raise ValueError('@snapshots must not be a symlink')
            if subvolume.exists():
                if not subvolume.is_dir():
                    raise ValueError('@snapshots must be a Btrfs subvolume, not a file')
                self.run('btrfs', 'subvolume', 'show', str(subvolume))
                return False
            self.run('btrfs', 'subvolume', 'create', str(subvolume))
            return True

    def configure(self):
        if not self.preflight():
            return False

        if not self.mounted:
            self.ensure_subvolume()

        snapshots_dir = self.path('/.snapshots')
        if not self.mounted:
            snapshots_dir.mkdir(mode=0o750, exist_ok=True)
            os.chmod(snapshots_dir, 0o750)

        fstab_path = self.path('/etc/fstab')
        fstab_changed = self.read('/etc/fstab') != self.fstab
        if fstab_changed:
            fd, candidate = tempfile.mkstemp(dir=fstab_path.parent, prefix='.fstab.verify.')
            os.close(fd)
            candidate_path = Path(candidate)
            try:
                atomic_text(candidate_path, self.fstab, 0o644)
                self.run('findmnt', '--verify', '--tab-file', str(candidate_path))
            finally:
                candidate_path.unlink(missing_ok=True)
            backup = fstab_path.with_name('fstab.pre-eos-snapshots')
            if not backup.exists():
                shutil.copy2(fstab_path, backup)
            atomic_text(fstab_path, self.fstab, 0o644)
            self.run('systemctl', 'daemon-reload')

        if not self.mounted:
            self.run('mount', '/.snapshots')
            if (
                self.mount_info('/.snapshots', 'FSTYPE') != 'btrfs'
                or self.mount_info('/.snapshots', 'FSROOT') != '/@snapshots'
                or self.mount_info('/.snapshots', 'UUID') != self.uuid
            ):
                raise ValueError('Mounted /.snapshots did not match expected @snapshots subvolume')
            os.chmod(snapshots_dir, 0o750)

        print('Created or mounted @snapshots at /.snapshots.')
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify prerequisites without changing the system')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo on the installed system')
    try:
        with open('/run/eos-btrfs-snapshots.lock', 'w', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            setup = SnapshotsSubvol()
            if args.check:
                setup.preflight()
            else:
                setup.configure()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        parser.exit(1, f'Snapshots subvolume setup failed: {detail}\n')


if __name__ == '__main__':
    main()
