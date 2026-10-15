"""Audio framing and G.711 A-law decoding helpers."""
import struct


def elementary_audio(payload, codec):
    """Remove the observed optional four-byte prefix from each 20ms block.

    Single blocks belong to the ordinary path; XQ sends five-block batches.
    Unknown lengths/codecs are rejected rather than guessed as speech samples.
    """
    width = {0: 160, 1: 40}.get(codec)
    if width is None:
        raise ValueError('Audio codec not established for this decoder')
    if len(payload) in (width, width * 5):
        return payload
    if len(payload) in (width + 4, (width + 4) * 5):
        return b''.join(payload[i + 4:i + 4 + width]
                        for i in range(0, len(payload), width + 4))
    raise ValueError('Unexpected audio frame length')


def alaw_pcm16(payload):
    """G.711 A-law bytes to mono signed little-endian PCM, sampled at 8kHz."""
    samples = []
    for value in payload:
        value ^= 0x55
        sample = ((value & 15) << 4) + 8
        exponent = (value >> 4) & 7
        if exponent:
            sample = (sample + 0x100) << (exponent - 1)
        samples.append(sample if value & 0x80 else -sample)
    return struct.pack('<' + str(len(samples)) + 'h', *samples)
