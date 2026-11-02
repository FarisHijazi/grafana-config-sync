#!/usr/bin/env python3
"""Route `severity = critical` to the critical contact point, on a live Grafana.

Why this exists: a UI-made notification policy tree typically has a route or two for
whatever someone needed first. Everything else falls through to the default receiver —
including rules that label themselves `severity = critical`. They arrive in the noisy
default channel beside a `DatasourceNoData` that re-fires every few hours, which is the
same as not arriving at all.

`provisioning/alerting/notification-policies.yml` describes the right shape — a route
per severity — but that file is mounted only by a DEV docker stack, exactly as
`grafana_push_alert_rules.py` says of the rules beside it. This carries one route across.

Additive, like its sibling:

  * it only ever INSERTS the missing route, appended AFTER the existing ones, so every
    alert that matches a route today keeps the destination it has today — only what
    currently falls through to the default is re-aimed;
  * it never removes, reorders or edits an existing route, and never touches the root;
  * a tree that already routes `severity = critical` is a no-op, so re-running is free;
  * the live tree is written to a backup file before anything is sent, because the
    provisioning API replaces the whole tree in one PUT and there is no undo.

Credentials come from the environment — `GRAFANA_URL` and `GRAFANA_API_KEY`, which
a `.env` at the repo root can carry:

    set -a && . ./.env && set +a
    uv run python scripts/grafana_push_notification_policy.py --dry-run

`--dry-run` prints the before and after trees and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys

from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grafana_push_alert_rules import Grafana, duration_seconds

log = logging.getLogger('grafana_push_notification_policy')

POLICIES_PATH = '/api/v1/provisioning/policies'
DEFAULT_RECEIVER = os.environ.get('GRAFANA_CRITICAL_RECEIVER', 'critical-alerts')

# A critical page waits for nothing -- `group_wait: 0s` -- but it must not re-page every
# 15 minutes the way notification-policies.yml proposes. A permanently true condition then
# becomes 96 messages a day: one backup disk sitting at 95% sent 47 pages in twelve
# hours before this was corrected. 4h matches the root route.
CRITICAL_TIMINGS = {'group_wait': '0s', 'group_interval': '30s', 'repeat_interval': '4h'}

# Disk fills over days, so a disk alarm nagging every 4h is 6 messages a day about a
# number that has not meaningfully moved. The first page is still immediate
# (`group_wait: 0s`); only the REPEAT slows to daily. This route must sit BEFORE the
# severity = critical one, because Alertmanager routing is first-match-wins and every
# Proxmox storage critical matches both.
PROXMOX_TIMINGS = {'group_wait': '0s', 'group_interval': '5m', 'repeat_interval': '24h'}
PROXMOX_MATCHER = ('service', '=', 'proxmox')
CRITICAL_MATCHER = ('severity', '=', 'critical')


def timings_differ(route: dict, timings: dict[str, str]) -> dict[str, str]:
    """The subset of `timings` this route does not already express."""
    stale = {}
    for key, wanted in timings.items():
        current = route.get(key)
        want_s, got_s = duration_seconds(wanted), duration_seconds(current)
        same = want_s == got_s if want_s is not None and got_s is not None else current == wanted
        if not same:
            stale[key] = wanted
    return stale


def route_selects(route: dict, matcher: tuple[str, str, str]) -> bool:
    """True if this route already selects on `matcher` ([name, operator, value]).

    Grafana writes `object_matchers` (triples) but still accepts and returns the older
    `matchers` (["severity = critical"]) on trees that were written by hand, so both
    spellings have to count as "already there" or a second run would append a duplicate.
    """
    name, operator, value = matcher
    for got_name, got_operator, got_value in route.get('object_matchers') or []:
        if (got_name, got_operator, got_value) == (name, operator, value):
            return True
    wanted = f'{name}{operator}{value}'
    return any(''.join(str(raw).split()) == wanted for raw in route.get('matchers') or [])


def routes_severity_critical(route: dict) -> bool:
    """True if this route already selects on `severity = critical`."""
    return route_selects(route, CRITICAL_MATCHER)


def ensure_route(
    tree: dict,
    receiver: str,
    matcher: tuple[str, str, str],
    timings: dict[str, str],
    *,
    before: tuple[str, str, str] | None = None,
) -> tuple[dict, bool, str]:
    """Return (tree, changed, reason). The input tree is never mutated.

    Two jobs, because one without the other leaves production unreproducible: insert the
    route when it is missing, and bring an existing one's TIMINGS in line with `timings`.
    Without the second, a repeat interval this file prescribes could only ever be set by
    hand, and a tree restored from an older backup would silently go back to the old
    cadence -- which is how `severity = critical` sat at a 15-minute repeat.

    An existing route's receiver and matchers are never touched -- someone may have aimed
    it somewhere deliberately -- and no route is ever removed or reordered.

    `before` names a route this one must PRECEDE. Alertmanager routing is first-match-wins,
    so a narrow route appended after a broader one that also matches is dead on arrival:
    `service = proxmox` has to come before `severity = critical` or every Proxmox storage
    critical keeps the critical cadence. It is inserted directly before the first route
    selecting `before`, and appended if there is none.
    """
    label = f'{matcher[0]} {matcher[1]} {matcher[2]}'
    updated = copy.deepcopy(tree)
    existing = updated.get('routes') or []

    for route in existing:
        if not route_selects(route, matcher):
            continue
        stale = timings_differ(route, timings)
        if not stale:
            return updated, False, f'a route for {label} already exists (receiver {route.get("receiver")!r})'
        was = {k: route.get(k) for k in stale}
        route.update(stale)
        return updated, True, f'corrected the timings on the existing {label} route: {was} -> {stale}'

    new = {'receiver': receiver, 'object_matchers': [list(matcher)], 'continue': False, **timings}
    at = len(existing)
    if before is not None:
        at = next((i for i, r in enumerate(existing) if route_selects(r, before)), len(existing))
    updated['routes'] = [*existing[:at], new, *existing[at:]]
    where = f'at position {at} of {len(existing) + 1}' if before else f'after {len(existing)} existing route(s)'
    return updated, True, f'inserted a {label} route to {receiver!r}, {where}'


def ensure_critical_route(tree: dict, receiver: str) -> tuple[dict, bool, str]:
    """Route `severity = critical` to the critical contact point. Appended last."""
    return ensure_route(tree, receiver, CRITICAL_MATCHER, CRITICAL_TIMINGS)


def ensure_proxmox_route(tree: dict, receiver: str) -> tuple[dict, bool, str]:
    """Slow the Proxmox disk alarms to a daily repeat, ahead of the critical route."""
    return ensure_route(tree, receiver, PROXMOX_MATCHER, PROXMOX_TIMINGS, before=CRITICAL_MATCHER)


def check_order(routes: list[dict]) -> str | None:
    """The reason the route order is wrong, or None if it is fine.

    Only meaningful once both routes exist: a narrow route sitting after a broader one that
    also matches is dead, because Alertmanager stops at the first match and both carry
    `continue: false`.
    """
    pve_at = next((i for i, r in enumerate(routes) if route_selects(r, PROXMOX_MATCHER)), None)
    crit_at = next((i for i, r in enumerate(routes) if route_selects(r, CRITICAL_MATCHER)), None)
    if pve_at is None or crit_at is None or pve_at < crit_at:
        return None
    return (
        f'the service = proxmox route sits at position {pve_at}, AFTER severity = critical at '
        f'{crit_at}: first-match-wins means it is never reached and the Proxmox alarms keep the '
        f'4h critical cadence'
    )


def receiver_names(grafana: Grafana) -> list[str]:
    return [r['name'] for r in grafana.call('GET', '/api/v1/provisioning/contact-points') or []]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--receiver', default=DEFAULT_RECEIVER, help=f'contact point for criticals (default: {DEFAULT_RECEIVER})'
    )
    parser.add_argument(
        '--backup-dir', default='tmp', help='where the live tree is saved before writing (default: tmp/)'
    )
    parser.add_argument('--dry-run', action='store_true', help='print the before/after trees and write nothing')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(message)s')

    url, token = os.environ.get('GRAFANA_URL'), os.environ.get('GRAFANA_API_KEY')
    if not url or not token:
        raise SystemExit('GRAFANA_URL and GRAFANA_API_KEY must be set (source .env)')

    # The live tree is provenance `api`; writing it as `none` is a 403 Grafana will not
    # negotiate, so match what is there. See Grafana.__init__.
    grafana = Grafana(url, token, args.dry_run, disable_provenance=False)
    live = grafana.call('GET', POLICIES_PATH)
    if not isinstance(live, dict):
        raise SystemExit(f'GET {POLICIES_PATH} returned {type(live).__name__}, not a policy tree')

    names = receiver_names(grafana)
    if args.receiver not in names:
        raise SystemExit(f'no contact point named {args.receiver!r} on {url} (have: {", ".join(sorted(names))})')

    log.info('live tree : root receiver %r, %d child route(s)', live.get('receiver'), len(live.get('routes') or []))
    # Critical first so that it exists to be inserted BEFORE; the Proxmox route is
    # narrower and must win the first-match race against it.
    updated, crit_changed, reason = ensure_critical_route(live, args.receiver)
    log.info('critical  : %s', reason)
    updated, pve_changed, reason = ensure_proxmox_route(updated, args.receiver)
    log.info('proxmox   : %s', reason)
    changed = crit_changed or pve_changed
    # BEFORE the early return, not after the PUT. `ensure_route` never reorders, so a tree
    # that already holds both routes with the right timings but the WRONG WAY ROUND reports
    # "already exists" and exits 0 -- leaving the narrow route permanently unreachable with
    # a green run behind it, which is the exact failure this route was added to fix. That
    # state is reachable from a UI edit or a restore of an older backup, which is this
    # file's whole reason for existing.
    misordered = check_order(updated.get('routes') or [])
    if misordered:
        raise SystemExit(misordered + ' -- fix the order in the Grafana UI, or restore a backup; refusing to write')
    if not changed:
        return 0

    if args.dry_run:
        log.info('--- would PUT ---\n%s', json.dumps(updated, indent=2))
        return 0

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'notification-policy-{datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")}.json'
    backup.write_text(json.dumps(live, indent=2))
    log.info('backup    : %s  (restore with PUT %s)', backup, POLICIES_PATH)

    grafana.call('PUT', POLICIES_PATH, updated)
    after = grafana.call('GET', POLICIES_PATH)
    landed = (after or {}).get('routes') or []
    for matcher in (CRITICAL_MATCHER, PROXMOX_MATCHER):
        if not any(route_selects(r, matcher) for r in landed):
            raise SystemExit(
                f'PUT reported success but no {matcher[0]} = {matcher[2]} route is in the tree Grafana returns'
            )
    misordered = check_order(landed)
    if misordered:
        raise SystemExit(f'PUT reported success but {misordered}')
    log.info('applied   : %d child route(s) now', len(after.get('routes') or []))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
