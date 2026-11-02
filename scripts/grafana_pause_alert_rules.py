#!/usr/bin/env python3
"""Pause alert rules by uid on a live Grafana, reversibly.

Why this exists: `grafana_push_alert_rules.py` only ever pushes rules that live in this
repo, and it cannot delete. The rules that need silencing are the opposite case — created
in the production UI, existing nowhere in git — so there was no reviewable way to retire
one. The options were a hand-written curl during an incident, or deleting a rule and
losing its definition and its history with it.

Pausing is the right verb for retiring an alarm. `isPaused: true` stops evaluation and
notification while keeping the rule, its query, its annotations and its state history, so
re-arming is one flag and no archaeology.

  * it NEVER deletes anything (`call()` refuses the DELETE verb outright, inherited);
  * every rule it touches is written to a backup file FIRST, one JSON per rule, so a
    restore is a PUT of that file;
  * a rule already in the requested state is a no-op, so re-running is free;
  * an unknown uid is an error before anything is written, not a silent skip.

    set -a && . ./.env && set +a
    uv run python scripts/grafana_pause_alert_rules.py --dry-run disk-nearly-full
    uv run python scripts/grafana_pause_alert_rules.py disk-nearly-full disk-full
    uv run python scripts/grafana_pause_alert_rules.py --unpause disk-nearly-full
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grafana_push_alert_rules import Grafana

log = logging.getLogger('grafana_pause_alert_rules')

RULES_PATH = '/api/v1/provisioning/alert-rules'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('uids', nargs='+', help='alert rule uids to pause')
    parser.add_argument('--unpause', action='store_true', help='resume them instead')
    parser.add_argument('--backup-dir', default='tmp', help='where each rule is saved before writing (default: tmp/)')
    parser.add_argument('--dry-run', action='store_true', help='print what would change, write nothing')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    url, token = os.environ.get('GRAFANA_URL'), os.environ.get('GRAFANA_API_KEY')
    if not url or not token:
        raise SystemExit('GRAFANA_URL and GRAFANA_API_KEY must be set (source .env)')
    paused = not args.unpause

    grafana = Grafana(url, token, args.dry_run)

    # Fetch every rule BEFORE writing any of them: a typo in the third uid must not leave
    # the first two paused and the operator guessing which.
    rules = {}
    for uid in args.uids:
        rule = grafana.call('GET', f'{RULES_PATH}/{uid}')
        if rule is None:
            raise SystemExit(f'no alert rule with uid {uid!r} on {url} — nothing was changed')
        rules[uid] = rule

    stamp = datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')
    for uid, rule in rules.items():
        if bool(rule.get('isPaused')) == paused:
            log.info('%13s  %-24s (already %s)', 'unchanged', uid, 'paused' if paused else 'running')
            continue
        if args.dry_run:
            log.info('%13s  %-24s %r', 'would ' + ('pause' if paused else 'resume'), uid, rule['title'])
            continue
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f'alert-rule-{uid}-{stamp}.json'
        backup.write_text(json.dumps(rule, indent=2))
        grafana.call('PUT', f'{RULES_PATH}/{uid}', {**rule, 'isPaused': paused})
        after = grafana.call('GET', f'{RULES_PATH}/{uid}')
        if bool((after or {}).get('isPaused')) != paused:
            raise SystemExit(f'PUT reported success but {uid} is not {"paused" if paused else "running"}')
        log.info('%13s  %-24s %r  (backup %s)', 'paused' if paused else 'resumed', uid, rule['title'], backup)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
