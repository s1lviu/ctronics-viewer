"""Offline protocol regression tests: synthetic identifiers, no network or SDK.

Run with: python -m unittest test_protocol -v
Fixed reference vectors cover credential encoding, negotiation and media decoding.
"""
from collections import deque
import hashlib
import itertools
import queue
import socket
import struct
import unittest
from unittest.mock import Mock, patch

import hichip
from hichip_audio import alaw_pcm16, elementary_audio
from hichip_crypto import LoginCrypto, decrypt, encrypt


UID = "SSAS-000001-ABCDE"
PEER = ("198.51.100.23", 32100)
OTHER = ("198.51.100.24", 32100)


def crypto():
    return LoginCrypto("TEST-123456-ABCDE", "admin", "example-test-password",
                       nonce=123456789, random_user=bytes(range(32)),
                       random_password=bytes(range(129, 161)))


def response(code, body=b"", status=0, flag=0, checksum=0, magic=0x99999999):
    return struct.pack("<IIIiBHBI", magic, len(body), code, status, flag, checksum, 0, 0) + body


def media(data, kind=1, magic=0x46565848, size=None, pts=123):
    return struct.pack("<IIII", magic, len(data) if size is None else size, pts, kind) + data


def dual(first, second, split=None):
    return struct.pack("<IIII", 0x46445848, len(first) + len(second),
                       len(first) if split is None else split, 0x101) + first + second


class FakeDatagramSocket:
    def __init__(self):
        self.incoming = deque()
        self.sent = []
        self.closed = False

    def recvfrom(self, size):
        if self.incoming:
            return self.incoming.popleft()
        raise socket.timeout()

    def sendto(self, data, target):
        if self.closed:
            raise OSError("fake socket closed")
        self.sent.append((data, target))
        return len(data)

    def close(self):
        self.closed = True

    def feed(self, kind, body=b"", source=PEER):
        self.incoming.append((hichip.packet(kind, body), source))

    def data(self, channel, sequence, payload, source=PEER):
        self.feed(0xd0, struct.pack("!BBH", 0xd1, channel, sequence) + payload, source)


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, duration):
        self.now += duration
        return False


