#!/usr/bin/env python3
"""Sync the Grafana configuration that Git Sync CANNOT carry — the alerting.

Grafana's native Git Sync moves **dashboards and their folders, and nothing else**:
the connection is `path: dashboards`, target instance, so a UI save there IS a
commit to `dashboards/` in this repo. Alert rules, the notification policy tree, contact
points, mute timings and templates ride none of it. Carrying those is this script's whole
job — without it, alerting config tends to scatter across whichever service repo
needed an alert first.

    uv run scripts/grafana-sync.py status   # compare; writes nothing, anywhere
    uv run scripts/grafana-sync.py push     # this repo  -> live Grafana
    uv run scripts/grafana-sync.py pull     # live Grafana -> this repo

Two directories, and the difference between them is the whole design:

  * **`provisioning/alerting/`** is SOURCE — hand-written, reviewed, pushed by `push`. It
    mirrors Grafana's own `/etc/grafana/provisioning/<kind>/` layout, which is also what
    a dev docker stack can bind-mount, so one copy serves both.
  * **`alerting/`** is GENERATED — what production actually holds, written by `pull`.
    Never hand-edit it. It is the only record of rules that were made in the
    production UI and exist as source nowhere.

Credentials come from `.env` via `grafana_env`: `GRAFANA_URL`, plus
`GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD` for a decrypted `pull` (the service account
is refused `decrypt=true`) or `GRAFANA_API_KEY`, which is enough to `push`.

⚠️ `push` deliberately does NOT push contact points, and must not be "improved" to.
A contact point's `url` is a secure field that Grafana returns as `[REDACTED]` from every
read endpoint — so a write assembled from a read overwrites the live webhook
with that literal string and the channel goes silently dead. `contact-points.yml` here is
for the DEV stack, which provisions from files. Change a production contact point in the
UI, then `pull`. Mute timings and templates are captured by `pull` and authored nowhere.

`push` never deletes: the rule pusher refuses the DELETE verb outright and leaves an
existing rule alone unless `--update` is passed; the policy pusher only inserts a missing
route or corrects the timings of one it placed itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grafana_env

from grafana_push_alert_rules import main as push_rules
from grafana_push_notification_policy import main as push_policy

log = logging.getLogger('grafana-sync')

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'provisioning' / 'alerting'
GENERATED = ROOT / 'alerting'
RULE_GLOB = 'provisioning/alerting/rules-*.yml'

#: `GET …/<name>/export?format=yaml` for each, into `alerting/<name>.yaml`. An instance
#: with none of a kind answers 404, which is "empty" and not a failure.
EXPORTED = ('alert-rules', 'contact-points', 'policies', 'mute-timings')


def pull(args: argparse.Namespace) -> int:
    """Write what production actually holds into `alerting/`.

    Only these five files are touched — no staging directory and no `rm -rf`, so nothing
    that is not listed here can be destroyed by a pull.
    """
    decrypt = 'true' if os.environ.get('GRAFANA_ADMIN_PASSWORD') and not args.no_decrypt else 'false'
    if decrypt == 'false':
        log.info('note      : exporting REDACTED secrets (no admin credentials, or --no-decrypt)')
    GENERATED.mkdir(exist_ok=True)
    for name in EXPORTED:
        body = _get(f'/api/v1/provisioning/{name}/export?format=yaml&decrypt={decrypt}', raw=True)
        _write(GENERATED / f'{name}.yaml', '' if body is None else body, args.dry_run)
    # The YAML export drops uid and folderUID; the raw list keeps them, and that is what
    # any "which folder does this rule actually live in" question needs.
    for path, endpoint in (('alert-rules.json', 'alert-rules'), ('templates.json', 'templates')):
        body = _get(f'/api/v1/provisioning/{endpoint}')
        _write(GENERATED / path, json.dumps(body or [], indent=1, sort_keys=True) + '\n', args.dry_run)
    if decrypt == 'true' and not args.dry_run:
        log.info('⚠️ secrets : decrypted webhook URLs are now in alerting/ — this repo must stay private')
    return 0


def _get(path: str, *, raw: bool = False):
    request = urllib.request.Request(
        grafana_env.base_url() + path, headers={'Authorization': grafana_env.auth_header()}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # nothing of this kind exists, which is a valid snapshot
            return None
        raise SystemExit(f'GET {path} -> HTTP {exc.code}: {exc.read().decode(errors="replace")[:300]}') from exc
    return payload.decode() if raw else json.loads(payload or b'null')


def _write(path: Path, text: str, dry_run: bool) -> None:
    same = path.is_file() and path.read_text(encoding='utf-8') == text
    verb = 'unchanged' if same else ('would write' if dry_run else 'wrote')
    log.info('%13s  %s (%d bytes)', verb, path.relative_to(ROOT), len(text))
    if not same and not dry_run:
        path.write_text(text, encoding='utf-8')


def push(args: argparse.Namespace) -> int:
    """Apply this repo's alerting source to live Grafana."""
    if SOURCE.resolve() == GENERATED.resolve() or GENERATED.resolve() in SOURCE.resolve().parents:
        raise SystemExit(f'{SOURCE.relative_to(ROOT)} must not live inside the generated {GENERATED.name}/')
    flags = (['--dry-run'] if args.dry_run else []) + (['--update'] if args.update else [])
    log.info('rules     :')
    rules = push_rules([RULE_GLOB, *flags])
    log.info('policy    :')
    return rules or push_policy(['--dry-run'] if args.dry_run else [])


def status(_args: argparse.Namespace) -> int:
    """Report only: every call underneath is a dry run, so nothing is written anywhere."""
    log.info('Comparing this repo against %s. Nothing will be written.\n', grafana_env.base_url())
    result = push(argparse.Namespace(dry_run=True, update=True))
    log.info('\npull:')
    pull(argparse.Namespace(dry_run=True, no_decrypt=False))
    log.info(
        '\n`unchanged` means repo and production agree. `would update` / `would write` means they '
        'do not: `push --update` makes production match the repo, `pull` records what production has.'
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    for name, func, help_text in (
        ('pull', pull, 'live Grafana -> this repo (alerting/)'),
        ('push', push, 'this repo -> live Grafana (provisioning/alerting/)'),
        ('status', status, 'compare both ways; writes nothing'),
    ):
        cmd = sub.add_parser(name, help=help_text, description=func.__doc__)
        cmd.set_defaults(func=func)
        if name != 'status':
            cmd.add_argument('--dry-run', action='store_true', help='report what would change, write nothing')
        if name == 'push':
            cmd.add_argument('--update', action='store_true', help='also modify rules that already exist')
        if name == 'pull':
            cmd.add_argument('--no-decrypt', action='store_true', help='export redacted secrets even as admin')

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    grafana_env.load_env()
    if not os.environ.get('GRAFANA_URL'):
        raise SystemExit('GRAFANA_URL is not set and .env did not provide it')
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
