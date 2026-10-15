"""Infer the camera's fixed frame rate from inner HXVF millisecond timestamps."""
from fractions import Fraction
from statistics import median


def frame_rate(timestamps):
    """Return a rational FPS, rejecting resets, duplicates and isolated gaps."""
    points = list(timestamps)
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in points):
        raise ValueError('Video timestamps must be uint32 milliseconds')
    # Wrap at 2**32 is normal. A backward reset gives a huge invalid interval.
    deltas = [(b-a) & 0xffffffff for a, b in zip(points, points[1:])]
    deltas = [delta for delta in deltas if 8 <= delta <= 2000]
    if len(deltas) < 3:
        raise ValueError('Not enough camera timestamp intervals')
    middle = median(deltas)
    # Ignore an initial stale frame or a lost frame, without relying on network
    # arrival times; relay traffic can arrive in bursts.
    deltas = [delta for delta in deltas if middle * .5 <= delta <= middle * 1.5]
    if len(deltas) < 3:
        raise ValueError('Camera timestamp intervals are inconsistent')
    rate = Fraction(1000 * len(deltas), sum(deltas))
    # Millisecond rounding must not give the two 15 FPS lenses different clocks.
    nearest = round(rate)
    if nearest and abs(rate-nearest) / nearest < .005:
        return Fraction(nearest)
    return rate
