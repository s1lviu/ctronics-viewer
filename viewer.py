"""One local process: HiChip P2P -> FFmpeg -> browser, no saved credentials."""
import json
import http.client
from pathlib import Path
import select
import secrets
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hichip import Camera
from bridge import MediaBridge, ffmpeg_path, ffmpeg_command

TOKEN = secrets.token_urlsafe(24)
PAGE = Path(__file__).with_name('index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN).encode()


class ViewerError(Exception):
    """Only deliberately public messages may reach the browser."""


class ViewerServer(ThreadingHTTPServer):
    def __init__(self, address=('127.0.0.1', 0)):
        self.active = threading.Lock()
        self.cancel_stream = None
        self.camera = None
        super().__init__(address, Handler)
        self.address = '127.0.0.1:' + str(self.server_port)

    def server_close(self):
        if self.cancel_stream:
            self.cancel_stream()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond_headers(self, status, kind, length=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'nonce-" + TOKEN +
                         "'; style-src 'nonce-" + TOKEN +
                         "'; connect-src 'self'; media-src blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header('Connection', 'close')
        self.close_connection = True
        if length is not None:
            self.send_header('Content-Length', str(length))

    def error(self, status, message):
        body = message.encode()
        self.respond_headers(status, 'text/plain; charset=utf-8', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != '/' + TOKEN or self.headers_in_host() is False:
            return self.error(404, 'Page not found.')
        self.respond_headers(200, 'text/html; charset=utf-8', len(PAGE))
        self.end_headers()
        self.wfile.write(PAGE)

    def headers_in_host(self):
        return self.headers.get('Host') == self.server.address

    def read_json(self):
        self.connection.settimeout(10)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 1 <= size <= 4096:
                raise ValueError()
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError()
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError()
            return values
        except (ValueError, TypeError, socket.timeout):
            raise ViewerError('Invalid or incomplete request.') from None

    def do_POST(self):
        if (self.path not in ('/stream', '/ptz') or not self.headers_in_host()
                or self.headers.get('X-Viewer-Token') != TOKEN
                or self.headers.get('Origin') != 'http://' + self.server.address
                or self.headers.get('Content-Type') != 'application/json'):
            return self.error(403, 'Request denied.')
        if self.path == '/ptz':
            try:
                direction = self.read_json().get('direction')
                if direction not in ('up', 'down', 'left', 'right', 'stop') or self.server.camera is None:
                    raise ViewerError('Camera control is unavailable.')
                self.server.camera.request_ptz(direction)
                return self.error(200, 'OK')
            except (ViewerError, ValueError, queue.Full):
                return self.error(409, 'Camera control is unavailable.')
        if not self.server.active.acquire(blocking=False):
            return self.error(409, 'The camera is already open in another tab.')
        bridge = watcher = None
        cancelled = threading.Event()
        sent = False

        def cancel():
            cancelled.set()
            if bridge:
                bridge.close()

        def watch_connection():
            while not cancelled.wait(.2):
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        cancel()
                        return
                except OSError:
                    cancel()
                    return

        self.server.cancel_stream = cancel
        try:
            values = self.read_json()
            if (any(not isinstance(values.get(key), str) or not 1 <= len(values[key]) <= limit
                    for key, limit in [('uid', 128), ('username', 128), ('password', 256)]) or
                    str(values.get('stream')) not in ('0', '1')):
                raise ViewerError('Invalid or incomplete connection details.')
            camera = Camera(values['uid'].strip(), values['username'], values['password'], int(values['stream']))
            values.clear()
            self.server.camera = camera
            bridge = MediaBridge(camera)
            watcher = threading.Thread(target=watch_connection, daemon=True)
            watcher.start()
            self.respond_headers(200, 'application/octet-stream')
            self.end_headers()
            sent = True
            for record in bridge.records():
                self.wfile.write(record)
        except Exception as exc:
            if not sent and not cancelled.is_set():
                try:
                    self.error(400 if isinstance(exc, ViewerError) else 502,
                               str(exc) if isinstance(exc, ViewerError) else 'Could not connect to the camera.')
                except OSError:
                    pass
        finally:
            cancel()
            if watcher:
                watcher.join(timeout=1)
            self.server.camera = None
            self.server.cancel_stream = None
            self.server.active.release()


def check_installation(fixtures=None):
    from Crypto.Cipher import AES
    assert AES.new(bytes(16), AES.MODE_ECB).encrypt(bytes(16)).hex() == '66e94bd4ef8a2c3b884cfa59ca342b2e'
    for option, required in [('-encoders', ['libx264']), ('-decoders', ['h264', 'hevc'])]:
        output = subprocess.check_output([ffmpeg_path(), '-hide_banner', option], stderr=subprocess.STDOUT).decode()
        assert all(name in output for name in required), 'Required video codec is missing'
    if fixtures is not None:
        with tempfile.TemporaryDirectory(prefix='viewer media check ') as temporary:
            for codec in ('h264', 'hevc'):
                movie = Path(temporary)/(codec+'.mp4')
                result = subprocess.run(ffmpeg_command(codec, 15),
                                        input=(fixtures/('video.'+codec)).read_bytes(),
                                        capture_output=True, check=True, timeout=30)
                movie.write_bytes(result.stdout)
                result = subprocess.run([ffmpeg_path(), '-hide_banner', '-loglevel', 'error', '-xerror',
                                         '-i', str(movie), '-map', '0:v:0', '-progress', 'pipe:1',
                                         '-f', 'null', '-'], capture_output=True, check=True, timeout=30)
                frames = [int(line.split('=')[1]) for line in result.stdout.decode().splitlines()
                          if line.startswith('frame=')]
                assert frames[-1] == 45, (codec, frames)
                print('Bundled video round trip passed:', codec, flush=True)
    server = ViewerServer()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request('GET', '/' + TOKEN)
        response = connection.getresponse()
        assert response.status == 200 and response.read().count(b'<video ') == 2
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
    print('Installation check passed.', flush=True)


def main():
    if not ffmpeg_path():
        raise SystemExit('FFmpeg is missing. Install FFmpeg and restart the viewer.')
    if sys.argv[1:2] == ['--self-test']:
        if len(sys.argv) > 3:
            raise SystemExit('Usage: CtronicsViewer --self-test [fixture-directory]')
        check_installation(Path(sys.argv[2]) if len(sys.argv) == 3 else None)
        return
    server = ViewerServer()
    url = 'http://' + server.address + '/' + TOKEN
    print('Open in your browser: ' + url, flush=True)
    print('Press Ctrl+C to stop the viewer. Passwords are not saved.', flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
