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

"""Oscilloscope-style edge trigger for the Waveform widget.

The Waveform widget scans the raw samples of the trigger source signal
from the stream buffer, in order, as they arrive.  EdgeTrigger finds
the level crossings with hysteresis and holdoff.  BlockAverage optionally
averages the samples per plot pixel column first, so that the trigger
only fires on edges that the displayed trace shows.  TriggerScanner decides
which sample range to request next.  TriggerHistory keeps the recent
trigger times so that the widget can display the newest trigger whose
post-trigger window is fully available.

This module has no Qt dependency so that it can be tested directly.
"""

from collections import deque
import numpy as np
import re


MODES = ('off', 'auto', 'normal', 'single')
EDGES = ('rising', 'falling', 'either')
DIGITAL_QUANTITIES = ('0', '1', '2', '3', 'T')
DIGITAL_LEVEL = 0.5

SCAN_CHUNK_SAMPLES = 1 << 20     # maximum samples per scan request
SCAN_BACKLOG_SAMPLES = 1 << 23   # skip ahead when the scan falls this far behind
SCAN_TIMEOUT_S = 1.0             # re-request when a scan response never arrives
AUTO_TIMEOUT_S = 0.25            # auto mode free-runs without a trigger for this long
HISTORY_LENGTH = 4096            # maximum retained trigger times

