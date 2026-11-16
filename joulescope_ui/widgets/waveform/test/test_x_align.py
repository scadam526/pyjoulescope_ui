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

"""Test joulescope_ui.widgets.waveform.x_align."""

import unittest
import numpy as np
from pyjoulescope_driver import time64
from joulescope_ui.widgets.waveform import x_align


FS = 1_000_000
T0 = 1000 * time64.SECOND
S0 = 5_000_000_000  # streaming sample ids are large
K = FS / time64.SECOND
TMAP = (T0, S0, K)
WIDTH = 200


def _t(s):
    """Convert a sample id to time64 with TMAP."""
    return T0 + int(round((s - S0) / K))


def _s(t):
    """Convert a time64 to a fractional sample id with TMAP."""
    return S0 + (t - T0) * K


def _view(x0, incr, width=WIDTH):
    """A view starting at x0 with incr samples per pixel."""
    return [x0, x0 + int(incr * (width - 1) / K)]


def _summary(data, start, length, incr):
    """Summarize like the stream buffer: max of incr samples from start."""
    return data[start:start + length * incr].reshape((length, incr)).max(axis=1)


class TestLinearMap(unittest.TestCase):

    def test_from_ranges(self):
        utc = {'start': T0, 'end': T0 + time64.SECOND}
        samples = {'start': S0, 'end': S0 + FS}
        t0, s0, k = x_align.linear_map(utc, samples)
        self.assertEqual((T0, S0), (t0, s0))
        self.assertAlmostEqual(K, k)

    def test_single_sample_uses_sample_rate(self):
        utc = {'start': T0, 'end': T0}
        samples = {'start': S0, 'end': S0}
        self.assertAlmostEqual(K, x_align.linear_map(utc, samples, FS)[2])

    def test_unavailable(self):
        utc = {'start': T0, 'end': T0}
        samples = {'start': S0, 'end': S0}
        self.assertIsNone(x_align.linear_map(utc, samples))
        self.assertIsNone(x_align.linear_map(None, samples, FS))
        self.assertIsNone(x_align.linear_map({'start': T0}, samples, FS))


class TestIncrement(unittest.TestCase):

    def test_rounds(self):
        for spp, incr in [(2.0, 2), (2.4, 2), (2.6, 3), (48.8, 49), (1000.2, 1000)]:
            duration = int(round(spp * (WIDTH - 1) / K))
            self.assertEqual(incr, x_align.increment(duration, K, WIDTH), spp)

    def test_samples_below_2_per_pixel(self):
        duration = int(1.9 * (WIDTH - 1) / K)
        self.assertEqual(0, x_align.increment(duration, K, WIDTH))

    def test_invalid(self):
        self.assertEqual(0, x_align.increment(time64.SECOND, K, 1))
        self.assertEqual(0, x_align.increment(0, K, WIDTH))

    def test_duration(self):
        d = x_align.duration(50, K, WIDTH)
        self.assertAlmostEqual(50 * (WIDTH - 1), d * K, places=3)


