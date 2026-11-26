#!/usr/bin/env python3
"""Configure hibernation on EndeavourOS Btrfs / systemd-boot / dracut layout.

Never hibernates the machine. Swap allocation is confined to @swap and its
hibernate.swap file; existing swap areas are preserved and never deactivated.
"""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from st_assets import atomic_text

GIB = 1024 ** 3
SWAP = '/swap/hibernate.swap'
# How long the lid-close suspend lasts before the machine hibernates.
HIBERNATE_DELAY = '1h'
DRACUT_MODULES = '/usr/lib/dracut/modules.d'
# dracut leaves its resume module out of host-only images unless resume= was
# already on the running kernel's command line; always include it.
DRACUT_CONF = '/etc/dracut.conf.d/90-eos-hibernation.conf'
DRACUT_CONF_TEXT = 'add_dracutmodules+=" resume "\n'
DRACUT_RESUME_FILES = (
    'usr/lib/systemd/system-generators/systemd-hibernate-resume-generator',
    'usr/lib/systemd/system/systemd-hibernate-resume.service',
    'usr/lib/systemd/systemd-hibernate-resume',
)
CMDLINE_PATH = '/etc/kernel/cmdline'
ESP_PATH = '/efi'
SLEEP_CONF = '/etc/systemd/sleep.conf.d/90-eos-hibernation.conf'
LID_CONF = '/etc/systemd/logind.conf.d/90-lid-close.conf'


