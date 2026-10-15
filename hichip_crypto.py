"""HiChip authenticated command framing and encryption.

Only PyCryptodome is required.  This module has no network or logging side effects.
"""
import hashlib
import secrets
import struct
import time

from Crypto.Cipher import AES


_MASK = bytes.fromhex(
    "613c325f6a29355e3f23685d28392a2530246c3e406d7d2f36217e742960715b26352e3a307c2b387b2c27"
)
_POSITIONS = (
    (14, 15, 16, 17, 30, 31, 32, 33, 34, 70, 71, 72, 73, 74, 75, 76,
     110, 111, 112, 198, 256, 257, 258, 259, 260, 261, 262, 263, 280, 281, 282, 283),
    (22, 23, 24, 25, 32, 33, 34, 35, 36, 75, 76, 77, 78, 79, 80, 81,
     121, 122, 123, 199, 240, 241, 242, 243, 244, 245, 246, 247, 261, 262, 263, 264),
)


def _mask(data):
    # EXT skips the final block for exact multiples of 43; required for interoperability.
    size = len(data) - (43 if data and len(data) % 43 == 0 else 0)
    return bytes(v ^ _MASK[i % 43] if i < size else v for i, v in enumerate(data))


def encrypt(key, data, extended=False):
    data = bytes(data)
    size = min(len(data) // 16, 25) * 16
    out = AES.new(key, AES.MODE_ECB).encrypt(bytes(v ^ 0x3f for v in data[:size])) + data[size:]
    return _mask(out) if extended else out


def decrypt(key, data, extended=False):
    data = bytes(data)
    # The SDK accepts already-plaintext Annex B video without transforming it.
    if len(data) <= 3 or data.startswith(b"\x00\x00\x00\x01"):
        return data
    if extended:
        data = _mask(data)
    size = min(len(data) // 16, 25) * 16
    plain = AES.new(key, AES.MODE_ECB).decrypt(data[:size])
    return bytes(v ^ 0x3f for v in plain) + data[size:]


def _credential(key, uid, value, random_bytes, selector):
    encrypted = iter(encrypt(key, value.ljust(256, b"0") + uid.ljust(32, b"0")))
    inserted = dict(zip(_POSITIONS[selector], random_bytes))
    return bytes(inserted[i] if i in inserted else next(encrypted) for i in range(320))


def _select_key(digest_input, priority, count, random_bytes, ranges):
    digest = hashlib.md5(digest_input).hexdigest().encode("ascii")
    selected = [i for char in priority for i, v in enumerate(digest) if v == char][:count]
    for low, high, limit in ranges:
        selected += [v for v in random_bytes if low <= v <= high][:limit]
        if len(selected) >= 16:
            break
    return bytes(selected[:16]).ljust(16, b"\x00")


def negotiated_key(uid, username, password, random_user, random_password):
    a = _select_key(uid + b"&" + username + b"*" + password, b"C0E7B5A", 6, random_user,
                    ((79, 110, 2), (48, 78, 2), (0, 47, 2), (111, 160, 2),
                     (129, 160, 32), (55, 99, 32), (0, 54, 32), (100, 128, 32)))
    b = _select_key(username + b"@" + uid + b"-" + password, b"D839F0B1", 7, random_password,
                    ((68, 105, 2), (61, 67, 2), (0, 60, 2), (106, 160, 2),
                     (140, 160, 32), (77, 139, 32), (0, 45, 32), (46, 76, 32)))
    return encrypt(a, b)


def _random32():
    # The wire format carries these bytes; matching the libc RNG is unnecessary.
    # The protocol uses bytes 0..160 with at least one high bit set.
    value = bytearray(secrets.randbelow(161) for _ in range(32))
    value[1] = 128 + secrets.randbelow(32)
    return bytes(value)


class LoginCrypto:
    def __init__(self, uid, username, password, *, nonce=None, random_user=None, random_password=None):
        self.uid, self.username, self.password = (v.encode("ascii") for v in (uid, username, password))
        for label, value, size in (("UID", self.uid, 31), ("username", self.username, 15),
                                   ("password", self.password, 256)):
            if b"\x00" in value or len(value) > size:
                raise ValueError(f"{label} exceeds the SDK field size or contains a NUL byte")
        self.nonce = int(time.time() * 1000) % 1_000_000_000 if nonce is None else nonce
        if not 0 <= self.nonce < 1_000_000_000:
            raise ValueError("nonce must be an unsigned SDK timestamp below 1,000,000,000")
        self.random_user = _random32() if random_user is None else bytes(random_user)
        self.random_password = _random32() if random_password is None else bytes(random_password)
        if len(self.random_user) != 32 or len(self.random_password) != 32:
            raise ValueError("login random arrays must each contain 32 bytes")
        digest = hashlib.md5(str(self.nonce).encode() + self.password + self.uid + self.username)
        base_key = digest.hexdigest()[:16].encode("ascii")
        self.key = encrypt(base_key, (self.uid + self.password)[:32].ljust(32, b"0"))[:16]
        self.extended = False

    def login_payload(self):
        """Return pre-outer-encryption login688; checksum is sum(result) & 65535."""
        user = _credential(self.key, self.uid, self.username, self.random_user, 0)
        password = _credential(self.key, self.uid, self.password, self.random_password, 1)
        return bytes(4) + user + bytes(22) + password + bytes(22)

    def encrypt(self, data):
        return encrypt(self.key, data, self.extended)

    def decrypt(self, data):
        return decrypt(self.key, data, self.extended)

    def enable_v22(self):
        """Call when the successful login response reports password flag 10."""
        self.key = negotiated_key(self.uid, self.username, self.password,
                                  self.random_user, self.random_password)
        self.extended = True

    def command(self, command_id, payload=b"", *, login=False):
        """Build an application frame; PPPP transport framing is separate.

        Use login_payload() as payload and login=True for 0x1000 or 0x481b.
        Checksum covers the pre-outer-encryption payload only for login commands.
        """
        if len(payload) > 10240:
            raise ValueError("command payload exceeds the SDK limit")
        checksum = sum(payload) & 0xffff if login else 0
        header = struct.pack("<IIIiBHBI", 0x99999999, len(payload), command_id, 0,
                             10 if login else 0, checksum, 0, self.nonce if login else 0)
        return header + self.encrypt(payload)
