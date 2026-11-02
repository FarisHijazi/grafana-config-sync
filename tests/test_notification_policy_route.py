"""The severity=critical route: `scripts/grafana_push_notification_policy.py`.

Why this exists: a UI-made policy tree often has one child route (here a made-up
`service = network` one), so `severity = critical` rules fall through to the default
receiver and are lost in the noise there.

The script that fixes it replaces the WHOLE tree in one PUT, which is the dangerous
part: a route dropped on the way through is a page nobody gets. These tests pin the
properties that make that safe, and need no Grafana.
"""

import sys

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

from grafana_push_notification_policy import (  # noqa: E402
    CRITICAL_MATCHER,
    CRITICAL_TIMINGS,
    PROXMOX_MATCHER,
    PROXMOX_TIMINGS,
    check_order,
    duration_seconds,
    ensure_critical_route,
    ensure_proxmox_route,
    route_selects,
    routes_severity_critical,
    timings_differ,
)

NETWORK_ROUTE = {
    'receiver': 'critical-alerts',
    'group_by': ['alertname', 'uplink'],
    'object_matchers': [['service', '=', 'network']],
}

PROD_TREE = {
    'receiver': 'default-alerts',
    'group_by': ['alertname'],
    'group_wait': '30s',
    'group_interval': '5m',
    'repeat_interval': '4h',
    'routes': [NETWORK_ROUTE],
}


def test_adds_the_route_when_it_is_missing():
    updated, changed, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    assert changed
    assert any(routes_severity_critical(r) for r in updated['routes'])


def test_keeps_every_existing_route_untouched_and_in_order():
    updated, _, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    assert updated['routes'][0] == NETWORK_ROUTE, 'an existing route was edited or reordered'
    assert len(updated['routes']) == len(PROD_TREE['routes']) + 1


def test_appends_last_so_no_alert_changes_destination():
    """First match wins. Appending means only what currently falls through is re-aimed —
    the network route still catches its own warnings and infos, which a `severity` route
    placed above it would have stolen."""
    updated, _, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    assert routes_severity_critical(updated['routes'][-1])


def test_does_not_touch_the_root():
    updated, _, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    for key in ('receiver', 'group_by', 'group_wait', 'group_interval', 'repeat_interval'):
        assert updated[key] == PROD_TREE[key]


def test_does_not_mutate_its_input():
    before = len(PROD_TREE['routes'])
    ensure_critical_route(PROD_TREE, 'critical-alerts')
    assert len(PROD_TREE['routes']) == before


def test_is_idempotent():
    once, _, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    twice, changed, reason = ensure_critical_route(once, 'critical-alerts')
    assert not changed, reason
    assert twice == once


def test_recognises_a_hand_written_matcher_string():
    """A tree written by hand spells it `matchers: ["severity = critical"]`; missing that
    spelling would append a second, duplicate route on every run. (Its timings may still be
    corrected -- what must not happen is a second route.)"""
    tree = {'receiver': 'x', 'routes': [{'receiver': 'y', 'matchers': ['severity = critical']}]}
    updated, _, _ = ensure_critical_route(tree, 'critical-alerts')
    assert len(updated['routes']) == 1, 'a duplicate route was appended'
    assert updated['routes'][0]['receiver'] == 'y', "an existing route's receiver was rewritten"


def test_corrects_the_timings_on_an_existing_route():
    """Production already carries the route, so appending is no longer how it converges: a
    15m repeat against a permanently true condition is 96 messages a day, and the repo has
    to be able to put it back to 4h."""
    tree = {
        'receiver': 'default-alerts',
        'routes': [
            NETWORK_ROUTE,
            {
                'receiver': 'critical-alerts',
                'object_matchers': [['severity', '=', 'critical']],
                'group_wait': '0s',
                'group_interval': '30s',
                'repeat_interval': '15m',
            },
        ],
    }
    updated, changed, reason = ensure_critical_route(tree, 'critical-alerts')
    assert changed, reason
    assert updated['routes'][1]['repeat_interval'] == '4h'
    assert updated['routes'][1]['group_wait'] == '0s', 'the first page must still be instant'
    assert updated['routes'][0] == NETWORK_ROUTE, 'another route was touched'
    assert len(updated['routes']) == 2, 'a duplicate was appended instead of a correction'
    again, changed_again, _ = ensure_critical_route(updated, 'critical-alerts')
    assert not changed_again, 'the correction does not converge'
    assert again == updated


