#!/usr/bin/env python3
"""Update Snapper shell-style config as data; preserve other registered configs."""
from pathlib import Path
import re
import shlex
from st_assets import atomic_text


def read_assignments(text):
    return {m[1]: shlex.split(m[2])[0] if shlex.split(m[2]) else ''
            for m in re.finditer(r'^([A-Z_]+)=(.*)$', text, re.MULTILINE)}


def update(text, settings):
    for key, value in settings.items():
        replacement = key + '=' + '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        pattern = r'^' + re.escape(key) + '=.*$'
        if re.search(pattern, text, re.MULTILINE):
            text = re.sub(pattern, lambda _: replacement, text, flags=re.MULTILINE)
        else:
            text = text.rstrip() + '\n' + replacement + '\n'
    return text


def _backup_path(root_path):
    if root_path.name == 'root' and root_path.parent.name == 'configs':
        return root_path.parent.parent / f'{root_path.name}.pre-eos-setup'
    return root_path.with_name(f'{root_path.name}.pre-eos-setup')


def configure(root_config, default_config, initialize=False, check_mode=False, template=None):
    root = Path(root_config)
    defaults = Path(default_config)
    root_exists = root.exists()
    if root_exists:
        text = root.read_text()
    elif initialize and template:
        text = Path(template).read_text()
    else:
        text = root.read_text()
    values = read_assignments(text)
    if not initialize and values.get('SUBVOLUME', '/') != '/':
        raise ValueError('Existing root Snapper config describes another subvolume')
    settings = {'SUBVOLUME': '/', 'FSTYPE': 'btrfs'}
    if initialize:
        settings.update(NUMBER_CLEANUP='yes', NUMBER_MIN_AGE='1800', NUMBER_LIMIT='10',
                        NUMBER_LIMIT_IMPORTANT='5', TIMELINE_CREATE='no', TIMELINE_CLEANUP='yes')
    # Existing retention and access settings are preserved. Add the admin group.
    import grp
    group = 'wheel' if any(g.gr_name == 'wheel' for g in grp.getgrall()) else 'root'
    groups = list(dict.fromkeys(values.get('ALLOW_GROUPS', '').split() + [group]))
    settings.update(ALLOW_GROUPS=' '.join(groups), SYNC_ACL='yes')
    changed = False
    updated_root = update(text, settings)
    root_needs_update = (not root_exists) or updated_root != text or (root.stat().st_mode & 0o777) != 0o640
    if root_needs_update:
        if not check_mode:
            if root_exists:
                root_backup = _backup_path(root)
                if not root_backup.exists():
                    atomic_text(root_backup, text, 0o600)
            else:
                root.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(root, updated_root, 0o640)
        changed = True
    defaults_text = defaults.read_text() if defaults.exists() else ''
    default_values = read_assignments(defaults_text)
    names = list(dict.fromkeys(default_values.get('SNAPPER_CONFIGS', '').split() + ['root']))
    updated_defaults = update(defaults_text, {'SNAPPER_CONFIGS': ' '.join(names)})
    defaults_needs_update = (not defaults.exists()) or updated_defaults != defaults_text or (defaults.stat().st_mode & 0o777) != 0o644
    if defaults_needs_update:
        if not check_mode:
            if defaults.exists():
                defaults_backup = defaults.with_name(defaults.name + '.pre-eos-setup')
                if not defaults_backup.exists():
                    atomic_text(defaults_backup, defaults_text, 0o600)
            atomic_text(defaults, updated_defaults, 0o644)
        changed = True
    return changed


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--template', default=None)
    parser.add_argument('root', nargs='?', default='/etc/snapper/configs/root')
    parser.add_argument('defaults', nargs='?', default='/etc/conf.d/snapper')
    args = parser.parse_args()
    changed = configure(
        args.root,
        args.defaults,
        initialize=args.initialize,
        check_mode=args.check,
        template=args.template,
    )
    if args.check:
        print('Would update Snapper configuration.' if changed else 'Snapper configuration already up to date.')
    else:
        print('Snapper configuration updated.' if changed else 'Snapper configuration already up to date.')
