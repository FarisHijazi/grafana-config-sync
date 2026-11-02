#!/usr/bin/env python3
"""Push this repo's provisioned alert rules into a running Grafana over its HTTP API.

Why this exists: `provisioning/alerting/*.yml` is mounted only by a DEV docker stack.
A production Grafana that is UI-configured never reads this directory, so a rule that
lives here is documentation until something carries it across — and an alarm that was
written but never deployed is the worst kind: everyone believes it exists.

It is deliberately additive:

  * it NEVER deletes anything — `call()` refuses the DELETE verb outright, so no
    code path, present or future, can remove a rule, a group or a folder;
  * a rule that already exists is left ALONE and its differences are reported;
    changing one takes an explicit `--update`;
  * an unchanged rule is a no-op even under `--update`, so re-running is free and
    the report tells you honestly that nothing moved;
  * the only thing it creates unprompted is a missing folder.

Credentials come from the environment — `GRAFANA_URL` and `GRAFANA_API_KEY`, which
a `.env` at the repo root can carry — so the usual invocation is:

    set -a && . ./.env && set +a
    uv run python scripts/grafana_push_alert_rules.py provisioning/alerting/rules-rollups.yml

`--dry-run` prints the payloads and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from collections.abc import Callable  # ruff: ignore[typing-only-standard-library-import]
from pathlib import Path

import yaml

log = logging.getLogger('grafana_push_alert_rules')

DEV_DATASOURCE_UID = 'default'  # placeholder in rule files, resolved to --datasource-name
EXPRESSION_UID = '__expr__'
TIMEOUT_S = 30
RETRIES = 4

# The fields we send. Grafana echoes back more than it was given — `id`,
# `updated`, `provenance`, and `intervalMs`/`maxDataPoints` inside every query
# model — so "has this rule changed?" can only be asked about the keys we
# actually set. Comparing the raw responses makes every rule read as modified,
# which would turn `--update` into an unconditional rewrite and lose the
# idempotency this script is built on.
PUSHED_FIELDS = (
    'title',
    'folderUID',
    'ruleGroup',
    'condition',
    'data',
    'noDataState',
    'execErrState',
    'for',
    # Anti-flap, and it has to be in this list to exist: a rule file can set
    # `keep_firing_for` and the API will accept the POST while silently storing Grafana's
    # `0s` default, because the payload is built from these fields alone. That happened:
    # pushed with `keep_firing_for: 10m`, stored `0s`, reported `created`.
    'keep_firing_for',
    'labels',
    'annotations',
)


def duration_seconds(text: str | None) -> int | None:
    """`24h` and `1d` are the same interval, and Grafana rewrites one into the other.

    It normalised the `repeat_interval: 24h` this file asks for into `1d` on the way in, so
    a string comparison saw a difference on every run: the script reported "corrected the
    timings" forever, rewrote the whole tree on every push, and `status` could never be
    clean. Compare the interval, not the spelling.
    """
    if text is None:
        return None
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    total, number = 0, ''
    for char in str(text).strip():
        if char.isdigit():
            number += char
        elif char in units and number:
            total += int(number) * units[char]
            number = ''
        else:
            return None  # not a duration we understand; fall back to equality upstream
    return total if not number else None


def comparable(field: str, value):
    """`value` reduced to what this script is responsible for."""
    if field == 'data':
        return [
            {
                'refId': q.get('refId'),
                'datasourceUid': q.get('datasourceUid'),
                'relativeTimeRange': q.get('relativeTimeRange'),
                'model': {k: v for k, v in (q.get('model') or {}).items() if k not in {'intervalMs', 'maxDataPoints'}},
            }
            for q in value or []
        ]
    if field in {'labels', 'annotations'}:
        # YAML block scalars and Grafana's own store disagree about a trailing
        # newline; that is whitespace, not a change worth rewriting a live rule for.
        return {k: str(v).strip() for k, v in (value or {}).items()}
    if field in {'for', 'keep_firing_for'}:
        # Grafana stores `for: 0m` as `0s`; compare the interval, as for the policy tree.
        seconds = duration_seconds(value if value is not None else '0s')
        return value if seconds is None else seconds
    return value


class Conflict(Exception):
    """A POST was refused because the uid already exists (HTTP 409)."""


class Grafana:
    def __init__(self, url: str, token: str, dry_run: bool, *, disable_provenance: bool = True) -> None:
        self.url = url.rstrip('/')
        self.token = token
        self.dry_run = dry_run
        # `X-Disable-Provenance: true` stores a resource as provenance `none`, which keeps
        # it editable in the UI — right for alert rules. It is WRONG for a resource that
        # Grafana already holds at provenance `api`: it refuses to move one between
        # classes in place and answers 403 `alerting.notifications.invalidProvenance`. The
        # a UI/API-made notification policy tree is already `api`, so the policy pusher turns
        # this off and writes as `api` too.
        self.disable_provenance = disable_provenance
        self._datasource_uids: dict[str, str] = {}

    def call(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        if method.upper() == 'DELETE':  # the whole safety property, in one line
            raise SystemExit('this script never deletes anything')
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Content-Type', 'application/json')
        if self.disable_provenance:
            req.add_header('X-Disable-Provenance', 'true')  # keep pushed rules editable in the UI
        # If the Grafana host is reached over a VPN, a cold connect can
        # take longer than the whole timeout and a link blip drops one request out
        # of a run of twenty. Retrying a GET is free and PUT is idempotent on a uid.
        # ⚠️ POST IS NOT: creating an alert rule that already exists is a 409, so when the
        # link eats the RESPONSE to a POST that the server did apply, the retry fails on
        # the rule the first attempt created. Seen in practice:
        # created in the database, reported to the operator as a hard failure, and the rest
        # of the file never pushed. 409 is therefore raised as `Conflict` so `push_rule`
        # can tell "my own retry" from "a uid someone else owns".
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code == 409:
                    raise Conflict(f'{method} {path} -> HTTP 409') from exc
                raise SystemExit(f'{method} {path} -> HTTP {exc.code}: {exc.read().decode(errors="replace")}') from exc
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                if attempt == RETRIES - 1:
                    raise SystemExit(f'{method} {path} -> {type(exc).__name__}: {exc}') from exc
                log.debug('%s %s failed (%s), retrying', method, path, exc)
                time.sleep(2 * (attempt + 1))
        return None

    def datasource_uid(self, name: str) -> str:
        # Cached: a rule file naming several datasources would otherwise re-ask for each
        # query, and every request over a flaky link is one more chance to be dropped.
        if name in self._datasource_uids:
            return self._datasource_uids[name]
        found = self.call('GET', f'/api/datasources/name/{urllib.parse.quote(name)}')
        if not found:
            raise SystemExit(f'no datasource named {name!r} on {self.url} (GET /api/datasources lists them)')
        self._datasource_uids[name] = found['uid']
        return found['uid']

    def folder_uid(self, title: str) -> str:
        """Resolve a folder TITLE, refusing to guess when the title is not unique.

        After a Git Sync cutover an instance can hold two folders with the same title — one
        holding dashboards under Git Sync, the other holding the alert rules — and a
        first-match lookup returns whichever comes first. Giving the alerting folders their
        own titles (`... Alerts`) makes every title unique. The refusal stays
        because the failure it prevents is silent: a first-match win puts rules in the
        wrong folder with a success message. (When the shadowed folder is the Git Sync
        one it does fail loudly — `cannot store rules in folder managed by Git Sync` —
        but that is Grafana's luck, not this script's doing.) `--folder-uid` is the way
        past it if duplicate titles ever come back.
        """
        matches = [f['uid'] for f in self.call('GET', '/api/folders') or [] if f['title'] == title]
        if len(matches) > 1:
            raise SystemExit(
                f'folder title {title!r} is ambiguous on this instance: {", ".join(sorted(matches))}. '
                f'Pass --folder-uid <uid> to say which one (the alerting folders are the ones '
                f'that report zero children, because /api/search counts dashboards).'
            )
        if matches:
            return matches[0]
        if self.dry_run:
            return f'<new folder {title!r}>'
        log.info('%13s  folder %r', 'creating', title)
        return self.call('POST', '/api/folders', {'title': title})['uid']

    def _recover_conflict(self, payload: dict) -> str:
        """A POST 409'd on a uid the preceding GET said was absent. Whose rule is it?

        Either a retry of our own POST landed after the link dropped the response, or
        someone else owns the uid. Read it back and let the CONTENT decide — guessing
        either way is wrong: calling a stranger's rule ours hides a collision, and calling
        ours a stranger's aborts a run that in fact succeeded.
        """
        uid = payload['uid']
        stored = self.call('GET', f'/api/v1/provisioning/alert-rules/{uid}')
        if stored is None:
            raise SystemExit(f'{uid}: POST conflicted but the rule is not readable')
        differing = [f for f in PUSHED_FIELDS if comparable(f, stored.get(f)) != comparable(f, payload.get(f))]
        if differing:
            raise SystemExit(
                f'{uid} already exists and differs ({", ".join(differing)}) -- it is not the rule '
                f'this file describes. Refusing to touch it; rename the uid or reconcile by hand.'
            )
        return 'created (recovered a retried POST)'

    def _reconcile_rule(self, existing: dict, payload: dict, *, allow_update: bool) -> str:
        """The rule is already there: is it ours, is it current, and may we change it?"""
        differing = [f for f in PUSHED_FIELDS if comparable(f, existing.get(f)) != comparable(f, payload.get(f))]
        if not differing:
            return 'unchanged'
        if not allow_update:
            return 'EXISTS, left alone (differs: ' + ', '.join(differing) + '; pass --update)'
        if self.dry_run:
            return 'would update (' + ', '.join(differing) + ')'
        self.call('PUT', f'/api/v1/provisioning/alert-rules/{payload["uid"]}', payload)
        return 'updated (' + ', '.join(differing) + ')'

    def push_rule(self, payload: dict, *, allow_update: bool) -> str:
        existing = self.call('GET', f'/api/v1/provisioning/alert-rules/{payload["uid"]}')
        if existing is not None:
            return self._reconcile_rule(existing, payload, allow_update=allow_update)
        if self.dry_run:
            return 'would create'
        try:
            self.call('POST', '/api/v1/provisioning/alert-rules', payload)
        except Conflict:
            return self._recover_conflict(payload)
        return 'created'

    def set_group_interval(self, folder_uid: str, group: str, interval_s: int, *, allow_update: bool) -> None:
        """The evaluation cadence lives on the GROUP, not the rule, and a group the
        rule API has just created sits at Grafana's 60 s default — a rule file
        may ask for 30 s. Writing it back means PUTting the group, so it is gated like
        any other change to something that already exists, and it names the rules it
        is about to rewrite in place."""
        path = f'/api/v1/provisioning/folder/{folder_uid}/rule-groups/{urllib.parse.quote(group)}'
        current = self.call('GET', path)
        if not current or current.get('interval') == interval_s:
            return
        if not allow_update:
            log.info(
                '%13s  group %r interval %ss (live: %ss; pass --update)',
                'left alone',
                group,
                interval_s,
                current.get('interval'),
            )
            return
        log.info(
            '%13s  group %r interval %ss -> %ss (rewrites %d rule(s) in place)',
            'would set' if self.dry_run else 'setting',
            group,
            current.get('interval'),
            interval_s,
            len(current.get('rules', [])),
        )
        if not self.dry_run:
            self.call('PUT', path, {**current, 'interval': interval_s})


def interval_seconds(text: str | int) -> int:
    """'30s' / '1m' / '1h' → seconds, the only shapes the rule files use."""
    if isinstance(text, int):
        return text
    unit = {'s': 1, 'm': 60, 'h': 3600}[text[-1]]
    return int(text[:-1]) * unit


def rule_payload(
    rule: dict, group: dict, folder_uid: str, datasource_uid: str, resolve: Callable[[str], str] | None = None
) -> dict:
    data = []
    for source in rule['data']:
        query = dict(source)
        if query['datasourceUid'] == DEV_DATASOURCE_UID:
            query['datasourceUid'] = datasource_uid
        elif query['datasourceUid'] != EXPRESSION_UID:
            # Any other value is read as a datasource NAME on the target — which is how a
            # rule against Prometheus (or any second datasource) says so in the file
            # instead of borrowing the default-datasource placeholder and meaning something
            # else. Unknown names still stop the push: `datasource_uid` exits.
            if resolve is None:
                raise SystemExit(f'rule {rule["uid"]}: unknown datasourceUid {query["datasourceUid"]!r}')
            query['datasourceUid'] = resolve(query['datasourceUid'])
        query['model'] = {**query['model'], 'refId': query['refId']}
        data.append(query)
    return {
        'uid': rule['uid'],
        'title': rule['title'],
        'orgID': group.get('orgId', 1),
        'folderUID': folder_uid,
        'ruleGroup': group['name'],
        'condition': rule['condition'],
        'data': data,
        'noDataState': rule.get('noDataState', 'NoData'),
        'execErrState': rule.get('execErrState', 'Error'),
        'for': rule.get('for', '0s'),
        'keep_firing_for': rule.get('keep_firing_for', '0s'),
        'labels': rule.get('labels', {}),
        'annotations': rule.get('annotations', {}),
    }


def load_rules(paths: list[str]) -> list[tuple[dict, dict]]:
    """Every (group, rule) pair in the given files, in file order."""
    files = sorted({str(f) for p in paths for f in Path().glob(p)})
    if not files:
        raise SystemExit(f'no rule files match {paths}')
    pairs = []
    for file in files:
        for group in yaml.safe_load(Path(file).read_text(encoding='utf-8'))['groups']:
            pairs.extend((group, rule) for rule in group['rules'])
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', nargs='*', default=['provisioning/alerting/rules-*.yml'], help='rule files or globs')
    parser.add_argument('--url', default=os.environ.get('GRAFANA_URL'), help='Grafana base URL [$GRAFANA_URL]')
    parser.add_argument(
        '--token',
        default=os.environ.get('GRAFANA_API_KEY'),
        help='service-account token, Editor or Admin [$GRAFANA_API_KEY]',
    )
    parser.add_argument(
        '--datasource-name', default=os.environ.get('GRAFANA_DEFAULT_DATASOURCE', DEV_DATASOURCE_UID), help="the target Grafana's default datasource NAME [$GRAFANA_DEFAULT_DATASOURCE]"
    )
    parser.add_argument('--datasource-uid', help='… or its uid, if you already know it')
    parser.add_argument('--folder', help='override the folder every pushed rule lands in')
    parser.add_argument(
        '--folder-uid',
        help='… by uid instead of title, for an instance where the title is not unique',
    )
    parser.add_argument('--uid', action='append', help='only push these rule uids (repeatable)')
    parser.add_argument(
        '--update', action='store_true', help='also modify rules that already exist (without it they are left alone)'
    )
    parser.add_argument('--dry-run', action='store_true', help='print what would change, write nothing')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if not args.url:
        parser.error('no --url and no $GRAFANA_URL')
    if not args.token:
        parser.error('no --token and no $GRAFANA_API_KEY')

    grafana = Grafana(args.url, args.token, args.dry_run)
    datasource_uid = args.datasource_uid or grafana.datasource_uid(args.datasource_name)

    # Resolve every datasource a file names BEFORE the first write. Resolving lazily
    # inside the push loop meant a name missing from the target aborted a run that had
    # already created some rules, and one absent datasource killed the run before it
    # reported on any of the others.
    for _group, rule in load_rules(args.files):
        for query in rule['data']:
            named = query['datasourceUid']
            if named not in {DEV_DATASOURCE_UID, EXPRESSION_UID}:
                grafana.datasource_uid(named)

    # `--folder-uid` names ONE folder, so it may not be used where the files name several:
    # applied per-rule it would collapse every group into that uid while the log kept
    # printing each group's own title — the same "rules land in the wrong folder, success
    # reported" failure that `folder_uid()`'s refusal exists to prevent.
    if args.folder_uid and not args.folder:
        named_folders = {group['folder'] for group, _rule in load_rules(args.files)}
        if len(named_folders) > 1:
            raise SystemExit(
                f'--folder-uid names one folder but these files name {len(named_folders)}: '
                f'{", ".join(sorted(named_folders))}. Push them separately, or pass --folder too.'
            )

    folder_uids: dict[str, str] = {}
    touched_groups: dict[tuple[str, str], int] = {}
    for group, rule in load_rules(args.files):
        if args.uid and rule['uid'] not in args.uid:
            continue
        folder = args.folder or group['folder']
        folder_uids.setdefault(folder, args.folder_uid or grafana.folder_uid(folder))
        payload = rule_payload(rule, group, folder_uids[folder], datasource_uid, grafana.datasource_uid)
        log.info(
            '%13s  %-28s -> folder %r, group %r',
            grafana.push_rule(payload, allow_update=args.update),
            rule['uid'],
            folder,
            group['name'],
        )
        if args.dry_run:
            log.debug('%s', json.dumps(payload, indent=2))
        touched_groups[folder_uids[folder], group['name']] = interval_seconds(group.get('interval', '1m'))
    for (folder_uid, group_name), seconds in touched_groups.items():
        if not folder_uid.startswith('<new folder'):
            grafana.set_group_interval(folder_uid, group_name, seconds, allow_update=args.update)
    return 0


if __name__ == '__main__':
    sys.exit(main())