class TestAlign(unittest.TestCase):

    def test_one_point_per_pixel(self):
        incr = 100
        x_range = _view(_t(S0 + 12_345), incr)
        for anchor in x_align.ANCHORS:
            start, length, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor=anchor)
            self.assertEqual(0, start % incr)
            self.assertEqual(WIDTH, length)
            self.assertEqual([_t(start), _t(start + (WIDTH - 1) * incr)], view)

    def test_anchor_left(self):
        incr = 100
        x_range = _view(_t(S0 + 12_345), incr)
        start, _, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor='left')
        self.assertEqual(S0 + 12_300, start)

    def test_anchor_center(self):
        incr = 100
        x0 = _t(S0 + 12_345)
        x_range = [x0, x0 + int(incr * 1.2 * (WIDTH - 1) / K)]  # 20% longer
        _, _, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor='center')
        center = (x_range[0] + x_range[1]) // 2
        self.assertLessEqual(abs((view[0] + view[1]) // 2 - center), incr / K)

    def test_scrolling_moves_whole_pixels(self):
        """Each pixel shows the same samples as the view scrolls."""
        incr = 37
        rng = np.random.default_rng(1)
        data = rng.normal(size=500_000)
        values = {}  # absolute point start -> value
        prev = None
        for offset in range(0, 50_000, 13):  # scroll by sub-pixel amounts
            x_range = _view(_t(S0 + 1000 + offset), incr)
            start, length, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor='left')
            self.assertLessEqual(view[0], x_range[0])
            self.assertLess(x_range[0] - view[0], incr / K)  # within one pixel
            k = start - S0
            for idx, v in enumerate(_summary(data, k, length, incr)):
                key = k + idx * incr
                self.assertEqual(values.setdefault(key, v), v)
            if prev is not None:
                shift = (start - prev) / incr
                self.assertEqual(int(shift), shift)  # whole pixels
            prev = start

    def test_edge_on_grid_is_stable(self):
        """A view edge on a grid point survives time map rounding."""
        incr = 10
        for s in [S0 + 1000, S0 + 1000 - 0.3, S0 + 1000 + 0.3]:
            x0 = T0 + int(round((s - S0) / K))
            start, _, _ = x_align.align(_view(x0, incr), TMAP, incr, WIDTH, anchor='left')
            self.assertEqual(S0 + 1000, start)

    def test_anchor_right_newest_complete_point(self):
        incr = 50
        newest = S0 + 123_456  # inclusive
        x1 = _t(newest)
        x_range = [x1 - int(incr * (WIDTH - 1) / K), x1]
        start, length, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor='right',
                                            sample_range=(S0, newest))
        end = start + length * incr  # exclusive
        self.assertEqual(WIDTH, length)
        self.assertEqual(0, end % incr)
        self.assertLessEqual(end - 1, newest)
        self.assertGreater(end + incr - 1, newest)  # the next point is incomplete
        self.assertEqual(_t(end - incr), view[1])  # last point at the right edge

    def test_anchor_right_view_slightly_past_data(self):
        """Streaming: the view end can lead the buffered samples."""
        incr = 50
        newest = S0 + 123_456
        x1 = _t(newest + 20)  # less than one point past the data
        x_range = [x1 - int(incr * (WIDTH - 1) / K), x1]
        start, length, _ = x_align.align(x_range, TMAP, incr, WIDTH, anchor='right',
                                         sample_range=(S0, newest))
        self.assertEqual(S0 + 123_450, start + length * incr)

    def test_anchor_right_holdover_past_data(self):
        """Δt holdover: a view far past the data stays put."""
        incr = 50
        newest = S0 + 123_456
        x1 = _t(newest + 5_000)
        x_range = [x1 - int(incr * (WIDTH - 1) / K), x1]
        start, length, view = x_align.align(x_range, TMAP, incr, WIDTH, anchor='right',
                                            sample_range=(S0, newest))
        # The last point starts one to two points before the view end.
        self.assertLess(abs(view[1] - x1), 2 * incr / K)
        self.assertLessEqual(start + length * incr - 1, newest)  # no partial point

    def test_match_with_different_time_map(self):
        """Signals with slightly different time maps request the same points."""
        incr = 100
        x_range = _view(_t(S0 + 54_321), incr)
        tmap = (T0 + 3, S0, K * (1 + 2e-6))
        for anchor in x_align.ANCHORS:
            ref = x_align.align(x_range, TMAP, incr, WIDTH, anchor=anchor)
            rv = x_align.align(ref[2], tmap, incr, WIDTH, anchor='left', match=True)
            self.assertEqual(ref[:2], rv[:2], anchor)

    def test_clip_to_available(self):
        incr = 100
        x_range = _view(_t(S0 + 1050), incr, width=50)
        start, length, _ = x_align.align(x_range, TMAP, incr, 50, anchor='left',
                                         sample_range=(S0 + 1120, S0 + 4000))
        self.assertEqual(S0 + 1200, start)  # first complete point
        start, length, _ = x_align.align(x_range, TMAP, incr, 50, anchor='left',
                                         sample_range=(S0, S0 + 3456))
        self.assertEqual(S0 + 3400, start + length * incr)  # last complete point

    def test_invalid(self):
        x_range = [T0, T0 + time64.MILLISECOND]
        self.assertIsNone(x_align.align(x_range, TMAP, 1, WIDTH))
        self.assertIsNone(x_align.align([T0, T0], TMAP, 10, WIDTH))
        self.assertIsNone(x_align.align(x_range, TMAP, 10, 1))
        self.assertIsNone(x_align.align(x_range, TMAP, 10, WIDTH, sample_range=(S0 + 10**6, S0 + 2 * 10**6)))
        with self.assertRaises(ValueError):
            x_align.align(x_range, TMAP, 10, WIDTH, anchor='middle')
