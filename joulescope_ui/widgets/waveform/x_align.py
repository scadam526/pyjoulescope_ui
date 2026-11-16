# Copyright 2026 Jetperch LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Align waveform summary requests to a fixed sample grid.

The signal buffers summarize each point from ``incr`` consecutive samples,
starting at the requested sample.  When the request start follows the view
continuously, the samples in each point change every frame as the view
scrolls.  Peaks then jump between points and the trace flickers (aliasing).

Starting each request on a multiple of ``incr`` keeps the samples in each
point fixed.  Drawing exactly one point per pixel column, with the drawn
range on the same grid, makes scrolling move the trace by whole pixels.
The drawn duration is then a whole number of points, which can differ
slightly from the requested view duration.
"""

import math
from pyjoulescope_driver import time64


ANCHORS = ('left', 'center', 'right')


def linear_map(time_range_utc, time_range_samples, sample_rate=None):
    """Construct a local linear map between time64 and sample ids.

    :param time_range_utc: The dict with 'start' and 'end' time64.
    :param time_range_samples: The dict with 'start' and 'end' sample ids,
        corresponding to time_range_utc.
    :param sample_rate: The sample rate in Hz, used when the ranges
        are too short to compute the rate.
    :return: The (t0, s0, k) map where sample_id = s0 + (t - t0) * k,
        or None when unavailable.
    """
    try:
        t0, t1 = int(time_range_utc['start']), int(time_range_utc['end'])
        s0, s1 = int(time_range_samples['start']), int(time_range_samples['end'])
    except (KeyError, TypeError, ValueError):
        return None
    if t1 > t0 and s1 > s0:
        k = (s1 - s0) / (t1 - t0)
    elif sample_rate:
        k = float(sample_rate) / time64.SECOND
    else:
        return None
    if not math.isfinite(k) or k <= 0:
        return None
    return t0, s0, k


def increment(duration, k, width):
    """Compute the samples per point for a view.

    :param duration: The view duration in time64.
    :param k: The samples per time64 from :func:`linear_map`.
    :param width: The view width in pixels.
    :return: The samples per pixel rounded to an integer, or 0 when the
        view has fewer than 2 samples per pixel.  The buffers then provide
        individual samples, which do not alias.
    """
    if width < 2 or duration <= 0:
        return 0
    spp = duration * k / (width - 1)
    if spp < 1.999:  # tolerate time64 rounding
        return 0
    return int(math.floor(spp + 0.5))


def duration(incr, k, width):
    """Compute the drawn duration for one point per pixel.

    :param incr: The samples per point from :func:`increment`.
    :param k: The samples per time64 from :func:`linear_map`.
    :param width: The view width in pixels.
    :return: The duration in time64 from the first to the last point.
    """
    return int(round(incr * (width - 1) / k))


def align(x_range, tmap, incr, length, anchor='center', match=False, sample_range=None):
    """Align a view to the sample grid.

    :param x_range: The [x0, x1] view range in time64.
    :param tmap: The (t0, s0, k) map from :func:`linear_map`.
    :param incr: The samples per point from :func:`increment`.
    :param length: The number of points, which is the width in pixels.
    :param anchor: The x_range position that the aligned view keeps:
        'left', 'center' or 'right'.  'right' keeps the newest complete
        point at the right edge.
    :param match: False to align x_range to the grid point at or before
        the anchor.  True when x_range is another signal's already
        aligned view: use the nearest grid point, which tolerates the
        small differences between the signal time maps.
    :param sample_range: The optional (start, end) inclusive sample ids
        that are available.  The request excludes points that extend
        beyond the available samples, which the buffer would otherwise
        summarize from partial data.  Unless match, a 'right' view that
        ends less than one point past the available samples aligns to the
        newest complete point.
    :return: (start, length, [d0, d1]) for a request of length points of
        incr samples from sample id start, and the aligned view range
        from the first to the last point.  None when the view does not
        contain a complete point.
    """
    t0, s0, k = tmap
    x0, x1 = int(x_range[0]), int(x_range[1])
    if incr < 2 or length < 2 or x1 <= x0:
        return None
    if anchor not in ANCHORS:
        raise ValueError(f'invalid anchor: {anchor}')

    def to_sample(t):
        return s0 + (t - t0) * k

    def to_time(s):
        return t0 + int(round((s - s0) / k))

    def snap(s):
        if match:
            return int(round(s / incr)) * incr
        # Half-sample bias: a view edge on a grid point stays on it despite
        # the rounding error of the time map.
        return int(math.floor((s + 0.5) / incr)) * incr

    span = (length - 1) * incr  # samples from the first to the last point
    if anchor == 'right':
        end = to_sample(x1) + 1  # exclusive
        if sample_range is not None and not match:
            # Streaming: the view ends at the newest sample.  Only a view
            # beyond the data by more than one point (Δt holdover) stays put.
            overshoot = end - (int(sample_range[1]) + 1)
            if 0 < overshoot <= incr:
                end = int(sample_range[1]) + 1
        start = snap(end) - length * incr
    elif anchor == 'center':
        start = snap(to_sample((x0 + x1) // 2) - span / 2)
    else:
        start = snap(to_sample(x0))
    view = [to_time(start), to_time(start + span)]

    if sample_range is not None:
        a0, a1 = int(sample_range[0]), int(sample_range[1])
        if start < a0:
            skip = -((start - a0) // incr)  # ceil
            start += skip * incr
            length -= skip
        excess = start + length * incr - (a1 + 1)
        if excess > 0:
            length -= -(-excess // incr)  # ceil
    if length < 1:
        return None
    return start, length, view
