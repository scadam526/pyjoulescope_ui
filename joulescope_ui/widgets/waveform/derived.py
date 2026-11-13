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

"""Derived signals that the Waveform widget computes from other signals.

The instrument streams current (i), voltage (v), and power (p), which the
stream buffer stores.  The Waveform widget computes derived signals, such
as resistance, from the buffered signals that it already requests for
display.

This module has no Qt dependency so that it can be tested directly.
"""

import numpy as np


DERIVED = {
    # quantity: (numerator quantity, denominator quantity)
    'z': ('v', 'i'),  # resistance R = V / I
}
"""The derived signal quantities, computed as numerator / denominator."""


def is_derived(quantity):
    return quantity in DERIVED


def sources(quantity):
    """Get the (numerator, denominator) quantities for a derived quantity."""
    return DERIVED[quantity]


def ratio(num, den):
    """Compute the ratio of two signal responses on the same time grid.

    :param num: The numerator data dict with keys x, avg, std, min, max,
        time_range_utc, and time_range_samples.
    :param den: The denominator data dict on the same time grid.
    :return: The derived data dict, or None when num and den are not on
        the same time grid, such as responses to different requests.

    For sample data, each value is the instantaneous ratio.  For summary
    data, where each point represents many samples, each value is the
    ratio of the averages, such as average voltage / average current.
    The minimum, maximum, and standard deviation of a ratio cannot be
    computed from summary data, so they are None.  Division by zero
    produces NaN, which displays as missing data.
    """
    xn, xd = num['x'], den['x']
    if len(xn) != len(xd) or not np.array_equal(xn, xd):
        return None
    with np.errstate(divide='ignore', invalid='ignore'):
        y = np.asarray(num['avg'], dtype=np.float64) / np.asarray(den['avg'], dtype=np.float64)
    y[~np.isfinite(y)] = np.nan
    return {
        'x': xn,
        'avg': y,
        'std': None,
        'min': None,
        'max': None,
        'time_range_utc': num.get('time_range_utc'),
        'time_range_samples': num.get('time_range_samples'),
    }
