#!/usr/bin/env python3
"""Rewrite dashboards/ into the exact bytes Grafana Git Sync writes.

Seeding the repo with these bytes is what stops the first UI save of every
dashboard landing as a whole-file rewrite: Grafana saves in its own encoding
and schema, so anything else on disk is replaced wholesale on first touch.

Reads each dashboard back through the k8s-style dashboard API, which applies
the SAME backend schema migration a save applies (schemaVersion bump, defaults
materialised) and returns the same `spec` the file gets. Needs the
`kubernetesDashboards` feature toggle, i.e. run it on the instance that is
about to be connected to Git Sync.

    scripts/grafana-canonicalize-dashboards.py [--check] [repo-root]

Configuration comes from .env (loaded automatically) or the environment.

The encoding is Go's encoding/json with SetIndent("", "  "): sorted keys, two
spaces, & < > escaped as \\u0026 \\u003c \\u003e, and NO trailing newline.
Verified byte-identical against files Grafana itself committed.
"""
import json
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from grafana_env import http_get, load_env  # noqa: E402

API = "/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards/"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def go_json(obj: dict) -> str:
    s = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    return (s.replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">")
             .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))


def canonical(resource: dict) -> str:
    return go_json({
        "apiVersion": resource["apiVersion"],
        "kind": resource["kind"],
        "metadata": {"name": resource["metadata"]["name"]},
        "spec": resource["spec"],
    })


def main() -> int:
    load_env()
    get = http_get

    check = "--check" in sys.argv
    roots = [a for a in sys.argv[1:] if not a.startswith("-")]
    root = pathlib.Path(roots[0]).resolve() if roots else ROOT
    changed = failed = conflicted = 0
    for path in sorted((root / "dashboards").rglob("*.json")):
        # The uid is metadata.name once a file is in Grafana's own form; before
        # that the backup names the file after the uid. Never guess from a slug.
        try:
            uid = json.loads(path.read_text())["metadata"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError):
            uid = path.stem
        try:
            resource = get(API + uid)
        except urllib.error.HTTPError as e:
            print(f"  !! {path.relative_to(root)}: HTTP {e.code} — {e.reason}", file=sys.stderr)
            failed += 1
            continue

        # A resource another manager owns cannot be adopted by the Git Sync repo
        # ("is managed by plugin 'prometheus' and cannot be modified by repo 'grafana'"), so it
        # warns on every sync while its file sits under the synced path. It is reported, NOT
        # removed: with a whole-instance target, deleting the file DELETES THE DASHBOARD —
        # measured the hard way, when dropping the plugin-managed
        # "Prometheus 2.0 Stats" took it off production. Take the warning to the manager
        # instead, or let the repo re-create the resource as its own.
        manager = (resource["metadata"].get("annotations") or {}).get("grafana.app/managedBy")
        if manager and manager not in ("repo", "kubectl"):
            manager_id = (resource["metadata"].get("annotations") or {}).get("grafana.app/managerId", "")
            print(f"  !! {path.relative_to(root)} is managed by {manager} {manager_id!r};"
                  " Git Sync cannot adopt it and will warn on every sync", file=sys.stderr)
            conflicted += 1

        text = canonical(resource)
        if path.read_text() == text:
            continue
        changed += 1
        print(f"  {'would rewrite' if check else 'rewrote'} {path.relative_to(root)}")
        if not check:
            path.write_text(text)

    total = len(list((root / "dashboards").rglob("*.json")))
    print(f"{total} dashboards, {changed} {'differ' if check else 'rewritten'}, "
          f"{conflicted} owned by another manager, {failed} unreadable")
    return 1 if (failed or (check and changed)) else 0


if __name__ == "__main__":
    sys.exit(main())
