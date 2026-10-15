"""HiChip P2P transport for supported SSAS cameras."""
import base64
import re
import queue
import socket
import struct
import threading
import time

from hichip_crypto import LoginCrypto

SERVERS = {(ip, 32100) for ip in ('47.93.251.13', '8.218.151.94', '47.251.70.188')}
WAKE_SERVERS = ('47.52.116.220', '39.105.189.23', '47.106.188.83')
MAX_FRAME = 4 * 1024 * 1024


def packet(kind, payload=b''):
    return struct.pack('!BBH', 0xf1, kind, len(payload)) + payload


def identity(uid):
    if not isinstance(uid, str) or not re.fullmatch(r'SSAS-[0-9]{6}-[A-Z]{1,8}', uid):
        raise ValueError('Enter the SSAS UID exactly as printed on the camera.')
    prefix, number, suffix = uid.split('-')
    return struct.pack('!8sI8s', prefix.encode(), int(number), suffix.encode())


def address(endpoint):
    return b'\x00\x02' + struct.pack('<H', endpoint[1]) + socket.inet_aton(endpoint[0])[::-1] + bytes(8)


def read_address(data, offset=0):
    if len(data) < offset+16 or data[offset:offset+2] != b'\x00\x02':
        raise ValueError('Invalid P2P address.')
    port = struct.unpack_from('<H', data, offset+2)[0]
    if not port:
        raise ValueError('Invalid P2P port.')
    return socket.inet_ntoa(data[offset+4:offset+8][::-1]), port


def wake_packet(uid, now_ns=None):
    ident = identity(uid)
    value = 0
    for char in uid.encode():
        value = (value+char) & 0xffffffff
        value = (value+(value << 10)) & 0xffffffff
        value ^= value >> 6
    value = (value+(value << 3)) & 0xffffffff
    value ^= value >> 11
    value = (value+(value << 15)) & 0xffffffff
    seconds, ns = divmod(time.time_ns() if now_ns is None else now_ns, 1_000_000_000)
    usec, ticks = ns//1000, (seconds*10000) & 0xffffffff
    signed = ticks if ticks < 0x80000000 else ticks-0x100000000
    token = base64.b64encode(f'{uid}:{signed}:{usec}'.encode())[13:44].ljust(32, b'\0')
    return struct.pack('<HHI', 0x0b20, 0x4000, 0x20160831) + ident + struct.pack('<III', value, (ticks+usec)&0xffffffff, usec) + token


