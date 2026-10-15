"""Local HTTP/media tests using synthetic camera data only.

Run: .venv/bin/python3 -m unittest test_viewer -v
FFmpeg generates local test patterns; no real UID, credentials or camera is used.
"""
from fractions import Fraction
import http.client
import json
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import bridge
import viewer


SECRET = 'SYNTHETIC_SECRET_DO_NOT_ECHO'
FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


def run_media(arguments, data=None):
    return subprocess.run(arguments, input=data, capture_output=True,
                          check=True, timeout=30).stdout


def video_fixture(codec):
    args = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=160x96:rate=25', '-frames:v', '45', '-an']
    if codec == 'h264':
        args += ['-c:v', 'libx264', '-profile:v', 'baseline', '-preset', 'ultrafast',
                 '-tune', 'zerolatency', '-x264-params',
                 'aud=1:repeat-headers=1:keyint=15:scenecut=0']
    else:
        args += ['-c:v', 'libx265', '-preset', 'ultrafast', '-x265-params',
                 'aud=1:repeat-headers=1:keyint=15:bframes=0:pools=1:frame-threads=1:log-level=error']
    raw = run_media(args + ['-f', codec, 'pipe:1'])
    starts = []
    for match in re.finditer(b'\x00\x00(?:\x00)?\x01', raw):
        byte = raw[match.end()]
        nal_type = (byte & 31) if codec == 'h264' else ((byte >> 1) & 63)
        if nal_type == (9 if codec == 'h264' else 35):
            starts.append(match.start())
    if len(starts) != 45 or starts[0] != 0:
        raise AssertionError('Synthetic encoder did not produce 45 delimited access units')
    return raw, [raw[a:b] for a, b in zip(starts, starts[1:] + [len(raw)])]


def probe_video(data, codec=None):
    args = [FFPROBE, '-v', 'error']
    if codec:
        args += ['-f', codec]
    result = run_media(args + ['-i', 'pipe:0', '-select_streams', 'v:0', '-count_frames',
                              '-show_entries',
                              'stream=codec_name,width,height,nb_read_frames,avg_frame_rate,r_frame_rate,duration',
                              '-of', 'json'], data)
    return json.loads(result)['streams'][0]


