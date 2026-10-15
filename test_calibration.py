"""Offline timing checks: synthetic PTS and synthetic HEVC only."""
import json
import shutil
import subprocess
import unittest

from calibration import frame_rate


class CalibrationTest(unittest.TestCase):
    def test_both_lenses_use_the_same_clock_despite_ms_rounding(self):
        for pts in ([10646,10713,10779,10846,10913,10979,11046,11113],
                    [10680,10746,10813,10880,10946,11013,11080,11146]):
            self.assertEqual(frame_rate(pts), 15)

    def test_millisecond_quantization(self):
        for fps in (6, 10, 15, 20, 25, 30, 60):
            pts = [round(1000 * n / fps) for n in range(8)]
            self.assertAlmostEqual(float(frame_rate(pts)), fps, delta=fps * .005)

    def test_uint32_wrap(self):
        pts = [(0xfffffff0 + n * 50) & 0xffffffff for n in range(8)]
        self.assertEqual(frame_rate(pts), 20)

    def test_duplicate_and_isolated_gaps(self):
        self.assertEqual(frame_rate([0, 800, 850, 900, 900, 1000, 1050, 1100]), 20)

    def test_clock_reset(self):
        self.assertEqual(frame_rate([5000, 5050, 5100, 0, 50, 100, 150, 200]), 20)

    def test_fractional_rate_is_not_rounded_to_integer(self):
        self.assertEqual(str(frame_rate([n * 400 for n in range(8)])), '5/2')

    def test_invalid_timing_is_not_given_default_rate(self):
        for pts in ([], [0, 50], [0] * 8, [0, 5000, 10000, 15000], [0, -1, 50, 100]):
            with self.assertRaises(ValueError):
                frame_rate(pts)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
    def test_input_rate_overrides_conflicting_hevc_timing(self):
        ff = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        # The bitstream says 25 FPS, but camera timestamps in this test say 15.
        source = subprocess.run(ff + [
            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25', '-frames:v', '45',
            '-c:v', 'libx265', '-preset', 'ultrafast', '-x265-params',
            'log-level=error:bframes=0:repeat-headers=1', '-f', 'hevc', 'pipe:1'],
            capture_output=True, check=True, timeout=20).stdout
        for option, expected_duration in [(['-framerate', '15'], 1.8), (['-r', '15'], 3.0)]:
            movie = subprocess.run(ff + ['-fflags', '+genpts'] + option + [
                '-f', 'hevc', '-i', 'pipe:0', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-tune', 'zerolatency', '-pix_fmt', 'yuv420p', '-movflags',
                'empty_moov+default_base_moof+frag_keyframe', '-f', 'mp4', 'pipe:1'],
                input=source, capture_output=True, check=True, timeout=20).stdout
            result = subprocess.run([
                'ffprobe', '-v', 'error', '-count_frames', '-show_entries',
                'stream=avg_frame_rate,nb_read_frames,duration', '-of', 'json', 'pipe:0'],
                input=movie, capture_output=True, check=True, timeout=20)
            stream = json.loads(result.stdout)['streams'][0]
            self.assertEqual(int(stream['nb_read_frames']), 45)
            self.assertAlmostEqual(float(stream['duration']), expected_duration, places=4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
