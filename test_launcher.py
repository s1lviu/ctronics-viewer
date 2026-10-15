"""Offline launcher checks: no installs, camera access, or viewer launch."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import start as launcher


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctronics-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'requirements.txt').write_text('pycryptodome==3.23.0\n')

    def make_python(self, folder):
        path = folder / ('Scripts/python.exe' if launcher.sys.platform == 'win32' else 'bin/python')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_existing_environment_needs_no_network_install(self):
        python = self.make_python(self.base / '.venv')
        with mock.patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(launcher.prepare(self.base), python)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], 'pycryptodome==3.23.0')
            self.assertNotIn('pip', run.call_args.args[0])

    def test_new_environment_installs_only_locally_with_pinned_requirements(self):
        builder = mock.Mock()
        builder.create.side_effect = self.make_python
        with mock.patch.object(launcher.venv, 'EnvBuilder', return_value=builder) as factory, \
             mock.patch.object(launcher.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 0)]) as run:
            python = launcher.prepare(self.base)
            factory.assert_called_once_with(with_pip=True)
            builder.create.assert_called_once_with(self.base / '.venv')
            install = run.call_args_list[1]
            self.assertEqual(install.args[0], [str(python), '-I', '-m', 'pip', '--isolated', 'install',
                             '--disable-pip-version-check', '--no-cache-dir', '-r', str(self.base / 'requirements.txt')])
            self.assertTrue(install.kwargs['check'])

    def test_missing_ffmpeg_stops_before_creating_environment(self):
        with mock.patch.object(launcher.shutil, 'which', return_value=None), \
             mock.patch.object(launcher, 'prepare') as prepare:
            self.assertEqual(launcher.main(), 1)
            prepare.assert_not_called()

    def test_old_python_stops_before_creating_environment(self):
        with mock.patch.object(launcher.sys, 'version_info', (3, 10, 0)), \
             mock.patch.object(launcher, 'prepare') as prepare:
            self.assertEqual(launcher.main(), 1)
            prepare.assert_not_called()

    def test_windows_environment_path(self):
        with mock.patch.object(launcher.sys, 'platform', 'win32'):
            python = self.make_python(self.base / '.venv')
            with mock.patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(launcher.prepare(self.base), python)
                self.assertTrue(str(python).endswith('Scripts/python.exe'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