class ScheduledDatagramSocket(FakeDatagramSocket):
    """A timed packet script; no real socket or wall-clock waits."""
    def __init__(self, clock, events=()):
        super().__init__()
        self.clock = clock
        self.events = deque(events)
        self.sent_at = []
        self.endpoint = ("192.0.2.10", 45000)
        self.timeout = .1

    def bind(self, endpoint):
        assert endpoint == ("", 0)

    def connect(self, endpoint):
        self.route = endpoint

    def getsockname(self):
        return self.endpoint

    def settimeout(self, timeout):
        self.timeout = timeout

    def recvfrom(self, size):
        if self.events and self.events[0][0] <= self.clock.now + self.timeout:
            timestamp, kind, body, source = self.events.popleft()
            self.clock.now = max(self.clock.now, timestamp)
            return hichip.packet(kind, body), source
        self.clock.now += self.timeout
        raise socket.timeout()

    def sendto(self, data, target):
        self.sent_at.append((self.clock.now, data, target))
        return super().sendto(data, target)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # A regression must fail immediately if any test accidentally connects.
        for name in ("socket", "create_connection"):
            guard = patch.object(hichip.socket, name, side_effect=AssertionError("network forbidden in tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def camera(self):
        camera = hichip.Camera(UID, "admin", "test-only-password")
        camera.crypto = crypto()
        fake = FakeDatagramSocket()
        camera.sock, camera.peer = fake, PEER
        camera.next_retry = camera.next_alive = float("inf")
        self.addCleanup(camera.close)
        return camera, fake

    def fast_clock(self):
        return patch.object(hichip.time, "monotonic", side_effect=itertools.count())


class WireFormatTests(OfflineCase):
    def test_uid_wire_bytes_and_bounds(self):
        self.assertEqual(hichip.identity(UID), bytes.fromhex("5353415300000000000000014142434445000000"))
        self.assertEqual(len(hichip.identity("SSAS-999999-ABCDEFGH")), 20)
        for uid in (None, 123, "", "XXXX-000001-ABCDE", "SSAS-00001-ABCDE",
                    "SSAS-0000001-ABCDE", "SSAS-000001-abcdef", "SSAS-000001-ABCDEFGHI",
                    "SSAS-000001-", " SSAS-000001-ABCDE", "SSAS-٠٠٠٠٠١-ABCDE"):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                hichip.identity(uid)

    def test_address_layout_offset_and_port_bounds(self):
        encoded = bytes.fromhex("0002647d281e140a0000000000000000")
        self.assertEqual(hichip.address(("10.20.30.40", 32100)), encoded)
        self.assertEqual(hichip.read_address(b"prefix" + encoded, 6), ("10.20.30.40", 32100))
        for port in (1, 65535):
            self.assertEqual(hichip.read_address(hichip.address(("192.0.2.1", port))), ("192.0.2.1", port))
        for data in (encoded[:15], b"\x02\x00" + encoded[2:], encoded[:2] + bytes(2) + encoded[4:]):
            with self.subTest(data=data.hex()), self.assertRaises(ValueError):
                hichip.read_address(data)

    def test_udp_header_validation(self):
        c, fake = self.camera()
        for data in (b"", b"\xf1\xd0\x00", b"\xf0\xd0\x00\x00",
                     b"\xf1\xd0\x00\x02x", b"\xf1\xd0\x00\x00x"):
            fake.incoming.append((data, PEER))
            self.assertIsNone(c.receive())
        fake.feed(0xe0)
        self.assertEqual(c.receive(), (0xe0, b"", PEER))
        self.assertIsNone(c.receive())

    def test_input_limits_and_closed_connection(self):
        for args in ((UID, "admin", "x", 2), (UID, "", "x", 0), (UID, "admin", "", 0),
                     (UID, "a" * 16, "x", 0), (UID, "admin", "x" * 257, 0)):
            with self.subTest(args=(len(args[1]), len(args[2]), args[3])), self.assertRaises(ValueError):
                hichip.Camera(*args)
        for lens in (-1, 2, "0"):
            with self.subTest(lens=lens), self.assertRaises(ValueError):
                hichip.Camera(UID, "admin", "test-only-password", lens=lens)
        self.assertEqual(hichip.Camera(UID, "admin", "test-only-password").lens, 0)
        c, fake = self.camera()
        c.close()
        c.close()
        self.assertTrue(fake.closed)
        self.assertEqual(fake.sent, [(hichip.packet(0xf0), PEER)])
        with self.assertRaises(OSError):
            c.send(0xe0)


class RelayHandshakeTests(OfflineCase):
    SERVER = ("192.0.2.1", 32100)
    RELAY = ("198.51.100.10", 32100)
    RELAY2 = ("198.51.100.11", 32100)
    OFFER = ("203.0.113.10", 40000)
    OFFER2 = ("203.0.113.11", 40001)
    COOKIE = bytes.fromhex("12345678")
    COOKIE2 = bytes.fromhex("90abcdef")
    IDENT = bytes.fromhex("5353415300000000000000014142434445000000")

    def connect_script(self, events, timeout=6):
        clock = VirtualClock()
        fake = ScheduledDatagramSocket(clock, events)
        route = ScheduledDatagramSocket(clock)
        camera = hichip.Camera(UID, "admin", "test-only-password")
        self.addCleanup(camera.close)
        with patch.object(hichip, "SERVERS", {self.SERVER}), \
                patch.object(hichip.socket, "create_connection", side_effect=OSError("wake unavailable")), \
                patch.object(hichip.socket, "socket", side_effect=[fake, route]), \
                patch.object(hichip.time, "monotonic", clock), \
                patch.object(camera.closed, "wait", side_effect=clock.wait):
            camera.connect(timeout=timeout)
        return camera, fake, clock

    def prefix(self, relays=None):
        relays = relays or [self.RELAY]
        events = [(.1, 0x69, bytes([len(relays), 0, 0, 0]) +
                   b"".join(hichip.address(r) for r in relays), self.SERVER)]
        events.extend((.55 + i * .01, 0x71, b"", relay) for i, relay in enumerate(relays))
        events.append((1.05, 0x73, self.COOKIE + struct.pack("!H", 45001) + bytes(2), self.RELAY))
        return events

    def test_relay_wire_layout_immediate_request_and_hello_burst(self):
        # The ready source may use a different port on the offered relay IP.
        actual = (self.OFFER[0], 40009)
        events = self.prefix() + [
            (1.15, 0x82, hichip.address(self.OFFER) + self.COOKIE, self.SERVER),
            (1.5, 0x84, self.IDENT, actual),
            (1.6, 0xe1, b"", actual),
        ]
        camera, fake, clock = self.connect_script(events)
        self.assertEqual((camera.mode, camera.peer), ("relay", actual))
        # Raw cookie stays in its received byte order; address uses reversed IP
        # and little-endian port, while the DID serial is big-endian.
        relay_address = bytes.fromhex("0002c9af0a6433c60000000000000000")
        request = b"\xf1\x80\x00\x28" + self.IDENT + relay_address + self.COOKIE
        requests = [(at, data, target) for at, data, target in fake.sent_at if data[1] == 0x80]
        self.assertEqual(requests, [(1.05, request, self.SERVER)])
        hello = b"\xf1\x83\x00\x1c" + self.COOKIE + self.IDENT + bytes(4)
        hellos = [(at, data, target) for at, data, target in fake.sent_at if data[1] == 0x83]
        self.assertEqual(len(hellos), 3)
        for i, (at, data, target) in enumerate(hellos):
            self.assertAlmostEqual(at, 1.15 + .01 * i)
            self.assertEqual((data, target), (hello, self.OFFER))
        self.assertIn((b"\xf1\xe0\x00\x00", actual), fake.sent)

    def test_allocation_hold_cookie_match_and_late_old_offer(self):
        events = self.prefix([self.RELAY, self.RELAY2]) + [
            (1.5, 0x73, self.COOKIE2 + struct.pack("!H", 45002) + bytes(2), self.RELAY2),
            (1.6, 0x82, hichip.address(self.OFFER2) + self.COOKIE2, self.SERVER),
            (1.7, 0x82, hichip.address(self.OFFER) + self.COOKIE, self.SERVER),
            (3.2, 0x73, self.COOKIE2 + struct.pack("!H", 45002) + bytes(2), self.RELAY2),
            (3.3, 0x82, hichip.address(self.OFFER2) + self.COOKIE2, self.SERVER),
            (3.4, 0x84, self.IDENT, self.OFFER),
            (3.5, 0xe1, b"", self.OFFER),
        ]
        camera, fake, clock = self.connect_script(events)
        self.assertEqual(camera.peer, self.OFFER)
        requests = [(at, data[-4:]) for at, data, _ in fake.sent_at if data[1] == 0x80]
        self.assertEqual(requests, [(1.05, self.COOKIE), (3.2, self.COOKIE2)])
        hellos = [(at, target) for at, data, target in fake.sent_at if data[1] == 0x83]
        self.assertEqual([target for _, target in hellos], [self.OFFER] * 3 + [self.OFFER2] * 3)
        self.assertGreaterEqual(hellos[3][0], 3.3)

    def test_malformed_foreign_and_wrong_identity_handshakes_are_ignored(self):
        events = [
            (.05, 0x69, b"\x01\0\0\0" + hichip.address(OTHER), OTHER),
        ] + self.prefix() + [
            (1.1, 0x82, hichip.address(OTHER) + self.COOKIE, OTHER),
            (1.15, 0x82, hichip.address(self.OFFER) + self.COOKIE[:3], self.SERVER),
            (1.2, 0x82, hichip.address(self.OFFER) + self.COOKIE2, self.SERVER),
            (1.25, 0x82, hichip.address(self.OFFER) + self.COOKIE, self.SERVER),
            (1.4, 0x84, self.IDENT, OTHER),
            (1.45, 0x84, self.IDENT[:-1] + b"X", self.OFFER),
            (1.5, 0x84, self.IDENT, self.OFFER),
            (1.6, 0xe1, b"", self.OFFER),
        ]
        camera, fake, clock = self.connect_script(events)
        self.assertEqual(camera.peer, self.OFFER)
        hellos = [(at, target) for at, data, target in fake.sent_at if data[1] == 0x83]
        self.assertEqual(len(hellos), 3)
        self.assertGreaterEqual(hellos[0][0], 1.25)
        self.assertFalse(any(target == OTHER for _, target in fake.sent))

    def test_peer_alive_both_directions_with_legacy_and_extended_body(self):
        for kind in (0xe0, 0xe1):
            for body in (b"", struct.pack("<II", 123, 456)):
                with self.subTest(kind=kind, body_length=len(body)):
                    events = self.prefix() + [
                        (1.15, 0x82, hichip.address(self.OFFER) + self.COOKIE, self.SERVER),
                        (1.5, 0x84, self.IDENT, self.OFFER),
                        (1.55, 0xe1, b"", OTHER),
                        (1.6, 0xe1, b"invalid body", self.OFFER),
                        (1.7, kind, body, self.OFFER),
                    ]
                    camera, fake, clock = self.connect_script(events)
                    self.assertEqual(camera.peer, self.OFFER)
                    self.assertAlmostEqual(clock.now, 1.7)
                    if kind == 0xe0:
                        # Official Mac builds a new empty ACK, without echoing
                        # the peer's optional eight-byte extension.
                        self.assertIn((b"\xf1\xe1\x00\x00", self.OFFER), fake.sent)
                    self.assertFalse(any(target == OTHER for _, target in fake.sent))


class ReliableTransportTests(OfflineCase):
    def test_write_fragmentation_wrap_and_exact_ack(self):
        c, fake = self.camera()
        c.next_out = 65535
        data = bytes(range(256)) * 9
        c.write(data)
        expected = []
        for sequence, fragment in zip((65535, 0, 1), (data[:1024], data[1024:2048], data[2048:])):
            expected.append((hichip.packet(0xd0, struct.pack("!BBH", 0xd1, 0, sequence) + fragment), PEER))
        self.assertEqual(fake.sent, expected)
        self.assertEqual(set(c.pending), {65535, 0, 1})
        self.assertEqual(c.next_out, 2)
        fake.feed(0xd1, struct.pack("!BBHHH", 0xd1, 0, 2, 65535, 1))
        c.pump()
        self.assertEqual(set(c.pending), {0})

    def test_malformed_and_foreign_acks_do_not_clear_pending(self):
        c, fake = self.camera()
        c.write(b"test")
        for source, body in ((OTHER, b"\xd1\0\0\1\0\0"), (PEER, b"\xd1\0\0\2\0\0"),
                             (PEER, b"\xd1\2\0\1\0\0"), (PEER, b"\xd0\0\0\1\0\0")):
            fake.feed(0xd1, body, source)
            c.pump()
            self.assertEqual(set(c.pending), {0})
        fake.feed(0xd1, b"\xd1\0\0\1\x7f\xff")
        c.pump()
        self.assertEqual(set(c.pending), {0})

    def test_reassembly_out_of_order_duplicates_and_channel_isolation(self):
        c, fake = self.camera()
        for channel, seq, payload in ((0, 1, b"B"), (2, 0, b"video"), (0, 1, b"B"),
                                      (0, 0, b"A"), (0, 0, b"A"), (0, 2, b"C")):
            fake.data(channel, seq, payload)
            c.pump()
        self.assertEqual(c.read(0, 2), b"AB")
        self.assertEqual(c.read(0, 1), b"C")
        self.assertEqual(c.read(2, 5), b"video")
        self.assertEqual(c.next_in, {0: 3, 2: 1})
        self.assertEqual(c.reorder, {0: {}, 2: {}})
        acks = [data for data, target in fake.sent if data[1] == 0xd1]
        self.assertEqual(len(acks), 6)  # Duplicates are acknowledged again.
        self.assertEqual(acks[0], hichip.packet(0xd1, b"\xd1\0\0\1\0\1"))

    def test_receive_sequence_wrap_and_late_duplicate(self):
        c, fake = self.camera()
        c.next_in[2] = 65535
        for seq, payload in ((0, b"B"), (65535, b"A"), (65535, b"A"), (1, b"C")):
            fake.data(2, seq, payload)
            c.pump()
        self.assertEqual(c.read(2, 3), b"ABC")
        self.assertEqual(c.next_in[2], 2)

    def test_foreign_data_and_unknown_channels_are_ignored(self):
        c, fake = self.camera()
        fake.data(0, 0, b"injected", OTHER)
        c.pump()
        fake.data(3, 0, b"unsupported")
        c.pump()
        self.assertFalse(fake.sent)
        self.assertEqual(c.received, {0: bytearray(), 2: bytearray()})
        self.assertEqual(c.next_in, {0: 0, 2: 0})

    def test_gap_and_datagram_limits(self):
        for seq, payload in ((1025, b"x"), (0, b"x" * 1397)):
            c, fake = self.camera()
            fake.data(2, seq, payload)
            with self.assertRaises(ValueError):
                c.pump()
            self.assertEqual(c.received[2], b"")
        c, fake = self.camera()
        fake.data(2, 32768, b"old-or-ambiguous")
        c.pump()
        self.assertEqual(c.received[2], b"")
        with patch.object(hichip, "MAX_FRAME", 32):
            fake.data(2, 0, b"x" * 65)
            with self.assertRaises(ValueError):
                c.pump()

    def test_pending_retransmission_and_keepalive(self):
        c, fake = self.camera()
        c.write(b"retry-me")
        initial = fake.sent[0]
        fake.sent.clear()
        c.next_retry = c.next_alive = 0
        with patch.object(hichip.time, "monotonic", return_value=10):
            c.pump()
            self.assertEqual(fake.sent, [initial, (hichip.packet(0xe0), PEER)])
            c.pump()
            self.assertEqual(len(fake.sent), 2)
        fake.feed(0xe0)
        c.pump()
        self.assertEqual(fake.sent[-1], (hichip.packet(0xe1), PEER))
        fake.feed(0xf0)
        with self.assertRaises(ConnectionError):
            c.pump()

    def test_truncated_read_times_out_without_hanging(self):
        c, fake = self.camera()
        fake.data(0, 0, b"short")
        with self.fast_clock(), self.assertRaises(TimeoutError):
            c.read(0, 24)
        self.assertEqual(c.received[0], b"short")


class AuthenticationAndMediaTests(OfflineCase):
    def test_login_negotiates_before_decryption_and_checks_checksum(self):
        c, fake = self.camera()
        server = crypto()
        server.enable_v22()
        plain = struct.pack("<4I", 0, 1, 2, 3) + bytes(range(32))
        c.received[0].extend(response(0x481b, server.encrypt(plain), flag=10, checksum=sum(plain) & 0xffff))
        self.assertEqual(c.command(0x481b, c.crypto.login_payload(), login=True), plain)
        self.assertEqual(c.crypto.key, server.key)
        self.assertTrue(c.crypto.extended)
        self.assertEqual(len(fake.sent), 1)

    def test_auth_refusal_and_checksum_failure(self):
        for status in (-1, -2):
            c, fake = self.camera()
            c.received[0].extend(response(0x481b, status=status, flag=10))
            with self.assertRaisesRegex(ValueError, "Authentication"):
                c.command(0x481b, b"", login=True)
            self.assertFalse(c.crypto.extended)
        c, fake = self.camera()
        server = crypto()
        server.enable_v22()
        c.received[0].extend(response(0x481b, server.encrypt(bytes(48)), flag=10, checksum=1))
        with self.assertRaisesRegex(ValueError, "integrity"):
            c.command(0x481b, b"", login=True)

    def test_command_header_limits_and_truncation(self):
        for magic, size in ((0, 0), (0x99999999, 10241)):
            c, fake = self.camera()
            c.received[0].extend(struct.pack("<6I", magic, size, 0x1001, 0, 0, 0))
            with self.assertRaises(ValueError):
                c.command(0x1001)
        c, fake = self.camera()
        c.received[0].extend(response(0x1001, bytes(48))[:-1])
        with self.fast_clock(), self.assertRaises(TimeoutError):
            c.command(0x1001)

    def test_unrelated_response_does_not_corrupt_following_response(self):
        c, fake = self.camera()
        desired = b"response-is-here!"
        c.received[0].extend(response(0x1234, b"ignore-me") + response(0x1001, c.crypto.encrypt(desired)))
        self.assertEqual(c.command(0x1001), desired)
        self.assertFalse(c.received[0])

    def prepare_frames(self, wire=b"", login=bytes(48), lens=0):
        c, fake = self.camera()
        c.lens = lens
        c.connect = Mock()
        c.command = Mock(side_effect=(login, b""))
        c.received[2].extend(wire)
        return c, fake, c.frames()

    def test_low_power_response_length_and_result(self):
        for result in (b"", bytes(4), bytes(47), struct.pack("<i", -1) + bytes(44)):
            c, fake, stream = self.prepare_frames(login=result)
            with self.subTest(size=len(result)), self.assertRaisesRegex(ValueError, "Authentication"):
                next(stream)
            self.assertTrue(c.closed.is_set())
            self.assertTrue(fake.closed)

    def test_media_decrypts_only_iframe_prefix_and_discards_preceding_pframes(self):
        original = b"\0\0\0\1\x65" + bytes(range(123))
        cipher = crypto().encrypt(original[:96]) + original[96:]
        pframe = b"\0\0\0\1\x41P-frame-remains-plain"
        wire = media(b"before-I-frame", kind=0) + media(b"audio", magic=0x46415848)
        wire += media(cipher) + media(pframe, kind=0)
        c, fake, stream = self.prepare_frames(wire)
        self.assertEqual(next(stream), original)
        self.assertEqual(next(stream), pframe)
        stream.close()
        self.assertTrue(fake.closed)

    def test_dual_frames_select_each_lens_and_keep_inner_headers_out_of_video(self):
        originals = [b"\0\0\0\1\x65" + bytes(range(123)),
                     b"\0\0\0\1\x65" + bytes(range(139, 0, -1))]
        iframe = [media(crypto().encrypt(data[:96]) + data[96:]) for data in originals]
        pframe = [b"\0\0\0\1\x41lens-zero-P", b"\0\0\0\1\x41lens-one-P"]
        wire = dual(media(b"early-zero", kind=0), media(b"early-one", kind=0))
        wire += dual(*iframe) + dual(*(media(data, kind=0) for data in pframe))
        for lens in (0, 1):
            with self.subTest(lens=lens):
                c, fake, stream = self.prepare_frames(wire, lens=lens)
                self.assertEqual(next(stream), originals[lens])
                self.assertEqual(next(stream), pframe[lens])
                stream.close()
                self.assertTrue(fake.closed)

    def test_absent_dual_lens_is_skipped_without_losing_next_frame(self):
        original = b"\0\0\0\1\x65" + bytes(range(123))
        iframe = media(crypto().encrypt(original[:96]) + original[96:])
        for lens in (0, 1):
            absent = dual(b"", iframe) if lens == 0 else dual(iframe, b"")
            c, fake, stream = self.prepare_frames(absent + dual(iframe, iframe), lens=lens)
            self.assertEqual(next(stream), original)
            self.assertFalse(c.received[2])
            stream.close()

    def test_ordinary_and_dual_audio_metadata_are_consumed_before_video(self):
        original = b"\0\0\0\1\x65" + bytes(range(123))
        iframe = media(crypto().encrypt(original[:96]) + original[96:])
        metadata = [media(b"non-video", magic=magic)
                    for magic in (0x46415848, 0x43215678, 0x46000003)]
        wire = b"".join(metadata + [dual(item, item) for item in metadata]) + iframe
        for lens in (0, 1):
            c, fake, stream = self.prepare_frames(wire, lens=lens)
            self.assertEqual(next(stream), original)
            self.assertFalse(c.received[2])
            stream.close()

    def test_dual_split_inner_size_magic_and_short_iframe_are_rejected(self):
        good = media(bytes(96))
        for wire in (dual(good, good, split=2 * len(good) + 1),
                     dual(b"short", good),
                     dual(media(bytes(96), size=97), good),
                     dual(media(bytes(96), magic=0), good),
                     dual(media(bytes(95)), good)):
            c, fake, stream = self.prepare_frames(wire)
            with self.subTest(header=wire[:32].hex()), self.assertRaises(ValueError):
                next(stream)
            self.assertTrue(fake.closed)
        # A malformed selected second frame must also be caught.
        c, fake, stream = self.prepare_frames(dual(good, b"short"), lens=1)
        with self.assertRaises(ValueError):
            next(stream)

    def test_invalid_media_header_and_short_iframe(self):
        for wire in (media(b"", size=0), media(b"", size=hichip.MAX_FRAME + 1),
                     media(b"x", magic=0), media(bytes(95))):
            c, fake, stream = self.prepare_frames(wire)
            with self.subTest(header=wire[:16].hex()), self.assertRaises(ValueError):
                next(stream)
            self.assertTrue(fake.closed)

    def test_truncated_media_header_and_body(self):
        for wire in (media(bytes(96))[:15], media(bytes(20), size=96)):
            c, fake, stream = self.prepare_frames(wire)
            with self.fast_clock(), self.assertRaises(TimeoutError):
                next(stream)
            self.assertTrue(fake.closed)


class MultiplexedMediaTests(OfflineCase):
    def prepare_media(self, wire, audio=False, codec=0):
        camera, fake = self.camera()
        camera.connect = Mock()
        replies = [bytes(48), struct.pack("<4I", 0, 0, 0, codec)]
        if audio:
            replies.append(b"")
        camera.command = Mock(side_effect=replies)
        camera.received[2].extend(wire)
        stream = camera.media(audio=audio)
        self.addCleanup(stream.close)
        return camera, fake, stream

    def test_two_lenses_have_independent_keyframe_state_and_timestamps(self):
        first = b"\0\0\0\1\x65" + bytes(range(123))
        second = b"\0\0\0\1\x65" + bytes(range(139, 0, -1))
        iframe = [crypto().encrypt(data[:96]) + data[96:] for data in (first, second)]
        wire = dual(media(iframe[0], pts=1000), media(b"skip-before-I", kind=0, pts=1001))
        wire += dual(media(b"lens0-P", kind=0, pts=1067), media(iframe[1], pts=1068))
        wire += dual(media(b"lens0-P2", kind=0, pts=1134), media(b"lens1-P", kind=0, pts=1135))
        camera, fake, stream = self.prepare_media(wire)
        self.assertEqual([next(stream) for _ in range(5)], [
            (0, 1000, 1, first), (0, 1067, 0, b"lens0-P"),
            (1, 1068, 1, second), (0, 1134, 0, b"lens0-P2"),
            (1, 1135, 0, b"lens1-P"),
        ])
        self.assertTrue(camera.dual)
        self.assertFalse(camera.received[2])

    def test_audio_start_and_raw_alaw_share_existing_media_channel(self):
        # This is the camera's observed framing shape, with synthetic samples.
        # Audio kind0 and kind1 both remain byte-for-byte plain: kind1 does not
        # make an audio frame subject to the encrypted video I-frame rule.
        audio = bytes(range(256)) * 3 + bytes(range(32))
        wire = media(audio, kind=0, magic=0x46415848, pts=10832)
        wire += media(audio[::-1], kind=1, magic=0x46415848, pts=10932)
        camera, fake, stream = self.prepare_media(wire, audio=True)
        with patch.object(camera.crypto, "decrypt", wraps=camera.crypto.decrypt) as decrypt_audio:
            self.assertEqual(next(stream), (2, 10832, 0, audio))
            self.assertEqual(next(stream), (2, 10932, 1, audio[::-1]))
            decrypt_audio.assert_not_called()
        self.assertEqual(camera.audio_type, 0)
        self.assertEqual([call.args[0] for call in camera.command.call_args_list], [0x481b, 0x1001, 0x3103])
        self.assertEqual(camera.command.call_args_list[1].args[1], struct.pack("<IIIII", 0, 1, 0, 0, 0))
        self.assertEqual(camera.command.call_args_list[2].args, (0x3103,))
        stream.close()
        self.assertTrue(fake.closed)

    def test_muted_audio_and_duplicate_dual_audio_are_consumed_once(self):
        sample = bytes([0xd5]) * 800
        raw = b"\0\0\0\1\x65" + bytes(range(123))
        iframe = media(crypto().encrypt(raw[:96]) + raw[96:])
        audio = media(sample, kind=0, magic=0x46415848, pts=2000)
        camera, fake, stream = self.prepare_media(audio + iframe, audio=False)
        self.assertEqual(next(stream), (0, 123, 1, raw))
        self.assertEqual([call.args[0] for call in camera.command.call_args_list], [0x481b, 0x1001])
        camera, fake, stream = self.prepare_media(dual(audio, audio) + dual(iframe, iframe), audio=True)
        self.assertEqual([next(stream) for _ in range(3)], [
            (2, 2000, 0, sample), (0, 123, 1, raw), (1, 123, 1, raw),
        ])


class BatteryResponseTests(OfflineCase):
    @staticmethod
    def status(level, charge=0):
        return struct.pack("<iiIIII", charge, -75, level, 0, 0, 0)

    def test_split_encrypted_response_waits_for_complete_body(self):
        camera, fake = self.camera()
        camera.crypto.enable_v22()
        wire = response(0x10004, camera.crypto.encrypt(self.status(75)))
        for boundary in (23, 24, len(wire) - 1):
            with self.subTest(boundary=boundary):
                camera.battery = camera.charging = None
                camera.received[0] = bytearray(wire[:boundary])
                camera.responses()
                self.assertIsNone(camera.battery)
                self.assertEqual(camera.received[0], wire[:boundary])
                camera.received[0].extend(wire[boundary:])
                camera.responses()
                self.assertEqual((camera.battery, camera.charging), (75, True))
                self.assertFalse(camera.received[0])

    def test_refused_unknown_short_and_out_of_range_status_do_not_overwrite(self):
        camera, fake = self.camera()
        camera.battery, camera.charging = 75, True
        for code, payload, status in (
            (0x10004, self.status(50), -1),
            (0x10004, self.status(50)[:23], 0),
            (0x10004, self.status(101), 0),
            (0x10004, self.status(0xffffffff), 0),
            (0x10005, self.status(50), 0),
        ):
            camera.received[0].extend(response(code, camera.crypto.encrypt(payload), status=status))
        camera.responses()
        self.assertEqual((camera.battery, camera.charging), (75, True))
        self.assertFalse(camera.received[0])
        for level, charge in ((0, 1), (100, 0)):
            camera.received[0].extend(response(0x10004, camera.crypto.encrypt(self.status(level, charge))))
            camera.responses()
            self.assertEqual((camera.battery, camera.charging), (level, charge == 0))

    def test_bad_response_header_is_rejected(self):
        for magic, size in ((0, 24), (0x99999999, 10241)):
            camera, fake = self.camera()
            camera.received[0].extend(struct.pack("<6I", magic, size, 0x10004, 0, 0, 0))
            with self.subTest(magic=magic, size=size), self.assertRaises(ValueError):
                camera.responses()

    def test_battery_polling_is_nonblocking_and_limited_to_once_per_interval(self):
        camera, fake = self.camera()
        camera.streaming = True
        camera.write = Mock()
        fake.data(2, 0, b"media-still-arrives")
        with patch.object(hichip.time, "monotonic", return_value=100):
            camera.pump()
        self.assertEqual(camera.received[2], b"media-still-arrives")
        with patch.object(hichip.time, "monotonic", return_value=129.9):
            camera.pump()
        self.assertEqual(camera.write.call_count, 1)
        with patch.object(hichip.time, "monotonic", return_value=130):
            camera.pump()
        self.assertEqual(camera.write.call_count, 2)
        for call in camera.write.call_args_list:
            self.assertEqual(call.args, (struct.pack("<6I", 0x99999999, 0, 0x10004, 0, 0, 0),))


class PtzControlTests(OfflineCase):
    def active_camera(self):
        camera, fake = self.camera()
        camera.streaming = True
        camera.next_battery = float("inf")
        return camera, fake

    def test_direction_payloads_are_bounded_and_unavailable_states_reject(self):
        camera, fake = self.active_camera()
        with patch.object(hichip.time, "monotonic", return_value=100):
            for direction, code in (("left", 1), ("right", 2), ("up", 3), ("down", 4)):
                camera.request_ptz(direction)
                self.assertEqual(camera.controls.get_nowait(),
                                 (0x1002b, struct.pack("<IIIHH", 0, code, 1, 58, 20)))
                self.assertEqual(camera.ptz_deadline, 100.8)
        with self.assertRaises(ValueError):
            camera.request_ptz("invalid")
        camera.streaming = False
        with self.assertRaises(ValueError):
            camera.request_ptz("left")
        camera.streaming = True
        camera.close()
        with self.assertRaises(ValueError):
            camera.request_ptz("left")

    def test_stop_and_watchdog_take_priority_over_full_move_queue(self):
        for watchdog in (False, True):
            with self.subTest(watchdog=watchdog):
                camera, fake = self.active_camera()
                camera.write = Mock()
                with patch.object(hichip.time, "monotonic", return_value=100):
                    for _ in range(16):
                        camera.request_ptz("left")
                self.assertTrue(camera.controls.full())
                with patch.object(hichip.time, "monotonic", return_value=101):
                    if not watchdog:
                        camera.request_ptz("stop")
                    camera.pump()
                camera.write.assert_called_once()
                sent = camera.write.call_args.args[0]
                self.assertEqual(struct.unpack_from("<I", sent, 8)[0], 0x1002b)
                self.assertEqual(camera.crypto.decrypt(sent[24:]), struct.pack("<IIIHH", 0, 0, 0, 58, 0))
                self.assertFalse(camera.controls.qsize())
                self.assertEqual(camera.ptz_deadline, 0)

    def test_watchdog_fires_once_after_deadline(self):
        camera, fake = self.active_camera()
        camera.write = Mock()
        with patch.object(hichip.time, "monotonic", return_value=100):
            camera.request_ptz("up")
            camera.pump()
        self.assertEqual(camera.write.call_count, 1)
        with patch.object(hichip.time, "monotonic", return_value=100.79):
            camera.pump()
        self.assertEqual(camera.write.call_count, 1)
        with patch.object(hichip.time, "monotonic", return_value=100.8):
            camera.pump()
        with patch.object(hichip.time, "monotonic", return_value=101):
            camera.pump()
        self.assertEqual(camera.write.call_count, 2)
        stop = camera.write.call_args.args[0]
        self.assertEqual(camera.crypto.decrypt(stop[24:]), struct.pack("<IIIHH", 0, 0, 0, 58, 0))

    def test_stop_draining_queue_concurrently_does_not_abort_playback(self):
        camera, fake = self.active_camera()
        # Deterministically model HTTP stop draining the queue between pump's
        # emptiness check and dequeue. Queue.empty() is advisory across threads.
        with patch.object(camera.controls, "empty", side_effect=[False, True]), \
                patch.object(camera.controls, "get_nowait", side_effect=queue.Empty):
            camera.pump()
        self.assertFalse(camera.closed.is_set())

    def test_close_sends_stop_even_if_requested_stop_is_still_queued(self):
        camera, fake = self.active_camera()
        with patch.object(hichip.time, "monotonic", return_value=100):
            camera.request_ptz("right")
            camera.pump()
            camera.request_ptz("stop")
        fake.sent.clear()
        camera.close()
        self.assertEqual([data[1] for data, _ in fake.sent], [0xd0, 0xf0])
        wire, endpoint = fake.sent[0]
        self.assertEqual(endpoint, PEER)
        self.assertEqual(struct.unpack_from("<I", wire, 16)[0], 0x1002b)
        self.assertEqual(camera.crypto.decrypt(wire[32:]), struct.pack("<IIIHH", 0, 0, 0, 58, 0))
        self.assertTrue(fake.closed)
        camera.close()
        self.assertEqual(len(fake.sent), 2)

    def test_refused_ptz_response_sets_error_without_corrupting_status(self):
        camera, fake = self.active_camera()
        camera.battery, camera.charging = 75, True
        camera.received[0].extend(response(0x1002b, status=-1))
        camera.responses()
        self.assertTrue(camera.ptz_error)
        self.assertEqual((camera.battery, camera.charging), (75, True))
        camera.received[0].extend(response(0x1002b, status=0))
        camera.responses()
        self.assertFalse(camera.ptz_error)


class AudioVectorTests(unittest.TestCase):
    def test_all_alaw_codepoints_match_reference_decoder(self):
        decoded = alaw_pcm16(bytes(range(256)))
        self.assertEqual(len(decoded), 512)
        self.assertEqual(hashlib.sha256(decoded).hexdigest(),
                         "e04788d110e58ff8c70c93b8480190d973e3b67876b6119abbaec766cc75c174")
        # Check signedness and byte order explicitly in addition to the reference
        # vector digest. A-law's two nearest-to-zero values are +/-8.
        self.assertEqual(alaw_pcm16(bytes([0xd5, 0x55])), b"\x08\0\xf8\xff")
        self.assertEqual(alaw_pcm16(b""), b"")

    def test_each_twenty_millisecond_block_loses_its_own_optional_prefix(self):
        for codec, width in ((0, 160), (1, 40)):
            for count in (1, 5):
                raw = b"".join(bytes([i + 1]) * width for i in range(count))
                wrapped = b"".join(struct.pack("<I", 0x12340000 + i) + bytes([i + 1]) * width
                                    for i in range(count))
                with self.subTest(codec=codec, blocks=count):
                    self.assertEqual(elementary_audio(raw, codec), raw)
                    self.assertEqual(elementary_audio(wrapped, codec), raw)
        for codec, data in ((0, b""), (0, bytes(159)), (0, bytes(804)), (0, bytes(819)),
                            (1, bytes(199)), (1, bytes(224)), (2, bytes(160)), (3, bytes(800))):
            with self.subTest(codec=codec, size=len(data)), self.assertRaises(ValueError):
                elementary_audio(data, codec)


class CryptoVectorTests(unittest.TestCase):
    def test_complete_login_reference_vector(self):
        c = crypto()
        self.assertEqual(c.key.hex(), "2f52e43efa67b7d720a6289acb6c4d2e")
        payload = c.login_payload()
        self.assertEqual(len(payload), 688)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), "8c34ec676a3ffc22ecda3d6bb2ad2cc1dcdba991798071cd282d8d2689bc1339")
        self.assertEqual(sum(payload) & 0xffff, 20656)
        packet = c.command(0x1000, payload, login=True)
        self.assertEqual(packet[:24].hex(), "99999999b002000000100000000000000ab0500015cd5b07")
        self.assertEqual(hashlib.sha256(packet).hexdigest(), "a98fd3a0ffa83c29723d42059db0c6d91c7c1a2a0ad795a153645c6a5f350ee5")

    def test_negotiation_and_video_reference_vector(self):
        c = crypto()
        c.enable_v22()
        self.assertEqual(c.key.hex(), "acc16780e89ed1c1e2858ce920f93dcf")
        expected = bytes.fromhex(
            "b76528cd9b9f407036c9a369983a1dd4a735d46f9103732f41a4eb9b554e1134621fa1623dedd0aeb5d827d0b8d610f929d6d2735ae68bf382b956e3f8e4175177a88e3ee2f6d68fd49b5e193028162fe2a7a0458e92a96751d6945cc8239639")
        self.assertEqual(c.encrypt(bytes(range(96))), expected)
        self.assertEqual(c.decrypt(expected), bytes(range(96)))

    def test_normal_command_header_in_both_crypto_modes(self):
        c = crypto()
        payload = struct.pack("<IIIII", 1, 1, 0, 0, 0)
        for extended in (False, True):
            if extended:
                c.enable_v22()
            packet = c.command(0x1001, payload)
            self.assertEqual(packet[:24], struct.pack("<6I", 0x99999999, 20, 0x1001, 0, 0, 0))
            self.assertEqual(c.decrypt(packet[24:]), payload)

    def test_cipher_cap_plain_video_and_mask_boundary(self):
        c = crypto()
        data = bytes(range(256)) * 3
        ciphertext = c.encrypt(data)
        self.assertNotEqual(ciphertext[:400], data[:400])
        self.assertEqual(ciphertext[400:], data[400:])
        for extended in (False, True):
            clear_video = b"\0\0\0\1" + bytes(range(92))
            self.assertEqual(decrypt(c.key, clear_video, extended), clear_video)
        self.assertEqual(encrypt(c.key, data[:43], True), encrypt(c.key, data[:43]))


if __name__ == "__main__":
    unittest.main()
