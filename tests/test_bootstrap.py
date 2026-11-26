"""Tests for EndeavourOS v2 bootstrap, Ansible foundation, and pinned download metadata."""
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
import yaml

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / 'bootstrap.sh'
ANSIBLE_DIR = REPO / 'ansible'


class BootstrapAndV2FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eos-v2-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bash(self, code, *arguments, env=None):
        environment = dict(os.environ, TMPDIR=str(self.root))
        if env:
            environment.update(env)
        return subprocess.run(
            ['bash', '-euo', 'pipefail', '-c',
             f'source {shlex.quote(str(BOOTSTRAP))}\n{code}',
             'test', *map(str, arguments)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )

    def write_os_release(self, os_id, version_id=''):
        path = self.root / 'os-release'
        lines = [f'ID="{os_id}"']
        if version_id:
            lines.append(f'VERSION_ID="{version_id}"')
        path.write_text('\n'.join(lines) + '\n')
        return path

    def test_bootstrap_rejects_root_user(self):
        result = self.bash('require_user 0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('without sudo', result.stderr)

    def test_bootstrap_accepts_non_root_user(self):
        result = self.bash('require_user 1000')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_accepts_endeavouros_and_arch_systems(self):
        for os_id in ('endeavouros', 'arch'):
            with self.subTest(os_id=os_id):
                os_release = self.write_os_release(os_id)
                result = self.bash('require_supported_system "$1" x86_64', os_release)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_rejects_unsupported_systems_or_architectures(self):
        cases = (
            ('pop', '24.04', 'x86_64'),
            ('ubuntu', '24.04', 'x86_64'),
            ('debian', '12', 'x86_64'),
            ('endeavouros', '', 'aarch64'),
            ('arch', '', 'aarch64'),
        )
        for os_id, version_id, arch in cases:
            with self.subTest(os_id=os_id, version_id=version_id, arch=arch):
                os_release = self.write_os_release(os_id, version_id)
                result = self.bash('require_supported_system "$1" "$2"', os_release, arch)
                self.assertNotEqual(result.returncode, 0)

    def test_bootstrap_verify_sha256(self):
        sample = self.root / 'sample.bin'
        sample.write_bytes(b'eos-v2-checksum-test')
        good = self.bash('verify_sha256 "$1" "$(sha256sum "$1" | cut -d" " -f1)"', sample)
        self.assertEqual(good.returncode, 0, good.stderr)

        bad_digest = self.bash('verify_sha256 "$1" "0000"', sample)
        self.assertNotEqual(bad_digest.returncode, 0)

        mismatch = self.bash('verify_sha256 "$1" "' + ('0' * 64) + '"', sample)
        self.assertNotEqual(mismatch.returncode, 0)

    def test_bootstrap_download_verified_preserves_existing_file_on_failure(self):
        dest = self.root / 'tool.bin'
        dest.write_text('working')
        bad_hash = self.bash('''
curl() { printf partial > "${@: -1}"; }
download_verified https://example.invalid/tool.bin "$1" "$(printf '%064d' 0)"
''', dest)
        self.assertNotEqual(bad_hash.returncode, 0)
        self.assertEqual(dest.read_text(), 'working')
        self.assertFalse(list(self.root.glob('*.download.*')))

        net_err = self.bash(
            'curl() { return 42; }; download_verified https://example.invalid/tool.bin "$1" ignored',
            dest,
        )
        self.assertNotEqual(net_err.returncode, 0)
        self.assertEqual(dest.read_text(), 'working')

    def test_bootstrap_install_ansible_enforces_exact_pins_when_version_differs(self):
        calls = self.root / 'calls.log'
        result = self.bash('''
SCRIPT_DIR="$2"
log_file="$1"
ansible-playbook() { printf 'ansible-playbook [core 2.19.0]\n'; }
ansible-lint() { printf 'ansible-lint 25.1.0\n'; }
uv() { printf 'uv %s\n' "$*" >> "$log_file"; }
ansible-galaxy() { printf 'galaxy %s\n' "$*" >> "$log_file"; }
install_ansible
''', calls, REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        logged = calls.read_text()
        self.assertIn('uv tool install --force --with-executables-from ansible-core ansible-core==', logged)
        self.assertIn('uv tool install --force ansible-lint==', logged)
        self.assertIn('galaxy collection install --upgrade -r', logged)

    def test_bootstrap_install_uv_upgrades_outdated_local_uv(self):
        import io as _io
        import tarfile as _tarfile

        home = self.root / 'home-uv'
        local_bin = home / '.local/bin'
        local_bin.mkdir(parents=True)
        uv_path = local_bin / 'uv'
        uv_path.write_text('#!/bin/sh\nprintf "uv 0.1.0\\n"\n')
        uv_path.chmod(0o755)

        new_uv = b'#!/bin/sh\nprintf "upgraded-uv\\n"\n'
        new_uvx = b'#!/bin/sh\nprintf "upgraded-uvx\\n"\n'
        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode='w:gz') as archive:
            for name, body in (('uv-x86_64-unknown-linux-gnu/uv', new_uv),
                               ('uv-x86_64-unknown-linux-gnu/uvx', new_uvx)):
                info = _tarfile.TarInfo(name)
                info.size = len(body)
                info.mode = 0o755
                archive.addfile(info, _io.BytesIO(body))
        payload_file = self.root / 'new-uv.tar.gz'
        tar_bytes = buf.getvalue()
        payload_file.write_bytes(tar_bytes)
        new_sha = hashlib.sha256(tar_bytes).hexdigest()

        res = self.bash('''
SCRIPT_DIR="$5"
HOME="$1"
WORK_DIR="$2"
mkdir -p "$WORK_DIR"
UV_SHA256="$3"
PAYLOAD="$4"
PATH="$HOME/.local/bin:$PATH"
curl() { cp "$PAYLOAD" "${@: -1}"; }
install_uv
''', home, self.root / 'work-uv', new_sha, payload_file, REPO)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(uv_path.read_bytes(), new_uv)
        self.assertEqual((local_bin / 'uvx').read_bytes(), new_uvx)

        skip_res = self.bash('''
SCRIPT_DIR="$3"
HOME="$1"
WORK_DIR="$2"
PATH="$HOME/.local/bin:$PATH"
uv() { printf "uv %s\\n" "$UV_VERSION"; }
curl() { printf 'should not download\\n' >&2; return 99; }
install_uv
''', home, self.root / 'work-uv', REPO)
        self.assertEqual(skip_res.returncode, 0, skip_res.stderr)

    def test_bootstrap_install_mise_upgrades_outdated_local_mise(self):
        home = self.root / 'home'
        local_bin = home / '.local/bin'
        local_bin.mkdir(parents=True)
        mise_path = local_bin / 'mise'
        mise_path.write_text('#!/bin/sh\nprintf "2026.1.0 linux-x64 (2026-01-01)\\n"\n')
        mise_path.chmod(0o755)
        new_payload = b'#!/bin/sh\nprintf "upgraded-mise\\n"\n'
        new_sha = hashlib.sha256(new_payload).hexdigest()
        payload_file = self.root / 'new-mise'
        payload_file.write_bytes(new_payload)
        res = self.bash('''
HOME="$1"
WORK_DIR="$2"
MISE_SHA256="$3"
PAYLOAD="$4"
PATH="$HOME/.local/bin:$PATH"
curl() { cp "$PAYLOAD" "${@: -1}"; }
install_mise
''', home, self.root / 'work', new_sha, payload_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(mise_path.read_bytes(), new_payload)

        skip_res = self.bash('''
HOME="$1"
WORK_DIR="$2"
PATH="$HOME/.local/bin:$PATH"
mise() { printf "%s linux-x64\\n" "${MISE_VERSION#v}"; }
curl() { printf 'should not download\n' >&2; return 99; }
install_mise
''', home, self.root / 'work')
        self.assertEqual(skip_res.returncode, 0, skip_res.stderr)

    def test_tpm_missing_plugin_probe_ignores_comments_and_strips_branch_refs(self):
        tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/workstation/tasks/main.yml').read_text(encoding='utf-8'))
        tmux_block = next(entry['block'] for entry in tasks if 'tmux' in entry.get('tags', []))
        probe_task = next(t for t in tmux_block if t['name'] == 'Check declared tmux plugins missing from plugin directory')
        argv = probe_task['ansible.builtin.command']['argv']
        self.assertEqual(argv[:2], ['python3', '{{ repo_root }}/scripts/tmux_missing_plugins.py'])

        home = self.root / 'home'
        pdir = home / '.local/share/tmux/plugins'
        (home / '.config/tmux').mkdir(parents=True)
        (pdir / 'installed-plugin').mkdir(parents=True)
        (home / '.config/tmux/tmux.conf').write_text(
            "# set -g @plugin 'commented/ignored'\n"
            "  set -g @plugin 'tmux-plugins/tpm'\n"
            "set -g @plugin 'user/branch-plugin#main'\n"
            "set -g @plugin 'https://github.com/user/git-plugin.git'\n"
            "set-option -g @plugin \"user/installed-plugin#v2\" # inline comment\n"
        )
        res = subprocess.run(
            ['python3', str(REPO / 'scripts/tmux_missing_plugins.py'), str(home)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(res.stdout.splitlines(), ['tpm', 'branch-plugin', 'git-plugin'])

    def test_desktop_apps_and_systemd_check_mode_guards(self):
        pacman_apps = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/pacman_apps.yml').read_text(encoding='utf-8'))
        kicad_block = next(entry['block'] for entry in pacman_apps if 'block' in entry and 'kicad' in entry.get('tags', []))
        uv_probe = next(t for t in kicad_block if t['name'] == 'Check whether uv is available for kicad-python venv')
        self.assertIs(uv_probe['check_mode'], False)
        kicad_venv_task = next(
            t for entry in kicad_block if 'block' in entry
            for t in entry['block'] if t['name'] == 'Create kicad-python virtual environment'
        )
        self.assertIn('--clear', kicad_venv_task['ansible.builtin.command']['argv'])

        ws_tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/workstation/tasks/main.yml').read_text(encoding='utf-8'))
        nvim_block = next(entry['block'] for entry in ws_tasks if 'neovim' in entry.get('tags', []))
        nvim_venv_task = next(
            t for entry in nvim_block if 'block' in entry
            for t in entry['block'] if t['name'] == 'Create Neovim Python provider venv'
        )
        self.assertIn('--clear', nvim_venv_task['ansible.builtin.command']['argv'])

        vendor_apps = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/vendor_apps.yml').read_text(encoding='utf-8'))
        saleae_block = next(entry['block'] for entry in vendor_apps if 'saleae' in entry.get('tags', []))
        saleae_dir_stat = next(t for t in saleae_block if t['name'] == 'Check whether Saleae Logic destination directory exists')
        self.assertEqual(saleae_dir_stat['register'], 'desktop_apps_saleae_dir_stat')
        saleae_dl = next(t for t in saleae_block if t['name'] == 'Download verified Saleae Logic 2 AppImage')
        self.assertIn('desktop_apps_saleae_dir_stat.stat.exists', saleae_dl['when'])

        flatpak_tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/flatpak.yml').read_text(encoding='utf-8'))
        fp_probe = next(t for t in flatpak_tasks if t['name'] == 'Check whether flatpak binary is installed')
        self.assertIs(fp_probe['check_mode'], False)
        for task_name in ('Configure user Flathub remote', 'Install Flathub desktop applications'):
            task = next(t for t in flatpak_tasks if t['name'] == task_name)
            self.assertIn('desktop_apps_flatpak_which.rc == 0', task['when'])

        system_tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/system/tasks/main.yml').read_text(encoding='utf-8'))
        group_probe = next(t for t in system_tasks if t['name'] == 'Check existing hardware device groups')
        user_groups_task = next(t for t in system_tasks if t['name'] == 'Add workstation user to uucp and plugdev groups')
        self.assertIs(group_probe['check_mode'], False)
        self.assertEqual(user_groups_task['ansible.builtin.user']['groups'], '{{ system_existing_hardware_groups }}')
        ufw_probe = next(t for t in system_tasks if t['name'] == 'Check whether UFW binary is installed')
        ufw_unit_probe = next(t for t in system_tasks if t['name'] == 'Check whether ufw.service unit exists')
        oomd_probe = next(t for t in system_tasks if t['name'] == 'Check whether systemd-oomd.service unit exists')
        self.assertIs(ufw_probe['check_mode'], False)
        self.assertIs(ufw_unit_probe['check_mode'], False)
        self.assertIs(oomd_probe['check_mode'], False)

        def find_task(nodes, task_name):
            for entry in nodes:
                if isinstance(entry, dict):
                    if entry.get('name') == task_name:
                        return entry
                    if 'block' in entry:
                        nested = find_task(entry['block'], task_name)
                        if nested is not None:
                            return nested
            raise KeyError(task_name)

        snapper_tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/snapper/tasks/main.yml').read_text(encoding='utf-8'))
        subvol_probe = find_task(snapper_tasks, 'Check Btrfs @ root and @snapshots layout prerequisites')
        cleanup_probe = find_task(snapper_tasks, 'Check whether snapper-cleanup.timer unit exists')
        snapper_apply = find_task(snapper_tasks, 'Apply Snapper root configuration settings')
        self.assertIs(subvol_probe['check_mode'], False)
        self.assertIs(cleanup_probe['check_mode'], False)
        self.assertIn('not ansible_check_mode', snapper_apply['when'])

        aur_tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/aur_install/tasks/main.yml').read_text(encoding='utf-8'))
        helper_probe = next(t for t in aur_tasks if t['name'] == 'Check AUR helper availability')
        missing_probe = next(t for t in aur_tasks if t['name'] == 'Query missing AUR packages')
        self.assertIs(helper_probe['check_mode'], False)
        self.assertIs(missing_probe['check_mode'], False)

    def test_aur_install_includes_define_apply_tags(self):
        def walk_tasks(nodes):
            for entry in nodes or ():
                if not isinstance(entry, dict):
                    continue
                include_role = entry.get('ansible.builtin.include_role')
                if isinstance(include_role, dict) and include_role.get('name') == 'aur_install':
                    apply_cfg = include_role.get('apply', {})
                    self.assertIsInstance(apply_cfg, dict, f"Missing apply on {entry.get('name')}")
                    self.assertTrue(apply_cfg.get('tags'), f"Missing apply.tags on {entry.get('name')}")
                for key in ('block', 'rescue', 'always'):
                    if key in entry:
                        walk_tasks(entry[key])

        for yml in sorted((ANSIBLE_DIR / 'roles').rglob('*.yml')):
            parsed = yaml.safe_load(yml.read_text(encoding='utf-8'))
            if isinstance(parsed, list):
                walk_tasks(parsed)

    def test_bootstrap_pins_are_valid_https_and_sha256(self):
        result = self.bash(
            'printf "%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n" '
            '"$MISE_VERSION" "$MISE_URL" "$MISE_SHA256" '
            '"$UV_VERSION" "$UV_URL" "$UV_SHA256"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        mise_ver, mise_url, mise_sha, uv_ver, uv_url, uv_sha = result.stdout.splitlines()
        for ver, url, sha in ((mise_ver, mise_url, mise_sha), (uv_ver, uv_url, uv_sha)):
            self.assertTrue(ver)
            self.assertTrue(url.startswith('https://'))
            self.assertNotIn('/latest/', url)
            self.assertRegex(sha, re.compile(r'^[0-9a-f]{64}$'))

        text = BOOTSTRAP.read_text(encoding='utf-8')
        self.assertRegex(text, r'ANSIBLE_CORE_VERSION="[0-9]+\.[0-9]+\.[0-9]+"')
        self.assertRegex(text, r'ANSIBLE_LINT_VERSION="[0-9]+\.[0-9]+\.[0-9]+"')
        self.assertNotIn('self_update', text)

    def test_requirements_versions_are_pinned_exactly(self):
        reqs = yaml.safe_load((ANSIBLE_DIR / 'requirements.yml').read_text(encoding='utf-8'))
        for entry in reqs['collections']:
            with self.subTest(name=entry['name']):
                self.assertRegex(entry['version'], r'^[0-9]+\.[0-9]+\.[0-9]+$')

    def test_ansible_run_dispatches_positional_args_and_mise_tasks(self):
        stub_dir = self.root / 'bin'
        stub_dir.mkdir()
        stub = stub_dir / 'ansible-playbook'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=f'{stub_dir}:{os.environ.get("PATH", "")}')

        missing_tag = subprocess.run(
            ['bash', str(REPO / 'scripts/ansible_run.sh'), 'role'],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_tag.returncode, 2)
        self.assertIn('Usage: mise run role', missing_tag.stderr)

        role_res = subprocess.run(
            ['bash', str(REPO / 'scripts/ansible_run.sh'), 'role', 'stm32', '--check'],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(role_res.returncode, 0, role_res.stderr)
        lines = role_res.stdout.splitlines()
        self.assertIn('--tags', lines)
        idx = lines.index('--tags')
        self.assertEqual(lines[idx + 1], 'stm32')
        self.assertIn('--check', lines)

        mise_bin = subprocess.run(['which', 'mise'], capture_output=True, text=True)
        if mise_bin.returncode == 0:
            mise_env = dict(env, MISE_TRUSTED_CONFIG_PATHS=str(REPO))
            mise_role = subprocess.run(
                [mise_bin.stdout.strip(), 'run', 'role', 'stm32', '--check'],
                cwd=REPO,
                env=mise_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(mise_role.returncode, 0, mise_role.stderr)
            mise_lines = mise_role.stdout.splitlines()
            self.assertIn('--tags', mise_lines)
            self.assertEqual(mise_lines[mise_lines.index('--tags') + 1], 'stm32')
            self.assertIn('--check', mise_lines)

    def test_ansible_yaml_files_parse_cleanly(self):
        yaml_files = sorted(ANSIBLE_DIR.rglob('*.yml'))
        self.assertTrue(yaml_files)
        for path in yaml_files:
            with self.subTest(path=path.relative_to(REPO)):
                yaml.safe_load(path.read_text())

    def test_playbook_references_all_v2_roles(self):
        playbook = yaml.safe_load((ANSIBLE_DIR / 'playbook.yml').read_text())
        role_names = [entry['role'] for entry in playbook[0]['roles']]
        self.assertEqual(
            role_names,
            [
                'system',
                'snapper',
                'toolchains',
                'containers',
                'browsers',
                'desktop_apps',
                'ai_agents',
                'workstation',
                'stm32',
            ],
        )
        for role_name in role_names:
            self.assertTrue((ANSIBLE_DIR / 'roles' / role_name / 'tasks/main.yml').is_file())

    def test_ansible_yaml_line_lengths(self):
        for path in sorted(ANSIBLE_DIR.rglob('*.yml')):
            for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
                self.assertLessEqual(
                    len(line),
                    160,
                    f'{path.relative_to(REPO)}:{lineno} exceeds 160 chars ({len(line)})',
                )

    def test_ansible_notifies_match_role_handlers(self):
        def collect_notifies(node):
            found = []
            if isinstance(node, dict):
                if 'notify' in node:
                    val = node['notify']
                    if isinstance(val, str):
                        found.append(val)
                    elif isinstance(val, list):
                        found.extend(item for item in val if isinstance(item, str))
                for value in node.values():
                    found.extend(collect_notifies(value))
            elif isinstance(node, list):
                for item in node:
                    found.extend(collect_notifies(item))
            return found

        for role_dir in sorted((ANSIBLE_DIR / 'roles').iterdir()):
            if not role_dir.is_dir():
                continue
            handlers_file = role_dir / 'handlers/main.yml'
            handlers = set()
            if handlers_file.is_file():
                parsed_handlers = yaml.safe_load(handlers_file.read_text(encoding='utf-8')) or []
                for entry in parsed_handlers:
                    if isinstance(entry, dict):
                        self.assertNotIn(
                            'block',
                            entry,
                            f'{handlers_file.relative_to(REPO)} must not use block in handlers',
                        )
                        if 'name' in entry:
                            handlers.add(entry['name'])
            for task_file in sorted((role_dir / 'tasks').glob('*.yml')):
                parsed_tasks = yaml.safe_load(task_file.read_text(encoding='utf-8')) or []
                for notified in collect_notifies(parsed_tasks):
                    self.assertIn(
                        notified,
                        handlers,
                        f'{task_file.relative_to(REPO)} notifies unknown handler {notified!r}',
                    )

    def test_ansible_downloads_are_complete_and_valid(self):
        v2_downloads = yaml.safe_load((ANSIBLE_DIR / 'group_vars/all/downloads.yml').read_text())['pinned_downloads']
        expected_keys = {
            'saleae',
            'hyper_backup_explorer',
            'antigravity_cli',
            'arduino_ide',
            'cosmic_swipe',
        }
        self.assertEqual(set(v2_downloads.keys()), expected_keys)
        for key, entry in v2_downloads.items():
            with self.subTest(key=key):
                self.assertTrue(entry['version'])
                self.assertTrue(entry['url'].startswith('https://'))
                self.assertNotIn('/latest/', entry['url'])
                self.assertRegex(entry['sha256'], re.compile(r'^[0-9a-f]{64}$'))
                self.assertIn(entry['version'], entry['url'])

    def test_group_vars_local_yml_sorts_last(self):
        group_vars_dir = ANSIBLE_DIR / 'group_vars/all'
        for path in sorted(group_vars_dir.iterdir()):
            if path.is_file() and path.suffix in {'.yml', '.yaml', '.json'} and path.name != 'local.yml':
                self.assertLess(
                    path.name,
                    'local.yml',
                    f'{path.relative_to(REPO)} must sort before local.yml so local overrides win',
                )

    def test_e2e_container_test_cli_guards_and_inner_shell(self):
        script = REPO / 'scripts/e2e_container_test.sh'
        self.assertTrue(script.is_file(), f'missing {script}')
        self.assertTrue(os.access(script, os.X_OK), f'{script} must be executable')

        help_res = subprocess.run(
            ['bash', str(script), '--help'],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_res.returncode, 0, help_res.stderr)
        self.assertIn('podman run', help_res.stdout)
        self.assertIn('scripts/e2e_container_test.sh', help_res.stdout)

        bad_opt = subprocess.run(
            ['bash', str(script), '--unknown-flag'],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad_opt.returncode, 2, bad_opt.stderr)
        self.assertIn('Unknown option', bad_opt.stderr)

        in_container = Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()
        if not in_container:
            guard = subprocess.run(
                ['bash', str(script)],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            self.assertEqual(guard.returncode, 1, guard.stderr)
            self.assertIn('Refusing to run outside a disposable container', guard.stderr)
        elif os.geteuid() != 0:
            guard = subprocess.run(
                ['bash', str(script)],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            self.assertEqual(guard.returncode, 1, guard.stderr)
            self.assertIn('must start as root inside the container', guard.stderr)

        # Verify the dynamic version extraction against bootstrap.sh matches semver.
        awk_res = subprocess.run(
            [
                'bash',
                '-euo',
                'pipefail',
                '-c',
                'core=$(awk -F\'"\' \'$1 == "ANSIBLE_CORE_VERSION=" { print $2; exit }\' ./bootstrap.sh); '
                'lint=$(awk -F\'"\' \'$1 == "ANSIBLE_LINT_VERSION=" { print $2; exit }\' ./bootstrap.sh); '
                'printf "%s\\n%s\\n" "$core" "$lint"',
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(awk_res.returncode, 0, awk_res.stderr)
        lines = awk_res.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2, awk_res.stdout)
        for ver in lines:
            self.assertRegex(ver, r'^\d+\.\d+\.\d+$')

        # Validate the inner su heredoc body with bash -n and shellcheck.
        text = script.read_text()
        marker = "su - tester -s /bin/bash <<'EOF'\n"
        self.assertIn(marker, text)
        inner_and_tail = text.split(marker, 1)[1]
        self.assertTrue(inner_and_tail.endswith('\nEOF\n'))
        inner = inner_and_tail[:-len('\nEOF\n')]

        bash_n = subprocess.run(
            ['bash', '-n'],
            input=inner,
            capture_output=True,
            text=True,
        )
        self.assertEqual(bash_n.returncode, 0, bash_n.stderr)

        if shutil.which('shellcheck'):
            sc = subprocess.run(
                ['shellcheck', '-s', 'bash', '-'],
                input=inner,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sc.returncode, 0, sc.stdout + sc.stderr)

    def test_standalone_subtag_prerequisites(self):
        desktop_main = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/main.yml').read_text())
        dirs_task = next(t for t in desktop_main if t['name'] == 'Ensure workstation user directories exist')
        self.assertEqual(dirs_task['tags'], ['desktop_apps', 'desktop_gui'])

        vendor_apps = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/vendor_apps.yml').read_text())
        vendor_checks = {
            'arduino': (
                'Ensure Arduino IDE directories exist',
                ['{{ desktop_apps_arduino_versions_dir }}', '{{ workstation_local_bin }}',
                 '{{ workstation_home }}/.local/share/applications'],
            ),
            'saleae': (
                'Ensure Saleae Logic user directories exist',
                ['{{ workstation_home }}/Applications', '{{ workstation_local_bin }}',
                 '{{ workstation_home }}/.local/share/applications'],
            ),
            'hyper_backup': (
                'Ensure Hyper Backup Explorer directories exist',
                ['{{ desktop_apps_hbe_versions_dir }}', '{{ workstation_local_bin }}',
                 '{{ workstation_home }}/.local/share/applications'],
            ),
        }
        for tag, (task_name, expected_dirs) in vendor_checks.items():
            outer = next(t for t in vendor_apps if tag in t.get('tags', []))
            dir_task = next(t for t in outer['block'] if t['name'] == task_name)
            self.assertEqual(dir_task['loop'], expected_dirs)

        cosmic_swipe = yaml.safe_load((ANSIBLE_DIR / 'roles/desktop_apps/tasks/cosmic_swipe.yml').read_text())
        install_block = next(t for t in cosmic_swipe if t['name'] == 'Install cosmic-swipe touchpad gestures')
        swipe_dirs = next(t for t in install_block['block'] if t['name'] == 'Ensure cosmic-swipe directories exist')
        self.assertEqual(
            swipe_dirs['loop'],
            ['{{ desktop_apps_cosmic_swipe_versions_dir }}', '{{ workstation_local_bin }}'],
        )

        ai_agents = yaml.safe_load((ANSIBLE_DIR / 'roles/ai_agents/tasks/main.yml').read_text())
        bin_task = next(t for t in ai_agents if t['name'] == 'Ensure ~/.local/bin directory exists')
        self.assertIn('antigravity', bin_task['tags'])

    def test_live_systemd_and_udev_tasks_use_booted_fact(self):
        playbook = yaml.safe_load((ANSIBLE_DIR / 'playbook.yml').read_text())
        pre_tasks = playbook[0]['pre_tasks']
        stat_task = next(
            (
                t for t in pre_tasks
                if isinstance(t, dict)
                and t.get('ansible.builtin.stat', {}).get('path') == '/run/systemd/system'
            ),
            None,
        )
        self.assertIsNotNone(stat_task)
        self.assertEqual(stat_task.get('tags'), ['always'])
        booted_task = next(
            (
                t for t in pre_tasks
                if isinstance(t, dict)
                and 'eos_systemd_booted' in t.get('ansible.builtin.set_fact', {})
            ),
            None,
        )
        self.assertIsNotNone(booted_task)
        self.assertEqual(booted_task.get('tags'), ['always'])

        def iter_tasks(nodes):
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                yield node
                for key in ('block', 'rescue', 'always'):
                    yield from iter_tasks(node.get(key))

        for path in sorted((ANSIBLE_DIR / 'roles').rglob('*.yml')):
            text = path.read_text()
            self.assertNotIn("ansible_facts['service_mgr'] == 'systemd'", text, str(path))
            data = yaml.safe_load(text)
            for task in iter_tasks(data):
                is_systemd = 'ansible.builtin.systemd_service' in task
                cmd = task.get('ansible.builtin.command')
                argv = cmd.get('argv', []) if isinstance(cmd, dict) else []
                is_udevadm = isinstance(argv, list) and 'udevadm' in argv
                if is_systemd or is_udevadm:
                    when_val = task.get('when')
                    when_text = ' '.join(when_val) if isinstance(when_val, list) else str(when_val or '')
                    self.assertIn('eos_systemd_booted', when_text, f'{path}: {task.get("name")}')

    def test_system_pacman_upgrade_defaults_to_false(self):
        defaults = yaml.safe_load((ANSIBLE_DIR / 'group_vars/all/00-defaults.yml').read_text())
        self.assertIs(defaults['system_pacman_upgrade'], False)


if __name__ == '__main__':
    unittest.main()