def swap_size(meminfo):
    match = re.search(r'^MemTotal:\s+(\d+)\s+kB$', meminfo, re.MULTILINE)
    if not match or int(match[1]) == 0:
        raise ValueError('Cannot determine installed RAM from /proc/meminfo')
    ram = int(match[1]) * 1024
    # Room for a RAM-sized image plus normal swap use and image metadata.
    return ((ram + max(2 * GIB, (ram + 3) // 4) + GIB - 1) // GIB) * GIB


def resume_cmdline(text, uuid, offset):
    """Update /etc/kernel/cmdline text with resume=UUID=... and resume_offset=..."""
    cleaned = ' '.join(text.splitlines()).strip()
    shlex.split(cleaned)  # Reject malformed quoting, while retaining quotes in output.
    words = re.findall(r'''(?:[^\s"'\\]|\\.|"(?:[^"\\]|\\.)*"|'[^']*')+''', cleaned)
    kept = [
        word for word in words
        if shlex.split(word)[0] not in ('noresume', 'nohibernate')
        and not shlex.split(word)[0].startswith(('resume=', 'resume_offset='))
    ]
    kept.extend([f'resume=UUID={uuid}', f'resume_offset={offset}'])
    return ' '.join(kept) + '\n'


def swap_fstab(text, uuid):
    """Replace only our two entries, rejecting an unrelated /swap mount."""
    lines = []
    for line in text.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith('#'):
            if len(fields) < 4:
                raise ValueError('Malformed fstab entry')
            if fields[1] == '/swap':
                opts = fields[3].split(',')
                if (fields[0] != f'UUID={uuid}' or fields[2] != 'btrfs'
                        or ('subvol=@swap' not in opts and 'subvol=/@swap' not in opts)):
                    raise ValueError('Existing /swap fstab entry belongs to another mount')
                continue
            if fields[0] == SWAP:
                if fields[2] != 'swap':
                    raise ValueError('Existing hibernation swap entry is not swap')
                continue
        lines.append(line)
    lines.extend([
        f'UUID={uuid}\t/swap\tbtrfs\tdefaults,noatime,subvol=@swap\t0 0',
        f'{SWAP}\tnone\tswap\tsw,pri=100\t0 0',
    ])
    return '\n'.join(lines) + '\n'


class Hibernation:
    def __init__(self, root=Path('/')):
        # A fixture root is for unit tests only; the CLI always uses the real OS.
        self.root = Path(root)
        self.uuid = ''
        self.machine_id = ''
        self.size = 0
        self.fstab = ''
        self.cmdline_text = ''

    def path(self, name):
        return self.root / name.lstrip('/')

    def read(self, name):
        return self.path(name).read_text(encoding='utf-8')

    def run(self, *args):
        return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()

    def write(self, name, text, mode=0o644):
        path = self.path(name)
        if path.is_symlink():
            raise ValueError(f'Refusing to replace symlink: {name}')
        if path.exists() and path.read_text(encoding='utf-8') == text and (path.stat().st_mode & 0o777) == mode:
            return False
        backup = path.with_name(path.name + '.pre-hibernation')
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        atomic_text(path, text, mode)
        return True

    def mount_info(self, point, field):
        return self.run('findmnt', '-n', '-o', field, '--mountpoint', point)

    def preflight(self):
        if not any(self.path(DRACUT_MODULES).glob('*resume')):
            raise ValueError('Install dracut with its resume module first')
        for command in ('btrfs', 'findmnt', 'mount', 'umount', 'swapon', 'blkid',
                        'dracut', 'lsinitrd', 'reinstall-kernels', 'systemctl', 'systemd-analyze'):
            if not shutil.which(command):
                raise ValueError(f'Required command missing: {command}')
        if not re.search(r'^ID=(?:endeavouros|arch|"(?:endeavouros|arch)")$', self.read('/etc/os-release'), re.MULTILINE):
            raise ValueError('Automatic hibernation requires EndeavourOS or Arch Linux with systemd-boot/dracut')
        if self.mount_info('/', 'FSTYPE') != 'btrfs' or self.mount_info('/', 'FSROOT') != '/@':
            raise ValueError('Boot the installed Btrfs root subvolume @ first')
        if 'disk' not in self.read('/sys/power/state').split():
            raise ValueError('The running kernel does not support hibernation (disk sleep)')
        lockdown = self.path('/sys/kernel/security/lockdown')
        if lockdown.exists() and '[none]' not in lockdown.read_text(encoding='utf-8'):
            raise ValueError('Kernel lockdown prevents hibernation; check Secure Boot settings')
        if not {'platform', 'shutdown'}.intersection(
                self.read('/sys/power/disk').replace('[', '').replace(']', '').split()):
            raise ValueError('The kernel provides no supported hibernation power-off mode')
        version = re.search(r'v(\d+)\.(\d+)', self.run('btrfs', 'version'))
        if not version or tuple(map(int, version.groups())) < (6, 1):
            raise ValueError('Hibernation requires btrfs-progs >= 6.1 (mkswapfile/map-swapfile)')
        if self.mount_info(ESP_PATH, 'FSTYPE') != 'vfat':
            raise ValueError(f'Mount the systemd-boot EFI partition at {ESP_PATH} first')

        machine_id = self.read('/etc/machine-id').strip()
        if not re.fullmatch(r'[0-9a-f]{32}', machine_id):
            raise ValueError('Invalid or missing /etc/machine-id')
        self.machine_id = machine_id

        cmdline_path = self.path(CMDLINE_PATH)
        if cmdline_path.is_symlink() or not cmdline_path.is_file():
            raise ValueError(f'Missing or symlinked {CMDLINE_PATH}; systemd-boot/kernel-install is required')
        self.cmdline_text = cmdline_path.read_text(encoding='utf-8')
        if not self._boot_entry_paths():
            raise ValueError(f'No systemd-boot loader entries found under {ESP_PATH}/loader/entries')

        for unit in ('hibernate.target', 'systemd-hibernate.service'):
            state = self.run('systemctl', 'show', '-p', 'LoadState', '--value', unit)
            if state != 'loaded':
                raise ValueError(f'{unit} is {state}; resolve its availability/mask first')
        self.uuid = self.mount_info('/', 'UUID')
        if not re.fullmatch(r'[a-fA-F0-9-]+', self.uuid):
            raise ValueError('Invalid Btrfs filesystem UUID')
        devices = re.search(r'Total devices\s+(\d+)', self.run('btrfs', 'filesystem', 'show', '/'))
        if not devices or devices[1] != '1':
            raise ValueError('Automatic hibernation supports single-device Btrfs only')
        self.size = swap_size(self.read('/proc/meminfo'))
        self.fstab = swap_fstab(self.read('/etc/fstab'), self.uuid)
        resume_cmdline(self.cmdline_text, self.uuid, 1)  # Validate quoting before allocating swap.
        for name in ('/etc/fstab', CMDLINE_PATH, DRACUT_CONF, SLEEP_CONF, LID_CONF, '/swap'):
            if self.path(name).is_symlink():
                raise ValueError(f'Refusing a symlink at {name}')
        swap = self.path(SWAP)
        if swap.is_symlink() or (swap.exists() and not swap.is_file()):
            raise ValueError(f'{SWAP} must be a regular file, not a symlink')
        if swap.exists() and swap.stat().st_size < self.size:
            raise ValueError(
                f'{SWAP} is too small; need {self.size // GIB} GiB. '
                'Existing swap is never automatically resized or deactivated.'
            )
        needed = 0 if swap.exists() else self.size
        if shutil.disk_usage(self.path('/')).free < needed + 2 * GIB:
            raise ValueError(f'Need {needed // GIB + 2} GiB free, including filesystem headroom')
        print(f'Hibernation preflight passed: {self.size // GIB} GiB swap required.')

    @contextmanager
    def top_level(self):
        # Snapper's TMPDIR is recursively cleaned by its exit trap. Keep mounted
        # filesystems outside it, even when an unmount fails.
        directory = Path(tempfile.mkdtemp(prefix='eos-swap-', dir='/run'))
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
            # Never recursively remove a mountpoint: an unsuccessful unmount
            # must leave both the mount and its data intact for inspection.
            try:
                directory.rmdir()
            except OSError:
                if not body_failed and not mounted:
                    raise

    def ensure_subvolume(self):
        directory = self.path('/swap')
        if directory.is_symlink():
            raise ValueError('/swap must not be a symlink')
        # Query without --mountpoint: an unmounted directory resolves to /.
        mounted = self.run('findmnt', '-n', '-o', 'TARGET', '--target', str(directory)) if directory.exists() else ''
        if mounted == '/swap':
            if (self.mount_info('/swap', 'FSROOT') != '/@swap'
                    or self.mount_info('/swap', 'UUID') != self.uuid):
                raise ValueError('/swap is mounted from another subvolume or filesystem')
            return False
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError('The unmounted /swap directory must be empty')
        with self.top_level() as top:
            subvolume = top / '@swap'
            if subvolume.is_symlink():
                raise ValueError('@swap must not be a symlink')
            if subvolume.exists():
                self.run('btrfs', 'subvolume', 'show', str(subvolume))
            else:
                self.run('btrfs', 'subvolume', 'create', str(subvolume))
        directory.mkdir(mode=0o700, exist_ok=True)
        self.run('mount', '-t', 'btrfs', '-o', 'subvol=@swap,noatime',
                 f'UUID={self.uuid}', str(directory))
        if self.mount_info('/swap', 'FSROOT') != '/@swap' or self.mount_info('/swap', 'UUID') != self.uuid:
            raise ValueError('Mounted swap subvolume did not match the requested filesystem')
        return True

    def _boot_entry_paths(self):
        entries_dir = self.path(f'{ESP_PATH}/loader/entries')
        if not entries_dir.is_dir():
            return []
        all_entries = sorted(entries_dir.glob(f'{self.machine_id}-*.conf'))
        primary = [p for p in all_entries if not p.name.endswith('-fallback.conf')]
        return primary or all_entries

    def _initramfs_resumes(self, path):
        # lsinitrd prints `ls -l`-style lines; the path is a whole field.
        fields = set(self.run('lsinitrd', str(path)).split())
        return all(name in fields for name in DRACUT_RESUME_FILES)

    def _verify_boot_entry(self, offset):
        entries = self._boot_entry_paths()
        if not entries:
            raise ValueError(f'Missing systemd-boot loader entries for machine-id {self.machine_id}')
        expected = {f'resume=UUID={self.uuid}', f'resume_offset={offset}'}
        for entry_path in entries:
            entry = entry_path.read_text(encoding='utf-8')
            options = [line.split()[1:] for line in entry.splitlines()
                       if line.split() and line.split()[0] == 'options']
            if len(options) != 1 or not expected.issubset(options[0]):
                raise ValueError(f'EFI boot entry {entry_path.name} is missing the configured resume parameters')
            resume = [x for x in options[0] if x.startswith(('resume=', 'resume_offset='))]
            if len(resume) != 2 or 'noresume' in options[0] or 'nohibernate' in options[0]:
                raise ValueError(f'EFI boot entry {entry_path.name} contains conflicting resume parameters')
            images = [line.split() for line in entry.splitlines()
                      if line.split() and line.split()[0] in ('linux', 'initrd')]
            if sum(line[0] == 'linux' for line in images) != 1 or not any(line[0] == 'initrd' for line in images):
                raise ValueError(f'EFI boot entry {entry_path.name} needs a kernel and initramfs')
            has_resume = False
            for image in images:
                if len(image) != 2 or '..' in Path(image[1]).parts:
                    raise ValueError('Invalid EFI boot image path')
                path = self.path(f'{ESP_PATH}/' + image[1].lstrip('/'))
                if not path.is_file() or path.stat().st_size == 0:
                    raise ValueError(f'Missing EFI boot image: {path}')
                if image[0] == 'initrd':
                    has_resume |= self._initramfs_resumes(path)
            if not has_resume:
                raise ValueError(
                    f'EFI initramfs in {entry_path.name} is missing early-boot resume support '
                    '(systemd-hibernate-resume)'
                )

    def _boot_entry_valid(self, offset):
        try:
            self._verify_boot_entry(offset)
            return True
        except (ValueError, OSError, subprocess.CalledProcessError):
            return False

    def configure(self):
        self.preflight()
        subvol_changed = self.ensure_subvolume()
        swap_dir = self.path('/swap')
        if (swap_dir.stat().st_mode & 0o777) != 0o700:
            os.chmod(swap_dir, 0o700)
            subvol_changed = True
        swap = self.path(SWAP)
        if swap.is_symlink() or (swap.exists() and not swap.is_file()):
            raise ValueError('Refusing an invalid existing swap file')
        swap_changed = False
        if not swap.exists():
            # mkswapfile sets NOCOW, disables compression, allocates and formats.
            self.run('btrfs', 'filesystem', 'mkswapfile', '--size', str(self.size), str(swap))
            swap_changed = True
        if swap.stat().st_size < self.size:
            raise ValueError('Existing @swap file is too small; it was preserved without resizing')
        if (swap.stat().st_mode & 0o777) != 0o600:
            os.chmod(swap, 0o600)
            swap_changed = True
        if self.run('blkid', '-p', '-s', 'TYPE', '-o', 'value', str(swap)) != 'swap':
            raise ValueError('Existing file is not formatted as swap; refusing to overwrite it')
        offset = self.run('btrfs', 'inspect-internal', 'map-swapfile', '-r', str(swap))
        if not offset.isdecimal() or int(offset) <= 0:
            raise ValueError('Btrfs did not return a valid resume offset')
        active = self.run('swapon', '--show=NAME', '--noheadings', '--raw').splitlines()
        if str(swap) not in active:
            self.run('swapon', '--priority', '100', str(swap))
            swap_changed = True
        active = self.run('swapon', '--show=NAME', '--noheadings', '--raw').splitlines()
        if str(swap) not in active:
            raise ValueError('Hibernation swap did not become active')
        fstab_path = self.path('/etc/fstab')
        if self.read('/etc/fstab') != self.fstab:
            fd, candidate = tempfile.mkstemp(dir=fstab_path.parent, prefix='.fstab.verify.')
            os.close(fd)
            candidate_path = Path(candidate)
            try:
                atomic_text(candidate_path, self.fstab, 0o644)
                self.run('findmnt', '--verify', '--tab-file', str(candidate_path))
            finally:
                candidate_path.unlink(missing_ok=True)
            fstab_changed = self.write('/etc/fstab', self.fstab)
        else:
            self.run('findmnt', '--verify', '--tab-file', str(fstab_path))
            fstab_changed = self.write('/etc/fstab', self.fstab)
        dracut_changed = self.write(DRACUT_CONF, DRACUT_CONF_TEXT)
        # The lid uses suspend-then-hibernate: suspend first, hibernate after
        # HIBERNATE_DELAY. If hibernation fails, the machine stays suspended
        # instead of staying awake.
        sleep_changed = self.write(
            SLEEP_CONF,
            '[Sleep]\nAllowHibernation=yes\nAllowSuspendThenHibernate=yes\n'
            f'HibernateMode=platform shutdown\nHibernateDelaySec={HIBERNATE_DELAY}\n',
        )
        sleep = self.run('systemd-analyze', 'cat-config', 'systemd/sleep.conf')
        section, settings = '', {}
        for line in sleep.splitlines():
            line = line.strip()
            if line.startswith('[') and line.endswith(']'):
                section = line
            elif section == '[Sleep]' and '=' in line and not line.startswith(('#', ';')):
                key, value = line.split('=', 1)
                settings[key.strip()] = value.strip()
        if settings.get('AllowHibernation', '').lower() not in ('yes', 'true', '1', 'on'):
            raise ValueError('Another systemd sleep drop-in overrides AllowHibernation; resolve it and rerun')
        if settings.get('AllowSuspendThenHibernate', 'yes').lower() not in ('yes', 'true', '1', 'on'):
            raise ValueError('Another systemd sleep drop-in overrides AllowSuspendThenHibernate; resolve it and rerun')

        new_cmdline = resume_cmdline(self.cmdline_text, self.uuid, offset)
        cmdline_changed = self.write(CMDLINE_PATH, new_cmdline)
        if dracut_changed or cmdline_changed or not self._boot_entry_valid(offset):
            boot_changed = True
            self.run('reinstall-kernels')
            self._verify_boot_entry(offset)
        else:
            boot_changed = False
        # Install lid policy only after validating the persistent resume setup.
        # Reboot applies it without restarting logind in the desktop session.
        lid_changed = self.write(
            LID_CONF,
            '[Login]\nHandleLidSwitch=suspend-then-hibernate\n'
            'HandleLidSwitchExternalPower=suspend\nHandleLidSwitchDocked=ignore\n',
        )
        if fstab_changed or sleep_changed or lid_changed:
            self.run('systemctl', 'daemon-reload')
        changed = any((
            subvol_changed, swap_changed, fstab_changed, dracut_changed,
            sleep_changed, cmdline_changed, boot_changed, lid_changed,
        ))
        if changed:
            print(f'Hibernation configured: {SWAP} ({swap.stat().st_size // GIB} GiB).')
            print('Reboot to load the new resume settings, then test: systemctl hibernate')
            print(f'After reboot, closing the lid on battery suspends and then hibernates after {HIBERNATE_DELAY}; '
                  'on AC it suspends, and docked it is ignored.')
            print('A successful hibernate/resume cycle is required to verify hardware and driver support.')
        else:
            print(f'Hibernation already configured: {SWAP} ({swap.stat().st_size // GIB} GiB).')
        return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Run preflight without changing the system')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo from the installed OS')
    try:
        with open('/run/eos-btrfs-hibernation.lock', 'w', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            setup = Hibernation()
            if args.check:
                setup.preflight()
            else:
                setup.configure()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        parser.exit(
            1,
            f'Hibernation setup failed: {detail}\n'
            'Existing swap is preserved. Resolve the error and rerun before trying hibernation.\n',
        )


if __name__ == '__main__':
    main()