_SI_PREFIX = {
    'f': 1e-15, 'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6,
    'm': 1e-3, '': 1.0, 'k': 1e3, 'M': 1e6, 'G': 1e9,
}
_SI_PATTERN = re.compile(
    r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([fpnuµμmkMG]?)\s*([A-Za-z]*)\s*$')


def si_parse(text, units=None):
    """Parse a value with an optional SI prefix and units.

    :param text: The text, such as '1.5', '-20u', '3.3 V' or '150 mA'.
    :param units: The optional expected units, such as 'A'.  When provided,
        the text may include these units.
    :return: The float value.
    :raise ValueError: If text is not a valid value.

    A lone 'm' is milli, not meters.  Units other than the expected units
    are rejected.
    """
    m = _SI_PATTERN.match(str(text))
    if m is None:
        raise ValueError(f'invalid value: {text!r}')
    number, prefix, suffix = m.groups()
    if suffix and suffix != (units or ''):
        raise ValueError(f'invalid units: {text!r}')
    return float(number) * _SI_PREFIX[prefix]


def is_digital(quantity):
    return quantity in DIGITAL_QUANTITIES


def average_increment(duration_s, fs, width):
    """Compute the samples per plot pixel column for BlockAverage.

    :param duration_s: The view duration in seconds.
    :param fs: The sample rate in Hz.
    :param width: The plot width in pixels.
    :return: The samples per pixel rounded to an integer, or 0 when the
        view has fewer than 2 samples per pixel.  The plot then draws
        individual samples, so the trigger uses every sample.
    """
    if width < 2 or duration_s <= 0 or fs <= 0:
        return 0
    spp = duration_s * fs / (width - 1)
    if spp < 1.999:  # tolerate time64 rounding
        return 0
    return int(np.floor(spp + 0.5))


def _schmitt(x, lo, hi, state0):
    """Compute the Schmitt trigger state for each sample.

    :param x: The sample values.
    :param lo: The low threshold.  Samples <= lo are low (0).
    :param hi: The high threshold.  Samples > hi are high (1).
    :param state0: The state before x[0]: 0, 1, or -1 for unknown.
    :return: The int8 state array, which is 0, 1, or -1 for unknown.

    Samples between the thresholds, and NaN samples, hold the previous state.
    """
    v = np.full(len(x), -1, dtype=np.int8)
    with np.errstate(invalid='ignore'):
        v[x > hi] = 1
        v[x <= lo] = 0
    known = v >= 0
    idx = np.where(known, np.arange(len(x)), -1)
    np.maximum.accumulate(idx, out=idx)
    state = np.where(idx >= 0, v[np.maximum(idx, 0)], np.int8(state0)).astype(np.int8)
    return state


class EdgeTrigger:
    """Find edge triggers in a sample stream.

    :param edge: The edge type: 'rising', 'falling', or 'either'.
    :param level: The trigger level in signal units.
    :param hysteresis: The non-negative hysteresis in signal units.
        A rising edge arms when the signal falls to level - hysteresis.
        A falling edge arms when the signal rises above level + hysteresis.
    :param holdoff: The minimum duration, in samples, from one accepted
        trigger to the next.  0 accepts every trigger.

    Call process() with consecutive sample blocks.  The detector keeps
    its state across blocks, so edges that span blocks are found, and
    holdoff applies across blocks.
    """

    def __init__(self, edge='rising', level=0.0, hysteresis=0.0, holdoff=0.0):
        self._edge = 'rising'
        self._level = 0.0
        self._hysteresis = 0.0
        self._holdoff = 0.0
        self.configure(edge, level, hysteresis, holdoff)
        self.reset()

    @property
    def edge(self):
        return self._edge

    @property
    def level(self):
        return self._level

    @property
    def hysteresis(self):
        return self._hysteresis

    @property
    def holdoff(self):
        return self._holdoff

    def configure(self, edge, level, hysteresis=0.0, holdoff=0.0):
        """Configure the trigger and reset the detection state."""
        edge = str(edge).lower()
        if edge not in EDGES:
            raise ValueError(f'invalid edge: {edge}')
        self._edge = edge
        self._level = float(level)
        self.hysteresis = hysteresis
        self.holdoff = holdoff
        self.reset()

    @hysteresis.setter
    def hysteresis(self, value):
        """Change the hysteresis without resetting the detection state."""
        value = float(value)
        if not np.isfinite(value) or value < 0:
            value = 0.0
        self._hysteresis = value

    @holdoff.setter
    def holdoff(self, value):
        """Change the holdoff in samples without resetting the detection state."""
        value = float(value)
        if not np.isfinite(value) or value < 0:
            value = 0.0
        self._holdoff = value

    def reset(self):
        """Reset the detection state, including holdoff."""
        self.state_reset()
        self._holdoff_next = -np.inf  # next allowed trigger position

    def state_reset(self):
        """Reset the edge state, such as for a sample gap.  Keep holdoff."""
        self._sample_next = None    # the next expected sample id
        self._x_last = np.nan       # the last sample value
        self._state_up = -1         # rising edge Schmitt state
        self._state_down = -1       # falling edge Schmitt state

    def _edges(self, x, x_prev, lo, hi, state0, direction):
        """Find the edges in one direction.

        :return: (positions, state_last).  The positions are the
            fractional sample offsets relative to x[0], where -1 is x_prev.
        """
        state = _schmitt(x, lo, hi, state0)
        prev = np.empty(len(state), dtype=np.int8)
        prev[0] = state0
        prev[1:] = state[:-1]
        if direction > 0:
            k = np.nonzero((prev == 0) & (state == 1))[0]
        else:
            k = np.nonzero((prev == 1) & (state == 0))[0]
        if not len(k):
            return np.empty(0, dtype=np.float64), int(state[-1])
        x1 = x[k].astype(np.float64)
        x0 = np.where(k > 0, x[np.maximum(k - 1, 0)], x_prev).astype(np.float64)
        dx = x1 - x0
        with np.errstate(invalid='ignore', divide='ignore'):
            frac = (self._level - x0) / dx
        frac = np.where(np.isfinite(frac), np.clip(frac, 0.0, 1.0), 1.0)
        return k - 1 + frac, int(state[-1])

    def process(self, sample_id, samples):
        """Process a block of samples.

        :param sample_id: The sample id of samples[0].
        :param samples: The 1-D sample values.
        :return: The np.float64 array of accepted trigger positions,
            as fractional sample ids, in increasing order.
        """
        samples = np.asarray(samples)
        sample_id = int(sample_id)
        if self._sample_next is not None:
            if sample_id < self._sample_next:  # overlap: skip processed samples
                skip = self._sample_next - sample_id
                samples = samples[skip:]
                sample_id += skip
            elif sample_id > self._sample_next:  # gap: the edge state is unknown
                self.state_reset()
        if not len(samples):
            return np.empty(0, dtype=np.float64)
        if samples.dtype.kind not in 'f':
            samples = samples.astype(np.float32)
        level, h = self._level, self._hysteresis
        x_prev = self._x_last
        positions = []
        if self._edge in ('rising', 'either'):
            p, self._state_up = self._edges(samples, x_prev, level - h, level, self._state_up, 1)
            positions.append(p)
        if self._edge in ('falling', 'either'):
            p, self._state_down = self._edges(samples, x_prev, level, level + h, self._state_down, -1)
            positions.append(p)
        self._x_last = samples[-1]
        self._sample_next = sample_id + len(samples)
        if len(positions) == 1:
            p = positions[0]
        else:
            p = np.sort(np.concatenate(positions))
        if not len(p):
            return p
        return self._holdoff_apply(p + sample_id)

    def _holdoff_apply(self, p):
        if self._holdoff <= 0.0:
            p = p[p > self._holdoff_next]
            if len(p):
                self._holdoff_next = p[-1]
            return p
        accepted = []
        idx = int(np.searchsorted(p, self._holdoff_next, side='left'))
        while idx < len(p):
            t = p[idx]
            accepted.append(t)
            self._holdoff_next = t + self._holdoff
            idx = max(idx + 1, int(np.searchsorted(p, self._holdoff_next, side='left')))
        return np.array(accepted, dtype=np.float64)


class BlockAverage:
    """Average a sample stream over a fixed grid of sample blocks.

    :param incr: The samples per block.  Block b contains the samples
        b * incr to (b + 1) * incr - 1.  Values less than 2 disable
        averaging.

    The waveform plot summarizes each pixel column from consecutive
    samples, and draws the average as the trace.  A short spike that
    crosses the trigger level can vanish in this average.  Triggering on
    the block averages, with the same samples per block as the plot,
    only triggers on edges that the average trace shows.

    Call process() with consecutive sample blocks.  A partial block
    continues across calls.  The first block after reset() or a gap
    starts on the next grid boundary, since a partial block average
    differs from the displayed average.  NaN samples are excluded from
    the average, and a block with only NaN samples averages to NaN.
    """

    def __init__(self, incr=0):
        self._incr = 0
        self.incr = incr

    @property
    def incr(self):
        return self._incr

    @incr.setter
    def incr(self, value):
        """Change the samples per block and reset."""
        value = int(value)
        self._incr = value if value >= 2 else 0
        self.reset()

    @property
    def enabled(self):
        return self._incr >= 2

    def reset(self):
        self._sample_next = None    # the next expected sample id
        self._sum = 0.0             # the partial block sum of finite samples
        self._count = 0             # the partial block finite sample count

    def _accumulate(self, x):
        finite = np.isfinite(x)
        self._sum += float(np.sum(x, where=finite))
        self._count += int(np.count_nonzero(finite))

    def _pop(self):
        v = self._sum / self._count if self._count else np.nan
        self._sum, self._count = 0.0, 0
        return v

    def process(self, sample_id, samples):
        """Process a block of samples.

        :param sample_id: The sample id of samples[0].
        :param samples: The 1-D sample values.
        :return: (block_id, averages) for the blocks that these samples
            complete, where block_id is the id of averages[0].  The
            block ids are consecutive across calls without a gap.
        """
        incr = self._incr
        if not incr:
            raise RuntimeError('BlockAverage disabled')
        samples = np.asarray(samples, dtype=np.float64)
        sample_id = int(sample_id)
        if self._sample_next is not None:
            if sample_id < self._sample_next:  # overlap: skip processed samples
                skip = self._sample_next - sample_id
                samples = samples[skip:]
                sample_id += skip
            elif sample_id > self._sample_next:  # gap: discard the partial block
                self.reset()
        if self._sample_next is None:  # start on the grid
            skip = (-sample_id) % incr
            samples = samples[skip:]
            sample_id += skip
            self._sample_next = sample_id
        empty = np.empty(0, dtype=np.float64)
        n = len(samples)
        if not n:
            return sample_id // incr, empty
        self._sample_next = sample_id + n
        block_id, offset = divmod(sample_id, incr)
        values = []
        idx = 0
        if offset:  # continue the partial block
            idx = min(n, incr - offset)
            self._accumulate(samples[:idx])
            if offset + idx < incr:
                return block_id + 1, empty
            values.append(np.array([self._pop()]))
        m = (n - idx) // incr
        if m:
            x = samples[idx:idx + m * incr].reshape(m, incr)
            finite = np.isfinite(x)
            s = np.sum(x, axis=1, where=finite)
            c = np.count_nonzero(finite, axis=1)
            with np.errstate(invalid='ignore', divide='ignore'):
                values.append(np.where(c > 0, s / np.maximum(c, 1), np.nan))
            idx += m * incr
        if idx < n:
            self._accumulate(samples[idx:])
        if not values:
            return block_id, empty
        return block_id, np.concatenate(values)


class TriggerHistory:
    """The recent accepted trigger times, in increasing order."""

    def __init__(self, maxlen=None):
        self._t = deque(maxlen=HISTORY_LENGTH if maxlen is None else int(maxlen))

    def __len__(self):
        return len(self._t)

    def clear(self):
        self._t.clear()

    def extend(self, times):
        for t in times:
            t = int(t)
            if len(self._t) and t <= self._t[-1]:
                continue  # keep strictly increasing
            self._t.append(t)

    def latest(self, t_max=None):
        """Get the newest trigger time.

        :param t_max: When provided, the newest trigger time <= t_max.
        :return: The trigger time or None.
        """
        for t in reversed(self._t):
            if t_max is None or t <= t_max:
                return t
        return None

    def prune(self, t_min):
        """Remove trigger times before t_min, such as evicted data."""
        while len(self._t) and self._t[0] < t_min:
            self._t.popleft()


class TriggerScanner:
    """Track the scan through the trigger source signal.

    The scanner requests one sample range at a time.  The widget sends
    the request, then provides the response samples to on_response().
    """

    def __init__(self):
        self.detector = EdgeTrigger()
        self.average = BlockAverage()
        self.history = TriggerHistory()
        self._sample_next = None
        self._pending = None         # the monotonic request time
        self._request_start = None   # the requested starting sample id

    @property
    def sample_next(self):
        return self._sample_next

    @property
    def is_pending(self):
        return self._pending is not None

    def reset(self, start=None):
        """Restart scanning.

        :param start: The optional sample id to start scanning.  None
            starts with a lookback from the newest sample on the next request.
        """
        self.detector.reset()
        self.average.reset()
        self.history.clear()
        self._sample_next = None if start is None else int(start)
        self._pending = None
        self._request_start = None

    def request(self, buf_start, buf_end, lookback, now):
        """Compute the next scan request.

        :param buf_start: The oldest sample id available in the buffer.
        :param buf_end: The newest sample id available in the buffer.
        :param lookback: The number of samples to scan before buf_end
            when starting or when skipping ahead.
        :param now: The current monotonic time in seconds.
        :return: (start, end) inclusive sample ids to request, or None.
        """
        if self._pending is not None:
            if (now - self._pending) < SCAN_TIMEOUT_S:
                return None
            self._pending = None  # lost response
        buf_start, buf_end, lookback = int(buf_start), int(buf_end), max(0, int(lookback))
        if buf_end < buf_start:
            return None
        backlog_max = max(lookback, SCAN_BACKLOG_SAMPLES)
        k = self._sample_next
        if k is None or k < buf_start or (buf_end - k) > backlog_max:
            k = max(buf_start, buf_end - lookback)
            self.state_reset()
            self._sample_next = k
        if k > buf_end:
            return None
        end = min(buf_end, k + SCAN_CHUNK_SAMPLES - 1)
        self._pending = now
        self._request_start = k
        return k, end

    def is_stale(self, sample_id):
        """Check if a response does not match the pending request.

        :param sample_id: The response starting sample id.
        :return: True when no request is pending, or the response starts
            before the pending request, such as a response to a request
            made before reset().  A later start is valid: the buffer
            evicted the requested start.
        """
        return self._pending is None or int(sample_id) < self._request_start

    def on_response(self, sample_id, samples):
        """Process the response for the pending request.

        :param sample_id: The sample id for samples[0].
        :param samples: The raw sample values.
        :return: The accepted trigger positions as fractional sample ids,
            or None when the response is stale and ignored.
        """
        if self.is_stale(sample_id):
            return None
        self._pending = None
        if self.average.enabled:
            # Detect on the block averages, then convert the fractional
            # block positions to sample ids.  Each block average is at
            # its first sample, like the plot points.
            block_id, values = self.average.process(sample_id, samples)
            positions = self.detector.process(block_id, values) * self.average.incr
        else:
            positions = self.detector.process(sample_id, samples)
        self._sample_next = max(self._sample_next or 0, int(sample_id) + len(samples))
        return positions

    def state_reset(self):
        """Reset the edge and averaging state, such as for a sample gap."""
        self.detector.state_reset()
        self.average.reset()

    def on_response_invalid(self, sample_next=None):
        """Skip an unusable response, such as a summary.

        :param sample_next: The optional sample id to continue scanning.
        """
        self._pending = None
        self.state_reset()
        if sample_next is not None:
            self._sample_next = int(sample_next)


def trigger_window(t, dt, position):
    """Compute the x-axis range that places a trigger time at position.

    :param t: The trigger time in time64.
    :param dt: The view duration in time64.
    :param position: The trigger position as a fraction of the view, 0 to 1.
    :return: [x0, x1].
    """
    x0 = int(t) - int(round(position * dt))
    return [x0, x0 + int(dt)]


def trigger_select(history, e1, dt, position):
    """Select the newest trigger whose post-trigger window is available.

    :param history: The TriggerHistory.
    :param e1: The newest available time in time64.
    :param dt: The view duration in time64.
    :param position: The trigger position as a fraction of the view, 0 to 1.
    :return: The trigger time or None.
    """
    t_max = int(e1) - int(round((1.0 - position) * dt))
    return history.latest(t_max)
