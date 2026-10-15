"""Two video streams and PCM audio over one bounded local HTTP response."""
import itertools
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import threading
import time

from calibration import frame_rate
from hichip_audio import alaw_pcm16, elementary_audio


def video_format(frame):
    for nal in frame.replace(b'\0\0\0\1', b'\0\0\1').split(b'\0\0\1')[1:]:
        if nal and nal[0] & 31 == 7:
            return 'h264'
        if len(nal) > 1 and (nal[0] >> 1) & 63 == 33:
            return 'hevc'
    raise ValueError('Missing video parameters.')


def ffmpeg_path():
    bundled = Path(__file__).with_name('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    return str(bundled) if bundled.is_file() else shutil.which('ffmpeg')


def ffmpeg_command(codec, rate):
    executable = ffmpeg_path()
    if not executable:
        raise FileNotFoundError('FFmpeg is unavailable.')
    command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-fflags', '+genpts', '-probesize', '65536', '-analyzeduration', '500000',
               '-r', str(rate), '-f', codec, '-i', 'pipe:0', '-an']
    if codec == 'h264':
        command += ['-c:v', 'copy']
    else:
        command += ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                    '-pix_fmt', 'yuv420p', '-g', '30']
    return command + ['-movflags', 'empty_moov+default_base_moof+frag_keyframe',
                      '-frag_duration', '500000', '-f', 'mp4', 'pipe:1']


class MediaBridge:
    def __init__(self, camera):
        self.camera = camera
        self.cancelled = threading.Event()
        self.events = queue.Queue(maxsize=64)
        self.processes = []
        self.readers = []
        self.worker = None
        self.failure = None

    def emit(self, kind, payload):
        while not self.cancelled.is_set():
            try:
                self.events.put((kind, payload), timeout=.2)
                return
            except queue.Full:
                continue

    def metadata(self, value):
        self.emit(3, json.dumps(value).encode())

    def close(self):
        self.cancelled.set()
        self.camera.close()
        for process in list(self.processes):
            try:
                process.terminate()
            except OSError:
                pass

    def read_video(self, index, process, rate):
        try:
            initial = bytearray()
            while not self.cancelled.is_set():
                pos = initial.find(b'avcC')
                if pos >= 0 and len(initial) >= pos+8:
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk or len(initial)+len(chunk) > 262144:
                    raise ValueError('Missing MP4 parameters.')
                initial.extend(chunk)
            if self.cancelled.is_set():
                return
            self.metadata({'video': index, 'codec': 'avc1.'+initial[pos+5:pos+8].hex(),
                           'fps': str(rate)})
            self.emit(index, bytes(initial))
            while not self.cancelled.is_set():
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.emit(index, chunk)
        except Exception:
            if not self.cancelled.is_set():
                self.failure = 'Video processing stopped. Reconnect the camera.'
                self.camera.close()

    def feed(self):
        try:
            media = iter(self.camera.media(audio=True))
            saved, stamps, first = [], [[], []], [None, None]
            size = 0
            started = time.monotonic()
            for item in media:
                index, pts, kind, data = item
                saved.append(item)
                size += len(data)
                if index < 2:
                    stamps[index].append(pts)
                    if first[index] is None:
                        first[index] = data
                if all(len(points) >= 8 for points in stamps):
                    break
                if size > 8*1024*1024 or time.monotonic()-started > 30:
                    raise ValueError('Both lenses did not start.')
            if not all(len(points) >= 8 for points in stamps):
                raise ValueError('Incomplete video stream.')
            base = stamps[0][0]
            # shortcut: fixed initial FPS; use timestamped muxing if firmware changes rate mid-session.
            for index in (0, 1):
                rate = frame_rate(stamps[index])
                process = subprocess.Popen(ffmpeg_command(video_format(first[index]), rate),
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
                self.processes.append(process)
                reader = threading.Thread(target=self.read_video, args=(index, process, rate), daemon=True)
                self.readers.append(reader)
                reader.start()
            self.metadata({'audio': self.camera.audio_type == 0, 'sampleRate': 8000})
            last_status = None
            for index, pts, kind, data in itertools.chain(saved, media):
                if self.cancelled.is_set():
                    break
                if self.failure:
                    raise ValueError(self.failure)
                if index < 2:
                    self.processes[index].stdin.write(data)
                    self.processes[index].stdin.flush()
                elif self.camera.audio_type == 0:
                    relative = (pts-base) & 0xffffffff
                    if relative < 0x80000000:
                        self.emit(2, struct.pack('<I', relative)+alaw_pcm16(elementary_audio(data, 0)))
                status = self.camera.battery, self.camera.charging, self.camera.ptz_error
                if status != last_status:
                    self.metadata({'battery': status[0], 'charging': status[1], 'ptzError': status[2]})
                    last_status = status
        except Exception:
            if not self.cancelled.is_set():
                self.emit(4, (self.failure or 'The video connection stopped. Check the camera and reconnect.').encode())
        finally:
            for process in self.processes:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                except OSError:
                    pass
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            for reader in self.readers:
                reader.join(timeout=2)
            for process in self.processes:
                process.stdout.close()
            self.camera.close()
            self.emit(None, b'')

    def records(self):
        self.worker = threading.Thread(target=self.feed, daemon=True)
        self.worker.start()
        try:
            while not self.cancelled.is_set():
                try:
                    kind, data = self.events.get(timeout=.2)
                except queue.Empty:
                    continue
                if kind is None:
                    return
                yield struct.pack('!BI', kind, len(data))+data
        finally:
            self.close()
            self.worker.join(timeout=5)
