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

"""Test joulescope_ui.widgets.waveform.derived."""

import unittest
import numpy as np
from joulescope_ui.widgets.waveform import derived


def _data(x, avg, summary=False):
    n = len(x)
    d = {
        'x': np.asarray(x, dtype=np.int64),
        'avg': np.asarray(avg, dtype=np.float32),
        'std': None, 'min': None, 'max': None,
        'time_range_utc': {'start': int(x[0]), 'end': int(x[-1]), 'length': n},
        'time_range_samples': {'start': 0, 'end': n - 1, 'length': n},
    }
    if summary:
        d['std'] = np.zeros(n, dtype=np.float32)
        d['min'] = d['avg'] - 1
        d['max'] = d['avg'] + 1
    return d


class TestDerived(unittest.TestCase):

    def test_resistance_defined(self):
        self.assertTrue(derived.is_derived('z'))
        self.assertFalse(derived.is_derived('p'))
        self.assertEqual(('v', 'i'), derived.sources('z'))

    def test_ratio_samples(self):
        x = [10, 20, 30]
        v = _data(x, [3.3, 5.0, 1.0])
        i = _data(x, [0.1, 0.5, -0.25])
        r = derived.ratio(v, i)
        np.testing.assert_allclose([33.0, 10.0, -4.0], r['avg'], rtol=1e-6)
        np.testing.assert_array_equal(v['x'], r['x'])
        self.assertIsNone(r['min'])
        self.assertIsNone(r['max'])
        self.assertIsNone(r['std'])
        self.assertEqual(v['time_range_utc'], r['time_range_utc'])

    def test_ratio_summary_uses_averages(self):
        x = [10, 20]
        r = derived.ratio(_data(x, [4.0, 6.0], summary=True), _data(x, [2.0, 3.0], summary=True))
        np.testing.assert_allclose([2.0, 2.0], r['avg'])
        self.assertIsNone(r['min'])  # cannot compute from summary data

    def test_ratio_divide_by_zero_is_missing(self):
        x = [10, 20, 30, 40]
        r = derived.ratio(_data(x, [1.0, 0.0, 1.0, np.nan]), _data(x, [0.0, 0.0, 2.0, 1.0]))
        self.assertTrue(np.isnan(r['avg'][0]))  # 1 / 0
        self.assertTrue(np.isnan(r['avg'][1]))  # 0 / 0
        self.assertEqual(0.5, r['avg'][2])
        self.assertTrue(np.isnan(r['avg'][3]))  # missing data

    def test_ratio_mismatched_grid(self):
        self.assertIsNone(derived.ratio(_data([10, 20], [1, 1]), _data([10, 20, 30], [1, 1, 1])))
        self.assertIsNone(derived.ratio(_data([10, 20], [1, 1]), _data([11, 21], [1, 1])))


if __name__ == '__main__':
    unittest.main()