def test_the_repeat_interval_is_not_the_dev_file_default():
    """`notification-policies.yml` proposed 15m; production cannot use it."""
    assert CRITICAL_TIMINGS['repeat_interval'] == '4h'
    assert CRITICAL_TIMINGS['group_wait'] == '0s', 'the first critical page must stay instant'


def test_a_tree_with_no_routes_at_all():
    tree = {'receiver': 'default-alerts'}
    updated, changed, _ = ensure_critical_route(tree, 'critical-alerts')
    assert changed
    assert len(updated['routes']) == 1


@pytest.mark.parametrize(
    'route',
    [
        {'object_matchers': [['severity', '=', 'warning']]},
        {'object_matchers': [['service', '=', 'network']]},
        {'matchers': ['severity = warning']},
        {'object_matchers': [['severity', '!=', 'critical']]},
        {},
    ],
)
def test_other_routes_are_not_mistaken_for_it(route):
    assert not routes_severity_critical(route)


# --------------------------------------------------------------- the daily Proxmox route
#
# Disk fills over days. At the critical cadence a permanently-full backup store pages six
# times a day forever, which is how a backup disk (over 95% continuously)
# became the loudest thing in the critical space.


def _both(tree, receiver='critical-alerts'):
    tree, _, _ = ensure_critical_route(tree, receiver)
    tree, _, _ = ensure_proxmox_route(tree, receiver)
    return tree


def test_the_proxmox_route_repeats_daily_but_still_pages_at_once():
    assert PROXMOX_TIMINGS['repeat_interval'] == '24h'
    assert PROXMOX_TIMINGS['group_wait'] == '0s', 'slowing the REPEAT must not delay the first page'


def test_proxmox_comes_before_critical_or_it_is_never_reached():
    """Alertmanager routing is first-match-wins and a Proxmox critical matches both."""
    routes = _both(PROD_TREE)['routes']
    pve = next(i for i, r in enumerate(routes) if route_selects(r, PROXMOX_MATCHER))
    crit = next(i for i, r in enumerate(routes) if route_selects(r, CRITICAL_MATCHER))
    assert pve < crit, f'proxmox route at {pve}, critical at {crit}: the narrow one must win'


def test_the_order_holds_whichever_way_round_they_are_applied():
    tree, _, _ = ensure_proxmox_route(PROD_TREE, 'critical-alerts')
    tree, _, _ = ensure_critical_route(tree, 'critical-alerts')
    routes = tree['routes']
    pve = next(i for i, r in enumerate(routes) if route_selects(r, PROXMOX_MATCHER))
    crit = next(i for i, r in enumerate(routes) if route_selects(r, CRITICAL_MATCHER))
    assert pve < crit


def test_applying_both_is_idempotent():
    once = _both(PROD_TREE)
    twice = _both(once)
    assert twice == once
    _, changed, _ = ensure_proxmox_route(once, 'critical-alerts')
    assert changed is False


def test_both_routes_keep_the_network_route_and_the_root():
    tree = _both(PROD_TREE)
    assert tree['routes'][0] == NETWORK_ROUTE, 'the pre-existing route must not move or change'
    assert tree['receiver'] == PROD_TREE['receiver']
    assert tree['repeat_interval'] == PROD_TREE['repeat_interval']
    assert len(tree['routes']) == 3


def test_an_existing_proxmox_route_has_its_repeat_corrected_not_duplicated():
    tree = {
        **PROD_TREE,
        'routes': [
            NETWORK_ROUTE,
            {
                'receiver': 'critical-alerts',
                'object_matchers': [list(PROXMOX_MATCHER)],
                'repeat_interval': '4h',
            },
        ],
    }
    updated, changed, reason = ensure_proxmox_route(tree, 'critical-alerts')
    assert changed is True
    assert '24h' in reason
    matching = [r for r in updated['routes'] if route_selects(r, PROXMOX_MATCHER)]
    assert len(matching) == 1, 'correct it in place, never append a second one'
    assert matching[0]['repeat_interval'] == '24h'


def test_a_severity_route_is_not_mistaken_for_the_proxmox_one():
    critical_only = {'object_matchers': [list(CRITICAL_MATCHER)]}
    assert not route_selects(critical_only, PROXMOX_MATCHER)
    assert route_selects(critical_only, CRITICAL_MATCHER)


