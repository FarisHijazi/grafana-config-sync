"""Shared configuration for the scripts in this directory: one place that knows
where the settings live and how to authenticate.

Imported rather than duplicated because the scripts' own filenames use hyphens
and cannot be imported from each other.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Load the repo's `.env` without overriding the environment."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def auth_header() -> str:
    """Grafana admin if both credentials are set, else the service-account token.

    Admin is what unlocks the decrypted alerting export, /api/admin/settings and
    the provisioning APIs; the service account is refused all three.
    """
    user, password = os.environ.get("GRAFANA_ADMIN_USER"), os.environ.get("GRAFANA_ADMIN_PASSWORD")
    if user and password:
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return "Bearer " + os.environ["GRAFANA_API_KEY"]


def base_url() -> str:
    return os.environ["GRAFANA_URL"].rstrip("/")


def http_get(path: str, tries: int = 4, timeout: int = 120):
    """GET a Grafana JSON endpoint, retrying transport failures.

    A flaky link (VPN, overloaded proxy) resets mid-response often enough that one attempt
    is not a measurement. An HTTP status is an answer and is raised immediately; only
    transport failures are retried.
    """
    request = urllib.request.Request(base_url() + path, headers={"Authorization": auth_header()})
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last
