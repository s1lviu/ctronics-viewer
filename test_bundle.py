"""Bundled executable lookup without downloads or camera access."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge


class BundleTests(unittest.TestCase):
    def test_bundled_ffmpeg_wins_over_path(self):
        with tempfile.TemporaryDirectory(prefix='viewer bundle ') as folder:
            binary = Path(folder)/('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
            binary.touch()
            with patch.object(bridge, '__file__', str(Path(folder)/'bridge.py')), \
                 patch.object(bridge.shutil, 'which', return_value='system-ffmpeg') as which:
                self.assertEqual(bridge.ffmpeg_path(), str(binary))
                self.assertEqual(bridge.ffmpeg_command('h264', 15)[0], str(binary))
                which.assert_not_called()

    def test_source_install_uses_path(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(bridge, '__file__', str(Path(folder)/'bridge.py')), \
             patch.object(bridge.shutil, 'which', return_value='system-ffmpeg'):
            self.assertEqual(bridge.ffmpeg_path(), 'system-ffmpeg')

    def test_missing_ffmpeg_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(bridge, '__file__', str(Path(folder)/'bridge.py')), \
             patch.object(bridge.shutil, 'which', return_value=None):
            with self.assertRaises(FileNotFoundError):
                bridge.ffmpeg_command('hevc', 15)
