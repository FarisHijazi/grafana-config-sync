#!/usr/bin/env python3
"""Compare live folder/dashboard permissions against the snapshot in instance/.

Git Sync REPLACES folders (new uids), so permissions cannot be compared by uid
after a cutover. This matches folders by title and dashboards by uid, ignoring
uid/id/timestamp fields, and prints only what differs.

Exit 0 = no drift, 1 = drift found.
"""
from __future__ import annotations

import json
import sys
import urllib.error

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grafana_env import http_get, load_env  # noqa: E402

VOLATILE = {"created", "updated", "folderId", "folderUid", "uid", "url", "slug",
            "dashboardId", "userId", "teamId", "id", "userAvatarUrl", "teamAvatarUrl",
            "title", "accessControl"}


def key(perm: dict) -> tuple:
    """What a permission grant actually IS, independent of the object it sits on."""
    who = (f"team:{perm.get('team')}" if perm.get("teamId")
           else f"user:{perm.get('userLogin')}" if perm.get("userId")
           else f"role:{perm.get('role')}")
    return (who, perm.get("permissionName"), bool(perm.get("inherited")))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_env()
    get = http_get

    drift = 0

    # Folders are matched by TITLE, because the uid changes when Git Sync replaces them —
    # and after a cutover a title has two folders (the original and the repo's). Both sides
    # are therefore reduced to the UNION of grants per title, which is the question actually
    # being asked: did any access disappear?
    want: dict[str, set] = {}
    for f in sorted((root / "instance" / "folder-permissions").glob("*.json")):
        perms = json.loads(f.read_text())
        if perms:
            want.setdefault(perms[0]["title"], set()).update(key(p) for p in perms)
    # After a Git Sync cutover a title has TWO folders: the original and the one the
    # repo created. Both are checked; the title is satisfied if either carries the
    # expected grants, which is the question being asked ("did access survive?").
    live: dict[str, list[str]] = {}
    for f in get("/api/folders?limit=1000"):
        live.setdefault(f["title"], []).append(f["uid"])

    for title, expected in want.items():
        uids = live.get(title, [])
        if not uids:
            print(f"folder MISSING   {title}")
            drift += 1
            continue
        got = set()
        for uid in uids:
            got.update(key(p) for p in get(f"/api/folders/{uid}/permissions"))
        if got == expected:
            continue
        print(f"folder DRIFT     {title} ({', '.join(uids)})")
        for line in sorted(map(str, expected ^ got)):
            mark = "-" if line in map(str, expected) else "+"
            print(f"                 {mark} {line}")
        drift += 1
    for title in live.keys() - want.keys():
        print(f"folder NEW       {title}")

    # Dashboards keep their uid through Git Sync adoption, so match on uid.
    for f in sorted((root / "instance" / "dashboard-permissions").glob("*.json")):
        uid = f.stem
        expected = {key(p) for p in json.loads(f.read_text())}
        try:
            got = {key(p) for p in get(f"/api/dashboards/uid/{uid}/permissions")}
        except urllib.error.HTTPError as e:
            print(f"dashboard MISSING {uid} (HTTP {e.code})")
            drift += 1
            continue
        if got != expected:
            print(f"dashboard DRIFT  {uid}")
            for line in sorted(map(str, expected ^ got)):
                mark = "-" if line in map(str, expected) else "+"
                print(f"                 {mark} {line}")
            drift += 1

    print(f"\n{len(want)} folders + {len(list((root / 'instance' / 'dashboard-permissions').glob('*.json')))} dashboards checked, {drift} differ")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
