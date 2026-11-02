"""`push --update` must not see a rule as changed because Grafana respelled a duration.

A rule file saying `for: 0m`; Grafana stores `0s`. Compared as text, every
`push --update` would rewrite it, and that noise is how real drift went unpushed.
"""

import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

from grafana_push_alert_rules import comparable  # ruff: ignore[module-import-not-at-top-of-file]


def test_a_respelled_duration_is_not_a_change():
    assert comparable('for', '0m') == comparable('for', '0s')
    assert comparable('for', '1h') == comparable('for', '60m')


def test_an_unset_keep_firing_for_equals_grafanas_stored_default():
    assert comparable('keep_firing_for', None) == comparable('keep_firing_for', '0s')


def test_a_different_duration_is_still_a_change():
    assert comparable('for', '5m') != comparable('for', '10m')
