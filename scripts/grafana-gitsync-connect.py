#!/usr/bin/env python3
"""Create (or repair) the Grafana Git Sync repository on an instance.

Two ways to authenticate, and the script picks by what is configured:

  GitHub App (default, org-owned)  a Connection holds appID/installationID/privateKey
                                   and the Repository references it by name. Grafana
                                   demands the App declare repository_hooks:write.
  GRAFANA_GITSYNC_TOKEN set        no Connection at all: the token goes straight onto
                                   the Repository as secure.token. No App, no webhook
                                   permission — but a PAT belongs to a person.

Idempotent by design: every step is "read, compare, write only if different", because
a flaky link to the Grafana host can report a failure for a request that actually
succeeded, so this script has to be safe to run again immediately.

Configuration comes from .env (loaded automatically) or the environment.

  --check   report only: credentials + current connection/repository state.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grafana_env import base_url, load_env  # noqa: E402

PROV = "/apis/provisioning.grafana.app/v0alpha1/namespaces/default"
# Hardcoded in Grafana: apps/provisioning/pkg/connection/github/connection.go
# (validateAppPermissions) and client.go (GetApp -> GET /app). `webhooks` is read
# from the App's *declared* repository_hooks permission and is required even when
# no webhook is ever delivered.
REQUIRED_APP_PERMS = {
    "contents": "write",
    "metadata": "read",
    "pull_requests": "write",
    "repository_hooks": "write",
}
RANK = {"": 0, None: 0, "read": 1, "write": 2}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name) or default
    if v is None:
        die(f"{name} is not set")
    return v


# --------------------------------------------------------------------------- GitHub

def app_jwt(pem: Path, app_id: str) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    now = int(time.time())
    header = b64(b'{"alg":"RS256","typ":"JWT"}')
    payload = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(pem)],
        input=signing_input, capture_output=True, check=True,
    ).stdout
    return f"{header}.{payload}.{b64(sig)}"


def github_app_permissions(pem: Path, app_id: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/app",
        headers={"Authorization": f"Bearer {app_jwt(pem, app_id)}",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("permissions", {})


def check_app_permissions(pem: Path, app_id: str) -> list[str]:
    """Return the list of permissions Grafana will reject the connection for."""
    have = github_app_permissions(pem, app_id)
    missing = [
        f"{name}: needs '{need}', app declares '{have.get(name, '')}'"
        for name, need in REQUIRED_APP_PERMS.items()
        if RANK.get(have.get(name), 0) < RANK[need]
    ]
    return missing


# --------------------------------------------------------------------------- Grafana

class Grafana:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def request(self, method: str, path: str, body: dict | None = None,
                tries: int = 4, ok404: bool = False) -> dict | None:
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(tries):
            req = urllib.request.Request(self.url + path, data=data, method=method)
            req.add_header("Authorization", f"Basic {self.auth}")
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code == 404 and ok404:
                    return None
                # A create that "failed" on a dropped connection may already have
                # landed; the retry then says AlreadyExists, which is success.
                if e.code == 409 and method == "POST":
                    return None
                raise SystemExit(f"{method} {path} -> HTTP {e.code}: {detail[:600]}") from e
            except Exception as e:  # timeouts, resets
                last = e
                if attempt < tries - 1:
                    time.sleep(2 * (attempt + 1))
        raise SystemExit(f"{method} {path} failed after {tries} tries: {last}")

    def get(self, path: str) -> dict | None:
        return self.request("GET", path, ok404=True)

    def apply(self, collection: str, name: str, desired: dict) -> str:
        """Create or update one provisioning resource. Returns 'created'|'updated'|'unchanged'."""
        existing = self.get(f"{PROV}/{collection}/{name}")
        if existing is None:
            self.request("POST", f"{PROV}/{collection}", desired)
            return "created"
        if existing.get("spec") == desired["spec"] and "secure" not in desired:
            return "unchanged"
        body = dict(desired)
        body["metadata"] = dict(desired["metadata"])
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        self.request("PUT", f"{PROV}/{collection}/{name}", body)
        return "updated"

    def wait_ready(self, collection: str, name: str, timeout: int = 180) -> tuple[bool, str]:
        deadline = time.time() + timeout
        message = "no status yet"
        while time.time() < deadline:
            obj = self.get(f"{PROV}/{collection}/{name}") or {}
            status = obj.get("status", {})
            if status.get("observedGeneration") == obj.get("metadata", {}).get("generation"):
                health = status.get("health", {})
                conditions = {c["type"]: c for c in status.get("conditions", [])}
                ready = conditions.get("Ready", {})
                if health.get("healthy") and ready.get("status", "True") == "True":
                    return True, "healthy"
                message = (ready.get("message")
                           or " ".join(health.get("message", []) or [])
                           or json.dumps(status.get("fieldErrors", [])))
                if ready.get("status") == "False":
                    return False, message
            time.sleep(5)
        return False, f"timed out after {timeout}s ({message})"


def status_line(obj: dict | None) -> str:
    if obj is None:
        return "absent"
    status = obj.get("status", {})
    health = status.get("health", {})
    bits = [f"healthy={health.get('healthy')}"]
    for c in status.get("conditions", []):
        bits.append(f"{c['type']}={c['status']}({c.get('reason')})")
    for m in health.get("message", []) or []:
        bits.append(f"— {m}")
    sync = status.get("sync")
    if sync:
        bits.append(f"sync={sync.get('state')} ref={sync.get('lastRef', '')[:8]}")
    return " ".join(bits)


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    root = Path(__file__).resolve().parent.parent
    load_env()

    token = os.environ.get("GRAFANA_GITSYNC_TOKEN", "").strip()
    if token:
        pem = None
        app_id = installation_id = ""
    else:
        pem = Path(env("GRAFANA_GITHUB_PRIVATE_KEY_FILE")).expanduser()
        if not pem.is_absolute():
            pem = (root / pem).resolve()
        if not pem.is_file():
            die(f"private key not found: {pem}")
        app_id = env("GRAFANA_GITHUB_APP_ID")
        installation_id = env("GRAFANA_GITHUB_INSTALLATION_ID")

    conn_name = env("GRAFANA_GITSYNC_CONNECTION", "github")
    repo_name = env("GRAFANA_GITSYNC_REPOSITORY", "grafana")
    repo_url = env("GRAFANA_GITSYNC_REPO_URL")
    branch = env("GRAFANA_GITSYNC_BRANCH")
    path = env("GRAFANA_GITSYNC_PATH", "dashboards")

    g = Grafana(base_url(), env("GRAFANA_ADMIN_USER"), env("GRAFANA_ADMIN_PASSWORD"))

    print(f"grafana      {g.url}")
    print("auth         " + (f"github app {app_id} installation {installation_id}"
                             if pem else "personal access token on the repository (no connection)"))
    print(f"repository   {repo_url} branch {branch} path {path}/")

    missing = check_app_permissions(pem, app_id) if pem else []
    if missing:
        print("\nGitHub App is missing permissions Grafana requires:")
        for m in missing:
            print(f"  - {m}")
        print("\nFix: organisation settings -> GitHub Apps -> the app -> Permissions & events")
        print("     -> Repository permissions, then accept the update on the installation.")
        print("Grafana checks all four unconditionally (validateAppPermissions in")
        print("apps/provisioning/pkg/connection/github/connection.go); webhooks is required")
        print("even though this instance is not reachable for webhook delivery.")
        if not check_only:
            return 2
    elif pem:
        print("github app   permissions OK")

    if check_only:
        print("\nconnection   " + status_line(g.get(f"{PROV}/connections/{conn_name}")))
        print("repository   " + status_line(g.get(f"{PROV}/repositories/{repo_name}")))
        return 0

    if pem:
        connection = {
            "apiVersion": "provisioning.grafana.app/v0alpha1",
            "kind": "Connection",
            "metadata": {"name": conn_name, "namespace": "default"},
            "spec": {
                "title": env("GRAFANA_GITSYNC_CONNECTION_TITLE", "GitHub"),
                "type": "github",
                "github": {"appID": app_id, "installationID": installation_id},
            },
            # Grafana wants the PEM base64-encoded on the wire; it is write-only and
            # never reads back, so it is re-sent on every apply.
            "secure": {"privateKey": {"create": base64.b64encode(pem.read_bytes()).decode()}},
        }
        print(f"\nconnection   {g.apply('connections', conn_name, connection)}")
        ok, msg = g.wait_ready("connections", conn_name)
        print(f"connection   {'ready' if ok else 'NOT ready: ' + msg}")
        if not ok:
            return 2

    repository = {
        "apiVersion": "provisioning.grafana.app/v0alpha1",
        "kind": "Repository",
        "metadata": {"name": repo_name, "namespace": "default"},
        "spec": {
            "title": env("GRAFANA_GITSYNC_REPOSITORY_TITLE", "Grafana"),
            "type": "github",
            "github": {"url": repo_url, "branch": branch, "path": path,
                       "generateDashboardPreviews": False},
            "sync": {"enabled": True, "target": "instance", "intervalSeconds": 60},
            "workflows": ["write"],
        },
    }
    if pem:
        repository["spec"]["connection"] = {"name": conn_name}
    else:
        repository["secure"] = {"token": {"create": token}}
    print(f"repository   {g.apply('repositories', repo_name, repository)}")
    ok, msg = g.wait_ready("repositories", repo_name, timeout=300)
    print(f"repository   {'healthy' if ok else 'NOT healthy: ' + msg}")
    print("repository   " + status_line(g.get(f"{PROV}/repositories/{repo_name}")))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