def test_route_selects_reads_a_hand_written_matcher_string():
    assert route_selects({'matchers': ['service = proxmox']}, PROXMOX_MATCHER)
    assert not route_selects({'matchers': ['service = vm_host']}, PROXMOX_MATCHER)


def test_the_proxmox_route_does_not_mutate_its_input():
    import copy

    before = copy.deepcopy(PROD_TREE)
    ensure_proxmox_route(PROD_TREE, 'critical-alerts')
    assert PROD_TREE == before


def test_routes_severity_critical_still_works_for_its_old_callers():
    assert routes_severity_critical({'object_matchers': [list(CRITICAL_MATCHER)]})
    assert not routes_severity_critical(NETWORK_ROUTE)
    assert CRITICAL_TIMINGS['repeat_interval'] == '4h'


# ------------------------------------------------- a route in the wrong order is a dead route
#
# `ensure_route` deliberately never reorders, so a tree that already holds both routes the
# WRONG way round reports "already exists" and changes nothing. Without an order check
# before that early return, the run is green and the Proxmox alarms keep the 4h cadence.


def _reversed_tree():
    return {
        **PROD_TREE,
        'routes': [
            NETWORK_ROUTE,
            {'receiver': 'x', 'object_matchers': [list(CRITICAL_MATCHER)], **CRITICAL_TIMINGS},
            {'receiver': 'x', 'object_matchers': [list(PROXMOX_MATCHER)], **PROXMOX_TIMINGS},
        ],
    }


def test_check_order_passes_on_a_correct_tree():
    assert check_order(_both(PROD_TREE)['routes']) is None


def test_check_order_catches_proxmox_after_critical():
    reason = check_order(_reversed_tree()['routes'])
    assert reason is not None
    assert 'never reached' in reason


def test_check_order_is_silent_until_both_routes_exist():
    """Half a tree is not yet wrong — the critical-only state is what we start from."""
    assert check_order(PROD_TREE['routes']) is None
    critical_only, _, _ = ensure_critical_route(PROD_TREE, 'critical-alerts')
    assert check_order(critical_only['routes']) is None


def test_a_misordered_tree_is_reported_even_though_nothing_would_change():
    """The dangerous case: correct timings, wrong order, so both helpers say `unchanged`."""
    tree = _reversed_tree()
    _, crit_changed, _ = ensure_critical_route(tree, 'x')
    _, pve_changed, _ = ensure_proxmox_route(tree, 'x')
    assert not crit_changed and not pve_changed, 'nothing to change is exactly why the check must be separate'
    assert check_order(tree['routes']) is not None


# ------------------------------------------------------- `24h` and `1d` are the same thing
#
# Grafana normalises the `repeat_interval: 24h` this repo asks for into `1d` on the way in.
# A string comparison therefore saw a difference on EVERY run: "corrected the timings" was
# reported forever, the whole tree was rewritten on every push, and `grafana-sync.py status`
# could never come back clean. Measured against a live instance.


def test_grafana_spelling_of_the_same_interval_is_not_a_difference():
    assert duration_seconds('24h') == duration_seconds('1d') == 86400
    assert duration_seconds('4h') == 14400
    assert duration_seconds('30s') == 30
    assert duration_seconds('0s') == 0


def test_a_route_grafana_normalised_reports_no_drift():
    live = {'receiver': 'x', 'object_matchers': [list(PROXMOX_MATCHER)], **PROXMOX_TIMINGS, 'repeat_interval': '1d'}
    assert timings_differ(live, PROXMOX_TIMINGS) == {}
    tree = {**PROD_TREE, 'routes': [NETWORK_ROUTE, live]}
    _, changed, reason = ensure_proxmox_route(tree, 'x')
    assert changed is False, f'a normalised tree must be a no-op, got: {reason}'


def test_a_genuinely_different_interval_is_still_caught():
    live = {'receiver': 'x', 'object_matchers': [list(PROXMOX_MATCHER)], **PROXMOX_TIMINGS, 'repeat_interval': '4h'}
    assert timings_differ(live, PROXMOX_TIMINGS) == {'repeat_interval': '24h'}


def test_an_unparseable_duration_falls_back_to_equality():
    """Never silently treat something we cannot parse as equal."""
    assert duration_seconds('whenever') is None
    assert timings_differ({'repeat_interval': 'whenever'}, {'repeat_interval': '24h'}) == {'repeat_interval': '24h'}
