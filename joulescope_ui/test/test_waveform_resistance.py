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

"""Test the WaveformWidget resistance plot, a derived signal."""

import copy
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6 import QtWidgets

from joulescope_ui.widgets.waveform import waveform_widget
from joulescope_ui.widgets.waveform.waveform_widget import WaveformWidget, _marker_to_rsp_id, \
    _MARKER_RSP_DERIVED_NUM, _MARKER_RSP_DERIVED_DEN, _STATE_DEFAULT, _state_plots_migrate


_SUB = 'JsdrvStreamBuffer:001.JS220-000001'


class _PubSub:

    def __init__(self):
        self.published = []

    def publish(self, topic, value, **kwargs):
        self.published.append((topic, value))

    def query(self, topic, default=None):
        return default


def _data(x, avg):
    n = len(x)
    return {
        'x': np.asarray(x, dtype=np.int64),
        'avg': np.asarray(avg, dtype=np.float32),
        'std': None, 'min': None, 'max': None,
        'time_range_utc': {'start': int(x[0]), 'end': int(x[-1]), 'length': n},
        'time_range_samples': {'start': 0, 'end': n - 1, 'length': n},
    }


class TestStateMigrate(unittest.TestCase):

    def test_adds_resistance_after_power(self):
        old = copy.deepcopy(_STATE_DEFAULT)
        old['plots'] = [p for p in old['plots'] if p['quantity'] != 'z']
        old['plots'][1]['enabled'] = False  # keep user changes
        state = _state_plots_migrate(old)
        quantities = [p['quantity'] for p in state['plots']]
        self.assertEqual([p['quantity'] for p in _STATE_DEFAULT['plots']], quantities)
        self.assertEqual('z', quantities[3])
        self.assertFalse(state['plots'][1]['enabled'])
        self.assertFalse(state['plots'][3]['enabled'])  # new plot hidden by default
        self.assertEqual(9, len(old['plots']))  # input unchanged

    def test_complete_state_unchanged(self):
        self.assertIsNone(_state_plots_migrate(copy.deepcopy(_STATE_DEFAULT)))


class TestWaveformResistance(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        w = WaveformWidget(None)
        w.unique_id = 'WaveformWidget:test'
        w.pubsub = _PubSub()
        w.topic = 'registry/WaveformWidget:test'
        self.widget = w

    def tearDown(self):
        self.widget.close()

    def _range(self, quantity, r):
        topic = f'registry/JsdrvStreamBuffer:001/settings/signals/JS220-000001.{quantity}/range'
        value = None if r is None else {'utc': r, 'samples': {'start': 0, 'end': 100, 'length': 100},
                                        'sample_rate': 1e6}
        self.widget._on_signal_range(topic, value)

    def test_derived_signal_tracks_sources(self):
        w = self.widget
        self._range('i', [100, 1000])
        self.assertNotIn(f'{_SUB}.z', w._signals)
        self._range('v', [200, 900])
        z = w._signals[f'{_SUB}.z']
        self.assertEqual(('v', 'i'), z['derived'])
        self.assertEqual([200, 900], z['range'])
        self._range('v', [0, 0])  # voltage removed
        self.assertNotIn(f'{_SUB}.z', w._signals)

    def test_derived_data(self):
        w = self.widget
        self._range('i', [100, 1000])
        self._range('v', [100, 1000])
        x = [100, 200, 300]
        w._signals_data[f'{_SUB}.i'] = {'data': _data(x, [0.5, 0.0, 2.0])}
        w._derived_data_update(f'{_SUB}.i')
        self.assertNotIn(f'{_SUB}.z', w._signals_data)  # voltage not yet available
        w._signals_data[f'{_SUB}.v'] = {'data': _data(x, [5.0, 1.0, 3.0])}
        w._derived_data_update(f'{_SUB}.v')
        r = w._signals_data[f'{_SUB}.z']['data']['avg']
        np.testing.assert_allclose([10.0, np.nan, 1.5], r)

    def test_derived_not_requested_from_buffer(self):
        w = self.widget
        self._range('i', [100, 1000])
        self._range('v', [100, 1000])
        def requests():
            return [v for t, v in w.pubsub.published if t.endswith('/actions/!request')]
        w._request_signal(f'{_SUB}.z', [100, 1000], length=10)
        self.assertEqual([], requests())
        w._request_signal(f'{_SUB}.v', [100, 1000], length=10)
        self.assertEqual(['JS220-000001.v'], [r['signal_id'] for r in requests()])

    def test_dual_marker_data(self):
        w = self.widget
        key = (2, 3)  # marker 2, resistance plot
        w._marker_derived[key] = {}
        w._marker_data[key] = None
        num = _data([500], [6.0])
        den = _data([500], [2.0])
        w._marker_derived_response(2, _MARKER_RSP_DERIVED_NUM + 3, num)
        self.assertIsNone(w._marker_data[key])  # waiting for current
        w._marker_derived_response(2, _MARKER_RSP_DERIVED_DEN + 3, den)
        np.testing.assert_allclose([3.0], w._marker_data[key]['avg'])
        self.assertNotIn(key, w._marker_derived)

    def test_marker_rsp_ids_distinct(self):
        ids = {_marker_to_rsp_id(1, p) for p in range(len(_STATE_DEFAULT['plots']))}
        ids |= {_marker_to_rsp_id(1, _MARKER_RSP_DERIVED_NUM + p) for p in range(len(_STATE_DEFAULT['plots']))}
        ids |= {_marker_to_rsp_id(1, _MARKER_RSP_DERIVED_DEN + p) for p in range(len(_STATE_DEFAULT['plots']))}
        self.assertEqual(3 * len(_STATE_DEFAULT['plots']), len(ids))
        self.assertLess(_MARKER_RSP_DERIVED_DEN + len(_STATE_DEFAULT['plots']), waveform_widget._MARKER_RSP_STEP)

    def test_buffer_options_exclude_derived(self):
        for setting in ['summary_quantity', 'trigger_source']:
            options = [o[0] for o in WaveformWidget.SETTINGS[setting]['options']]
            self.assertNotIn('z', options, setting)
            self.assertIn('p', options, setting)


if __name__ == '__main__':
    unittest.main()
