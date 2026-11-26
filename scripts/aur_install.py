#!/usr/bin/env python3
"""Query missing Arch/AUR packages and install them idempotently with an AUR helper."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PACKAGE_RE = re.compile(r'^[a-z0-9@_+][a-z0-9@._+-]*$')


def validate_packages(packages):
    """Validate and deduplicate package names while preserving order."""
    seen = set()
    ordered = []
    for pkg in packages:
        if not PACKAGE_RE.fullmatch(pkg):
            raise ValueError(f'Invalid Arch package name: {pkg!r}')
        if pkg not in seen:
            seen.add(pkg)
            ordered.append(pkg)
    return ordered


def find_missing(packages, run=subprocess.run):
    """Return requested packages not currently satisfied according to `pacman -T`."""
    ordered = validate_packages(packages)
    if not ordered:
        return []
    proc = run(['pacman', '-T', *ordered], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return []
    missing_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if missing_lines and not proc.stderr.strip() and all(line in ordered for line in missing_lines):
        missing_set = set(missing_lines)
        return [pkg for pkg in ordered if pkg in missing_set]
    raise subprocess.CalledProcessError(proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr)


def install_packages(packages, *, helper='yay', run=subprocess.run, which=shutil.which, geteuid=os.geteuid):
    """Install missing packages non-interactively via `helper`; return list installed."""
    ordered = validate_packages(packages)
    if geteuid() == 0:
        raise PermissionError('AUR helpers must run as the desktop user, not root.')
    helper_path = which(helper)
    if not helper_path:
        raise FileNotFoundError(f'AUR helper not found on PATH: {helper}')

    missing = find_missing(ordered, run=run)
    if not missing:
        return []

    cmd = [helper_path, '-S', '--needed', '--noconfirm']
    if Path(helper).name == 'yay':
        cmd.extend(['--answerdiff', 'None', '--answerclean', 'None', '--nosudoloop'])
    cmd.extend(['--sudo', '/usr/bin/sudo', '--sudoflags', '-n', *missing])
    run(cmd, check=True)

    still_missing = find_missing(missing, run=run)
    if still_missing:
        raise RuntimeError(f'Packages missing after {helper} install: {", ".join(still_missing)}')
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    missing_cmd = subparsers.add_parser('missing', help='Print uninstalled packages, one per line')
    missing_cmd.add_argument('packages', nargs='+')

    install_cmd = subparsers.add_parser('install', help='Install missing packages with AUR helper')
    install_cmd.add_argument('--helper', default='yay', help='AUR helper binary (default: yay)')
    install_cmd.add_argument('packages', nargs='+')

    args = parser.parse_args(argv)
    try:
        if args.command == 'missing':
            for pkg in find_missing(args.packages):
                print(pkg)
        elif args.command == 'install':
            installed = install_packages(args.packages, helper=args.helper)
            print(f'changed: {" ".join(installed)}' if installed else 'unchanged')
    except (ValueError, PermissionError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
