"""Unit tests for scripts/aur_install.py (mocked pacman/yay, never runs real installs)."""
import importlib
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
aur_install = importlib.import_module('aur_install')


class AurInstallTests(unittest.TestCase):
    def test_validate_packages_rejects_unsafe_names_and_deduplicates(self):
        self.assertEqual(
            aur_install.validate_packages(['google-chrome', 'visual-studio-code-bin', 'google-chrome', 'foo+bar@1']),
            ['google-chrome', 'visual-studio-code-bin', 'foo+bar@1'],
        )
        for bad in ('', '-leading-dash', 'foo;id', 'foo bar', '../evil', 'UpperCase'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                aur_install.validate_packages([bad])

    def test_find_missing_preserves_order_and_uses_pacman_deptest(self):
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 2, stdout='litecli\navra\n', stderr='')

        missing = aur_install.find_missing(['google-chrome', 'avra', 'litecli', 'avra'], run=fake_run)
        self.assertEqual(missing, ['avra', 'litecli'])
        self.assertEqual(calls, [['pacman', '-T', 'google-chrome', 'avra', 'litecli']])

    def test_find_missing_raises_on_pacman_error(self):
        def fake_run(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='pacman error')

        with self.assertRaises(subprocess.CalledProcessError):
            aur_install.find_missing(['avra'], run=fake_run)

    def test_install_refuses_root_and_missing_helper(self):
        with self.assertRaises(PermissionError):
            aur_install.install_packages(['avra'], geteuid=lambda: 0)
        with self.assertRaises(FileNotFoundError):
            aur_install.install_packages(['avra'], helper='yay', which=lambda _: None, geteuid=lambda: 1000)

    def test_install_skips_helper_when_all_packages_already_present(self):
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        installed = aur_install.install_packages(
            ['avra', 'litecli'],
            helper='yay',
            run=fake_run,
            which=lambda name: f'/usr/bin/{name}',
            geteuid=lambda: 1000,
        )
        self.assertEqual(installed, [])
        self.assertEqual(calls, [['pacman', '-T', 'avra', 'litecli']])

    def test_install_runs_yay_noninteractively_and_verifies_result(self):
        installed_set = {'git'}
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[:2] == ['pacman', '-T']:
                missing = [pkg for pkg in cmd[2:] if pkg not in installed_set]
                return subprocess.CompletedProcess(
                    cmd, len(missing), stdout=(''.join(f'{pkg}\n' for pkg in missing)), stderr='',
                )
            installed_set.update(['avra', 'litecli'])
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        installed = aur_install.install_packages(
            ['git', 'avra', 'litecli'],
            helper='yay',
            run=fake_run,
            which=lambda name: f'/usr/bin/{name}',
            geteuid=lambda: 1000,
        )
        self.assertEqual(installed, ['avra', 'litecli'])
        self.assertEqual(calls[1], [
            '/usr/bin/yay', '-S', '--needed', '--noconfirm',
            '--answerdiff', 'None', '--answerclean', 'None', '--nosudoloop',
            '--sudo', '/usr/bin/sudo', '--sudoflags', '-n',
            'avra', 'litecli',
        ])
        self.assertEqual(calls[2], ['pacman', '-T', 'avra', 'litecli'])

    def test_install_raises_when_package_still_missing_after_helper(self):
        def fake_run(cmd, **_kwargs):
            if cmd[:2] == ['pacman', '-T']:
                return subprocess.CompletedProcess(cmd, 1, stdout='avra\n', stderr='')
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with self.assertRaisesRegex(RuntimeError, 'Packages missing after yay install: avra'):
            aur_install.install_packages(
                ['avra'],
                helper='yay',
                run=fake_run,
                which=lambda name: f'/usr/bin/{name}',
                geteuid=lambda: 1000,
            )


if __name__ == '__main__':
    unittest.main()