class Camera:
    def __init__(self, uid, username, password, stream=0, lens=0):
        self.ident = identity(uid)
        if stream not in (0, 1):
            raise ValueError('Invalid video stream.')
        if lens not in (0, 1):
            raise ValueError('Invalid lens.')
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise ValueError('Enter the username and password.')
        try:
            self.crypto = LoginCrypto(uid, username, password)
        except (ValueError, UnicodeError):
            raise ValueError('Credentials exceed the client limits: ASCII username up to 15 characters and ASCII password up to 256.') from None
        self.uid, self.stream, self.lens = uid, stream, lens
        self.closed = threading.Event()
        self.sock = self.peer = None
        self.received = {0: bytearray(), 2: bytearray()}
        self.reorder = {0: {}, 2: {}}
        self.next_in = {0: 0, 2: 0}
        self.next_out = 0
        self.pending = {}
        self.next_retry = self.next_alive = 0
        self.mode = None
        self.streaming = self.dual = False
        self.battery = self.charging = self.audio_type = None
        self.next_battery = 0
        self.controls = queue.Queue(maxsize=16)
        self.ptz_deadline = 0
        self.ptz_error = False

    def close(self):
        self.closed.set()
        sock, self.sock = self.sock, None
        if sock:
            try:
                if self.peer:
                    if self.streaming:
                        stop = self.crypto.command(0x1002b, struct.pack('<IIIHH', 0, 0, 0, 58, 0))
                        sock.sendto(packet(0xd0, struct.pack('!BBH', 0xd1, 0, self.next_out)+stop), self.peer)
                    sock.sendto(packet(0xf0), self.peer)
            except OSError:
                pass
            finally:
                sock.close()

    def send(self, kind, body=b'', target=None):
        sock = self.sock
        if self.closed.is_set() or sock is None:
            raise OSError('Connection closed.')
        sock.sendto(packet(kind, body), target or self.peer)

    def receive(self):
        sock = self.sock
        if self.closed.is_set() or sock is None:
            raise OSError('Connection closed.')
        try:
            data, source = sock.recvfrom(65535)
        except socket.timeout:
            return None
        if len(data) < 4 or data[0] != 0xf1 or len(data) != 4+int.from_bytes(data[2:4], 'big'):
            return None
        return data[1], data[4:], source

    def connect(self, timeout=40):
        # The UI overrides the prefix's push server for this 4G camera family.
        for server in WAKE_SERVERS:
            if self.closed.is_set():
                break
            try:
                with socket.create_connection((server, 20019), timeout=2) as wake:
                    wake.sendall(wake_packet(self.uid))
                    self.closed.wait(1.5)
                break
            except OSError:
                continue
        if self.closed.is_set():
            raise OSError('Connection closed.')
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('', 0)); self.sock.settimeout(.1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(next(iter(SERVERS)))
            local = route.getsockname()[0], self.sock.getsockname()[1]
        candidates, relays, relay_peers, retries = set(), set(), set(), {}
        selected_relay = None
        started, next_send = time.monotonic(), 0
        while time.monotonic()-started < timeout:
            now = time.monotonic()
            if now >= next_send:
                for server in SERVERS:
                    self.send(0, target=server)
                    self.send(0x20, self.ident+address(local), server)
                    if now-started > 5 and not relay_peers:
                        self.send(0x67, self.ident, server)
                for peer in candidates:
                    self.send(0x41, self.ident, peer)
                for target, (kind, body) in list(retries.items()):
                    self.send(kind, body, target)
                next_send = now+.5
            incoming = self.receive()
            if not incoming:
                continue
            kind, body, source = incoming
            try:
                if source in SERVERS:
                    if kind == 0x40 and len(body) == 16:
                        target = read_address(body); candidates.add(target)
                        self.send(0x41, self.ident, target)
                    elif kind == 0x69 and len(body) >= 4 and body[0] <= 16 and len(body) == 4+16*body[0]:
                        endpoints = [read_address(body, 4+16*i) for i in range(body[0])]
                        for endpoint in endpoints:
                            if endpoint not in relays:
                                relays.add(endpoint); retries[endpoint] = 0x70, b''
                    elif kind == 0x82 and len(body) == 20 and selected_relay and body[16:20] == selected_relay[1]:
                        target = read_address(body); relay_peers.add(target)
                        for _ in range(3):
                            self.send(0x83, body[16:20]+self.ident+bytes(4), target)
                            self.closed.wait(.01)
                if source in relays:
                    if kind == 0x71 and not body and retries.get(source) == (0x70, b''):
                        retries[source] = 0x72, b''
                    elif kind == 0x73 and len(body) == 8 and (selected_relay is None or time.monotonic()-selected_relay[2] > 2):
                        target = source[0], int.from_bytes(body[4:6], 'big')
                        if not target[1]:
                            continue
                        selected_relay = target, body[:4], time.monotonic()
                        retries.pop(source, None)
                        for peer in relay_peers:
                            retries.pop(peer, None)
                        # A previously offered relay can finish after the next allocation.
                        for server in SERVERS:
                            self.send(0x80, self.ident+address(target)+body[:4], server)
                ready = source in candidates and kind in (0x41, 0x42)
                ready |= any(source[0] == peer[0] for peer in relay_peers) and kind == 0x84
                if ready and body == self.ident:
                    self.peer, self.mode = source, 'relay' if kind == 0x84 else 'direct'
                    if kind == 0x41:
                        self.send(0x42, self.ident)
                    # Low-power sessions wait for the peer's alive handshake.
                    next_alive = 0
                    while time.monotonic()-started < timeout:
                        if time.monotonic() >= next_alive:
                            self.send(0xe0); next_alive = time.monotonic()+.5
                        alive = self.receive()
                        if alive and alive[2] == self.peer and len(alive[1]) in (0, 8):
                            if alive[0] == 0xe1:
                                return
                            if alive[0] == 0xe0:
                                self.send(0xe1)
                                return
                    break
            except ValueError:
                continue
        raise TimeoutError('The camera did not respond within 40 seconds. Check its power and 4G connection, then reconnect.')

    def write(self, data):
        for offset in range(0, len(data), 1024):
            seq = self.next_out
            self.next_out = (seq+1)&0xffff
            body = struct.pack('!BBH', 0xd1, 0, seq) + data[offset:offset+1024]
            self.pending[seq] = body
            self.send(0xd0, body)

    def pump(self):
        now = time.monotonic()
        if self.streaming:
            if self.ptz_deadline and now >= self.ptz_deadline:
                self.request_ptz('stop')
            if now >= self.next_battery:
                self.write(self.crypto.command(0x10004))
                self.next_battery = now+30
            while not self.controls.empty():
                try:
                    code, payload = self.controls.get_nowait()
                except queue.Empty:
                    break
                self.write(self.crypto.command(code, payload))
            self.responses()
        if now >= self.next_retry:
            for body in list(self.pending.values()):
                self.send(0xd0, body)
            self.next_retry = now+.5
        if now >= self.next_alive:
            self.send(0xe0); self.next_alive = now+3
        incoming = self.receive()
        if not incoming:
            return
        kind, body, source = incoming
        if source != self.peer:
            return
        if kind == 0xe0:
            self.send(0xe1)
        elif kind == 0xf0:
            raise ConnectionError('The camera closed the connection.')
        elif kind == 0xd1 and len(body) >= 4 and body[:2] == b'\xd1\0':
            count = int.from_bytes(body[2:4], 'big')
            if len(body) == 4+2*count:
                for seq in struct.unpack('!'+str(count)+'H', body[4:]):
                    self.pending.pop(seq, None)
        elif kind == 0xd0 and len(body) >= 4 and body[0] == 0xd1:
            channel, seq = body[1], int.from_bytes(body[2:4], 'big')
            if channel not in self.received:
                return
            self.send(0xd1, struct.pack('!BBHH', 0xd1, channel, 1, seq))
            distance = (seq-self.next_in[channel])&0xffff
            if distance >= 32768:
                return
            if distance > 1024 or len(body) > 1400:
                raise ValueError('Invalid P2P stream or too many missing packets.')
            reorder, buffer = self.reorder[channel], self.received[channel]
            reorder[seq] = body[4:]
            while self.next_in[channel] in reorder:
                index = self.next_in[channel]
                buffer.extend(reorder.pop(index)); self.next_in[channel] = (index+1)&0xffff
            if len(buffer) > MAX_FRAME+32:
                raise ValueError('The video frame exceeds the memory limit.')

    def read(self, channel, count, timeout=15):
        deadline = time.monotonic()+timeout
        while len(self.received[channel]) < count:
            if time.monotonic() >= deadline:
                raise TimeoutError('The camera stopped sending data. Reconnect to resume video.')
            self.pump()
        buffer = self.received[channel]
        data = bytes(buffer[:count]); del buffer[:count]
        return data

    def command(self, code, payload=b'', login=False):
        self.write(self.crypto.command(code, payload, login=login))
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            header = self.read(0, 24)
            magic, size, response_code, status = struct.unpack_from('<IIIi', header)
            if magic != 0x99999999 or size > 10240:
                raise ValueError('Invalid camera response.')
            body = self.read(0, size)
            if response_code != code:
                continue
            if status < 0:
                raise ValueError('Authentication was rejected.' if login else 'The camera rejected the video command.')
            if login and header[16] == 10:
                self.crypto.enable_v22()
            body = self.crypto.decrypt(body)
            if login and header[16] == 10 and (sum(body)&0xffff) != struct.unpack_from('<H', header, 17)[0]:
                raise ValueError('The authentication response failed the integrity check.')
            return body
        raise TimeoutError('The camera did not acknowledge the command.')

    def responses(self):
        buffer = self.received[0]
        while len(buffer) >= 24:
            magic, size, code, status = struct.unpack_from('<IIIi', buffer)
            if magic != 0x99999999 or size > 10240:
                raise ValueError('Invalid camera response.')
            if len(buffer) < 24+size:
                return
            body = self.crypto.decrypt(buffer[24:24+size])
            del buffer[:24+size]
            if code == 0x10004 and status >= 0 and len(body) >= 24:
                level = struct.unpack_from('<I', body, 8)[0]
                if level <= 100:
                    self.battery = level
                    self.charging = struct.unpack_from('<i', body)[0] == 0
            elif code == 0x1002b:
                self.ptz_error = status < 0

    def request_ptz(self, direction):
        directions = {'stop': 0, 'left': 1, 'right': 2, 'up': 3, 'down': 4}
        if direction not in directions or not self.streaming or self.closed.is_set():
            raise ValueError('PTZ control is unavailable.')
        code = directions[direction]
        if not code:
            while not self.controls.empty():
                try:
                    self.controls.get_nowait()
                except queue.Empty:
                    break
        self.controls.put_nowait((0x1002b, struct.pack('<IIIHH', 0, code, 1 if code else 0, 58, 20 if code else 0)))
        self.ptz_deadline = time.monotonic()+.8 if code else 0

    def media(self, audio=False):
        """Yield (lens 0/1 or audio 2, timestamp_ms, frame_type, payload)."""
        try:
            self.connect()
            result = self.command(0x481b, self.crypto.login_payload(), login=True)
            if len(result) < 48 or struct.unpack_from('<i', result)[0] != 0:
                raise ValueError('Authentication to the 4G camera was rejected.')
            live = self.command(0x1001, struct.pack('<IIIII', self.stream, 1, 0, 0, 0))
            if len(live) >= 16:
                self.audio_type = struct.unpack_from('<I', live, 12)[0]
            if audio:
                self.command(0x3103)
            self.streaming = True
            keyframe_seen = [False, False]
            while not self.closed.is_set():
                header = self.read(2, 16)
                magic, size, pts, kind = struct.unpack('<IIII', header)
                if magic not in (0x46565848, 0x46415848, 0x46445848, 0x43215678, 0x46000003) or not 0 < size <= MAX_FRAME:
                    raise ValueError('Invalid video frame.')
                data = self.read(2, size)
                if magic == 0x46445848:
                    self.dual = True
                    if not 0 <= pts <= size:
                        raise ValueError('Invalid dual frame.')
                    chunks = (data[:pts], data[pts:])
                else:
                    self.dual = False
                    chunks = (header+data,)
                for lens, chunk in enumerate(chunks):
                    if not chunk:
                        continue
                    if len(chunk) < 16:
                        raise ValueError('Incomplete inner frame.')
                    magic, size, pts, kind = struct.unpack('<IIII', chunk[:16])
                    data = chunk[16:]
                    if size != len(data) or magic not in (0x46565848, 0x46415848, 0x43215678, 0x46000003):
                        raise ValueError('Invalid inner frame.')
                    if magic in (0x46415848, 0x43215678):
                        if audio and lens == 0:
                            yield 2, pts, kind, data
                    elif magic == 0x46565848:
                        if kind & 1:
                            if size < 96:
                                raise ValueError('Incomplete video keyframe.')
                            data = self.crypto.decrypt(data[:96])+data[96:]
                            keyframe_seen[lens] = True
                        if keyframe_seen[lens]:
                            yield lens, pts, kind, data
        finally:
            self.close()

    def frames(self):
        for lens, pts, kind, data in self.media():
            if lens == self.lens or not self.dual:
                yield data