def complete_mp4(data):
    """Keep complete top-level boxes through an mdat when stopping a live stream."""
    offset = end = fragments = 0
    while len(data) - offset >= 8:
        size, kind = struct.unpack_from('!I4s', data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                break
            size, header = struct.unpack_from('!Q', data, offset + 8)[0], 16
        if size < header or offset + size > len(data):
            break
        offset += size
        if kind == b'mdat':
            end, fragments = offset, fragments + 1
    return bytes(data[:end]), fragments


class FakeCamera:
    video = None
    alaw = None

    def __init__(self, uid, username, password, stream):
        if uid == 'SYNTHETIC_FAILURE':
            raise ValueError('Private constructor exception: ' + password)
        self.mode = uid
        self.closed = threading.Event()
        self.started = threading.Event()
        self.battery, self.charging, self.audio_type, self.ptz_error = 100, True, 0, False
        self.directions = []
        self.reject_ptz = False

    def media(self, audio=True):
        assert audio is True
        self.started.set()
        if self.mode == 'SYNTHETIC_WAIT':
            self.closed.wait(20)
            return
        if self.mode == 'SYNTHETIC_MEDIA_FAILURE':
            raise ValueError('Private media exception: ' + SECRET)
        origin, frame, audio_frame = time.monotonic(), 0, 0
        while not self.closed.is_set():
            video_ms, audio_ms = round(frame * 1000 / 15), audio_frame * 100
            next_ms = min(video_ms, audio_ms)
            if self.closed.wait(max(0, origin + next_ms / 1000 - time.monotonic())):
                return
            if video_ms == next_ms:
                for index in (0, 1):
                    yield index, 123000 + video_ms, 1, self.video[index][frame % 45]
                frame += 1
            if audio_ms == next_ms:
                yield 2, 123000 + audio_ms, 0, self.alaw
                audio_frame += 1

    def request_ptz(self, direction):
        if self.reject_ptz:
            raise queue.Full('Private control exception: ' + SECRET)
        self.directions.append(direction)

    def close(self):
        self.closed.set()


@unittest.skipUnless(FFMPEG and FFPROBE, 'FFmpeg and ffprobe are required')
class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h264, h264_frames = video_fixture('h264')
        cls.hevc, hevc_frames = video_fixture('hevc')
        FakeCamera.video = h264_frames, hevc_frames
        FakeCamera.alaw = run_media([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                                    'sine=frequency=440:sample_rate=8000:duration=0.1',
                                    '-c:a', 'pcm_alaw', '-f', 'alaw', 'pipe:1'])
        cls.pcm = run_media([FFMPEG, '-v', 'error', '-f', 'alaw', '-ar', '8000',
                            '-ac', '1', '-i', 'pipe:0', '-f', 's16le', 'pipe:1'], FakeCamera.alaw)
        assert len(FakeCamera.alaw) == 800 and len(cls.pcm) == 1600

    def setUp(self):
        self.bridges, self.connections = [], []
        real_bridge = viewer.MediaBridge

        def track(camera):
            instance = real_bridge(camera)
            self.bridges.append(instance)
            return instance

        self.camera_patch = patch.object(viewer, 'Camera', FakeCamera)
        self.bridge_patch = patch.object(viewer, 'MediaBridge', side_effect=track)
        self.camera_patch.start()
        self.bridge_patch.start()
        self.addCleanup(self.camera_patch.stop)
        self.addCleanup(self.bridge_patch.stop)
        self.server = viewer.ViewerServer()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        for connection, response in self.connections:
            if response is not None:
                response.close()
            connection.close()
        self.server.server_close()
        self.server.shutdown()
        self.thread.join(2)
        self.wait_idle()
        self.assertFalse(self.thread.is_alive())
        for instance in self.bridges:
            self.assertTrue(instance.camera.closed.is_set())
            self.assertFalse(instance.worker and instance.worker.is_alive(), 'Media feed thread leaked')
            self.assertTrue(all(not reader.is_alive() for reader in instance.readers), 'MP4 reader leaked')
            for process in instance.processes:
                self.assertIsNotNone(process.poll(), 'FFmpeg process leaked')
                self.assertTrue(process.stdin.closed and process.stdout.closed, 'FFmpeg pipe leaked')

    def request(self, path='/stream', values=None, headers=None, raw=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        actual = {'Origin': 'http://' + self.server.address, 'X-Viewer-Token': viewer.TOKEN,
                  'Content-Type': 'application/json'}
        actual.update(headers or {})
        if values is None:
            values = {'uid': 'SYNTHETIC', 'username': 'test', 'password': SECRET, 'stream': '0'}
        body = raw if raw is not None else json.dumps(values).encode()
        connection.request(method, path, body if method == 'POST' else None, actual)
        entry = [connection, None]
        self.connections.append(entry)
        response = connection.getresponse()
        entry[1] = response
        return connection, response

    def wait_idle(self):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if self.server.active.acquire(False):
                self.server.active.release()
                return
            time.sleep(.02)
        self.fail('Stream lock remained held after cancellation')

    def wait_camera(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            camera = self.server.camera
            if camera is not None and camera.started.is_set():
                return camera
            time.sleep(.01)
        self.fail('Synthetic camera did not enter its media generator')

    def record(self, response):
        header = response.read(5)
        self.assertEqual(len(header), 5, 'Stream ended inside its record header')
        kind, length = struct.unpack('!BI', header)
        self.assertIn(kind, range(5))
        self.assertLessEqual(length, 2 * 1024 * 1024)
        payload = response.read(length)
        self.assertEqual(len(payload), length, 'Stream ended inside its record body')
        return kind, payload

    def test_host_origin_token_and_csp(self):
        for headers in ({'Origin': 'https://other.invalid'}, {'X-Viewer-Token': 'wrong'},
                        {'Host': 'other.invalid'}, {'Content-Type': 'text/plain'}):
            with self.subTest(headers=headers):
                _, response = self.request(headers=headers)
                self.assertEqual(response.status, 403)
                self.assertNotIn(SECRET.encode(), response.read())
        for path, headers in (('/wrong-token', {}), ('/' + viewer.TOKEN, {'Host': 'other.invalid'})):
            _, response = self.request(path, headers=headers, method='GET')
            self.assertEqual(response.status, 404)
            response.read()
        _, response = self.request('/' + viewer.TOKEN, method='GET')
        self.assertEqual(response.status, 200)
        csp = response.getheader('Content-Security-Policy')
        for directive in ("default-src 'none'", "script-src 'nonce-" + viewer.TOKEN + "'",
                          "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(directive, csp)
        self.assertNotIn('unsafe-inline', csp)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertIn(b'nonce="' + viewer.TOKEN.encode(), response.read())
        self.assertEqual(self.bridges, [])

    def test_invalid_input_and_private_exceptions_are_not_exposed(self):
        valid = {'uid': 'SYNTHETIC', 'username': 'test', 'password': SECRET, 'stream': '0'}
        cases = [[], {}, dict(valid, password=''), dict(valid, username=[]),
                 dict(valid, stream=True), dict(valid, uid='x' * 129)]
        for values in cases:
            with self.subTest(values=type(values).__name__):
                _, response = self.request(values=values)
                self.assertEqual(response.status, 400)
                self.assertNotIn(SECRET.encode(), response.read())
                self.wait_idle()
        for raw in (b'not-json ' + SECRET.encode(), b'"' + SECRET.encode() + b'"'):
            _, response = self.request(raw=raw)
            self.assertEqual(response.status, 400)
            self.assertNotIn(SECRET.encode(), response.read())
        _, response = self.request(values=dict(valid, uid='SYNTHETIC_FAILURE'))
        self.assertEqual(response.status, 502)
        self.assertNotIn(SECRET.encode(), response.read())
        self.wait_idle()
        _, response = self.request(values=dict(valid, uid='SYNTHETIC_MEDIA_FAILURE'))
        self.assertEqual(response.status, 200)
        kind, payload = self.record(response)
        self.assertEqual(kind, 4)
        self.assertNotIn(SECRET.encode(), payload)
        self.assertNotIn(b'Private', payload)

    def test_request_body_bounds_and_incomplete_body(self):
        for headers in ({'Content-Length': '4097'}, {'Content-Length': '-1'},
                        {'Transfer-Encoding': 'chunked'}):
            _, response = self.request(raw=b'{}', headers=headers)
            self.assertEqual(response.status, 400)
            response.read()
            self.wait_idle()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.connections.append([connection, None])
        connection.request('POST', '/stream', b'{}', {
            'Origin': 'http://' + self.server.address, 'X-Viewer-Token': viewer.TOKEN,
            'Content-Type': 'application/json', 'Content-Length': '20'})
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        self.connections[-1][1] = response
        self.assertEqual(response.status, 400)
        response.read()

    def test_concurrent_stream_rejected_and_cancel_before_first_frame(self):
        values = {'uid': 'SYNTHETIC_WAIT', 'username': 'test', 'password': SECRET, 'stream': 0}
        connection, response = self.request(values=values)
        self.assertEqual(response.status, 200)
        camera = self.wait_camera()
        _, second = self.request()
        self.assertEqual(second.status, 409)
        second.read()
        response.close()
        connection.close()
        self.wait_idle()
        self.assertTrue(camera.closed.is_set())
        self.assertEqual(self.bridges[0].processes, [])

    def test_ptz_accept_reject_and_no_camera(self):
        _, response = self.request('/ptz', {'direction': 'left'})
        self.assertEqual(response.status, 409)
        response.read()
        self.request(values={'uid': 'SYNTHETIC_WAIT', 'username': 'test', 'password': SECRET, 'stream': 0})
        camera = self.wait_camera()
        for direction in ('up', 'down', 'left', 'right', 'stop'):
            _, response = self.request('/ptz', {'direction': direction})
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'OK')
        self.assertEqual(camera.directions, ['up', 'down', 'left', 'right', 'stop'])
        for values in ({'direction': 'diagonal'}, {'direction': []}, {}):
            _, response = self.request('/ptz', values)
            self.assertEqual(response.status, 409)
            response.read()
        camera.reject_ptz = True
        _, response = self.request('/ptz', {'direction': 'up'})
        self.assertEqual(response.status, 409)
        self.assertNotIn(SECRET.encode(), response.read())
        _, response = self.request('/ptz', {'direction': 'up'}, headers={'X-Viewer-Token': 'bad'})
        self.assertEqual(response.status, 403)
        response.read()

    def test_generated_25fps_input_is_timed_by_15fps_camera_pts(self):
        rate = bridge.frame_rate(round(index * 1000 / 15) for index in range(8))
        for codec, raw in (('h264', self.h264), ('hevc', self.hevc)):
            with self.subTest(codec=codec):
                source = probe_video(raw, codec)
                # Raw H264 can expose twice the frame rate in r_frame_rate.
                self.assertEqual(Fraction(source['avg_frame_rate']), 25)
                converted = run_media(bridge.ffmpeg_command(codec, rate), raw)
                self.assertIn(b'avcC', converted)
                stream = probe_video(converted)
                self.assertEqual((stream['codec_name'], stream['width'], stream['height']), ('h264', 160, 96))
                self.assertEqual(int(stream['nb_read_frames']), 45)
                self.assertAlmostEqual(float(Fraction(stream['avg_frame_rate'])), float(rate), delta=.03)
                self.assertAlmostEqual(float(stream['duration']), 45 / float(rate), delta=.08)

    def test_two_lenses_pcm_metadata_and_process_cleanup(self):
        connection, response = self.request()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'application/octet-stream')
        video, audio, metadata = [bytearray(), bytearray()], [], []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            kind, payload = self.record(response)
            if kind < 2:
                video[kind].extend(payload)
            elif kind == 2:
                self.assertEqual(len(payload), 1604)
                timestamp = struct.unpack_from('<I', payload)[0]
                self.assertEqual(payload[4:], self.pcm)
                audio.append(timestamp)
            elif kind == 3:
                metadata.append(json.loads(payload))
            else:
                self.fail('Synthetic stream failed: ' + payload.decode())
            if audio and audio[-1] >= 2300 and all(complete_mp4(data)[1] >= 2 for data in video):
                break
        response.close()
        connection.close()
        self.wait_idle()
        self.assertGreaterEqual(len(audio), 24)
        self.assertEqual(audio, list(range(0, audio[-1] + 1, 100)))
        self.assertIn({'audio': True, 'sampleRate': 8000}, metadata)
        self.assertIn({'battery': 100, 'charging': True, 'ptzError': False}, metadata)
        for index in (0, 1):
            details = next(item for item in metadata if item.get('video') == index)
            self.assertRegex(details['codec'], r'^avc1\.[0-9a-f]{6}$')
            self.assertAlmostEqual(float(Fraction(details['fps'])), 15, delta=.03)
            data, fragments = complete_mp4(video[index])
            self.assertGreaterEqual(fragments, 2)
            stream = probe_video(data)
            frames = int(stream['nb_read_frames'])
            rate = float(Fraction(details['fps']))
            self.assertGreaterEqual(frames, 15)
            self.assertEqual(stream['codec_name'], 'h264')
            self.assertAlmostEqual(float(Fraction(stream['avg_frame_rate'])), rate, delta=.03)
            self.assertAlmostEqual(float(stream['duration']), frames / rate, delta=.08)
        self.assertEqual(len(self.bridges[0].processes), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
