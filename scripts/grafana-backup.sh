#!/usr/bin/env bash
# Snapshot EVERYTHING a Grafana instance holds, into this repo, normalised (jq -S)
# so re-runs diff line by line.
#
#   scripts/grafana-backup.sh [out-dir]                # default: repo root
#
# Covers dashboards/ and instance/. ALERTING IS NOT HERE — `scripts/grafana-sync.py pull`
# owns alerting/, because it is also what pushes it.
#
# Configuration comes from .env (loaded automatically) or the environment.
#
# Auth: GRAFANA_ADMIN_USER + GRAFANA_ADMIN_PASSWORD (Grafana admin) if both are
# set, else the GRAFANA_API_KEY service-account token. Admin unlocks two things
# the service account is refused: the decrypted alerting export (webhook URLs,
# tokens) and /api/admin/settings.
#
# Read-only against Grafana: every request is a GET.
#
# GRAFANA_DECRYPT=false forces the redacted export even with admin credentials.
# With decryption ON the output carries live secrets, so it may only be
# committed to a private repo — the script says what it wrote.
set -euo pipefail
# Load .env when the caller has not already, so every script in this repo takes
# its configuration from the same one place.
if [[ -z "${GRAFANA_URL:-}" ]]; then
  ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
  [[ -f "$ENV_FILE" ]] && { set -a; . "$ENV_FILE"; set +a; }
fi
: "${GRAFANA_URL:?}"
G="${GRAFANA_URL%/}"; OUT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

if [[ -n "${GRAFANA_ADMIN_USER:-}" && -n "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  AUTH=(-u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD"); ADMIN=1
else
  : "${GRAFANA_API_KEY:?}"; AUTH=(-H "Authorization: Bearer $GRAFANA_API_KEY"); ADMIN=0
fi
DECRYPT="${GRAFANA_DECRYPT:-$([[ $ADMIN == 1 ]] && echo true || echo false)}"
[[ $ADMIN == 1 ]] || DECRYPT=false          # the service account is refused decrypt=true

# --retry: a first connect over a VPN link can stall for tens of seconds
# after an idle period, which is not the server being down.
CURL=(curl -sS --connect-timeout 30 -m 120 --retry 3 --retry-delay 2 --retry-all-errors)
# Every `api` endpoint returns JSON, and this link resets *mid-body* often enough that
# curl exits 0 with a truncated response — which then dies in jq with "Invalid numeric
# literal" halfway through a backup. curl cannot retry a body it already started, so the
# retry lives here: the answer is not accepted until it parses.
api() {
  local i body
  for i in 1 2 3 4; do
    # `jq .` not `jq -e .`: -e reports the VALUE's truthiness, so a perfectly good
    # `null` body (an instance with no templates) would look like a failed request.
    if body=$("${CURL[@]}" --fail-with-body "${AUTH[@]}" "$G$1") \
       && jq . >/dev/null 2>&1 <<<"$body"; then
      printf '%s' "$body"; return 0
    fi
    sleep $((i * 2))
  done
  echo "api $1: no complete JSON response after 4 attempts" >&2
  return 1
}
# Endpoints that only exist on some instances (feature toggles, editions).
opt() { local b; b=$("${CURL[@]}" "${AUTH[@]}" "$G$1" 2>/dev/null || true); jq -e . >/dev/null 2>&1 <<<"$b" && printf '%s' "$b" || echo 'null'; }
save() { local path="$STAGE/$1"; mkdir -p "$(dirname "$path")"; jq -S . > "$path"; }
# Grafana returns permission lists in an arbitrary order, so `jq -S` alone leaves two
# identical snapshots diffing by a swapped Viewer/Editor pair. Order carries no meaning
# in these lists (unlike a dashboard's panels), so sort them.
save_set() { local path="$STAGE/$1"; mkdir -p "$(dirname "$path")"; jq -S 'sort_by(tostring)' > "$path"; }
# Build into a staging directory and swap it in only once every fetch has succeeded.
# The previous version deleted the three trees up front, so any mid-run failure — and
# this link fails often — left a half-written snapshot where a complete one had been.
STAGE=$(mktemp -d "$OUT/.backup-staging.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/dashboards" "$STAGE/instance"

# ── instance ────────────────────────────────────────────────────────────────
api /api/health                          | save instance/health.json
api /api/frontend/settings | jq '{buildInfo, featureToggles}' | save instance/build-and-feature-toggles.json
api '/api/folders?limit=1000'            | save instance/folders.json
api /api/datasources                     | save instance/datasources.json
api '/api/plugins?enabled=1' | jq 'map({id,name,type,info:{version:.info.version}})' | save instance/plugins.json
api '/api/library-elements?perPage=100'  | save instance/library-panels.json
api /api/playlists                       | save instance/playlists.json
api /api/org/preferences                 | save instance/org-preferences.json
api '/api/teams/search?perpage=100'      | save instance/teams.json
api '/api/org/users'                     | save instance/org-users.json
opt '/api/orgs'                          | save instance/orgs.json
opt '/api/serviceaccounts/search?perpage=100' | save instance/service-accounts.json
# Effective grafana.ini as the server sees it. Grafana redacts secret keys itself.
[[ $ADMIN == 1 ]] && api /api/admin/settings | save instance/settings.json
# Git Sync connections (empty until the provisioning feature toggle is on).
opt '/apis/provisioning.grafana.app/v0alpha1/namespaces/default/repositories' | save instance/provisioning-repositories.json

for f in $(jq -r '.[].uid' "$STAGE/instance/folders.json"); do
  api "/api/folders/$f/permissions" | save_set "instance/folder-permissions/$f.json"
done

# ── alerting ────────────────────────────────────────────────────────────────
# NOT here. `scripts/grafana-sync.py pull` owns alerting/ — it is the script that also
# PUSHES alerting, so one thing decides that directory's shape. This script used to write
# it too, and two writers for one directory is exactly the split ownership this repo keeps
# being bitten by. The alertmanager config stays, because nothing else captures it.
api /api/alertmanager/grafana/config/api/v1/alerts     | save instance/alertmanager-config.json

# ── dashboards ──────────────────────────────────────────────────────────────
# Bodies only, one file per dashboard, under the folder it lives in — the layout
# Grafana Git Sync expects. Everything else about a dashboard is a sidecar under
# instance/, so nothing but dashboard JSON is ever inside dashboards/.
index=$(api '/api/search?type=dash-db&limit=5000')
jq -r '.[] | "\(.uid)\t\(.folderTitle // "General")"' <<<"$index" | while IFS=$'\t' read -r uid folder; do
  full=$(api "/api/dashboards/uid/$uid")
  jq '.dashboard' <<<"$full" | save "dashboards/$folder/$uid.json"
  jq '.meta | {folderUid, folderTitle, version, created, createdBy, updated, updatedBy, provisioned, provisionedExternalId, slug, url}' <<<"$full" \
    | save "instance/dashboard-meta/$uid.json"
  api "/api/dashboards/uid/$uid/permissions" | save_set "instance/dashboard-permissions/$uid.json"
done
jq -S . <<<"$index" | save instance/dashboard-index.json

# Once the instance can serve dashboard.grafana.app, rewrite the bodies above into the
# exact bytes Grafana Git Sync writes. Without this the backup and the seeder disagree
# and each re-run would undo the other, which is the churn this repo exists to avoid.
#
# The probe MUST distinguish "this instance has no dashboard.grafana.app" (a legitimate
# instance without the kubernetesDashboards toggle — keep the classic bodies) from "the
# link failed" (retry, then refuse to publish). A transport failure treated as the former
# silently republishes every dashboard in the wrong format and un-seeds the repo, which
# is exactly what happened once after five `Connection reset by peer`s.
PROBE_CODE=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${AUTH[@]}" \
  "$G/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards" 2>/dev/null || echo 000)
case "$PROBE_CODE" in
  2*) "$(dirname "$0")/grafana-canonicalize-dashboards.py" "$STAGE" ;;
  404|501) echo "note: no dashboard.grafana.app (HTTP $PROBE_CODE) — keeping classic bodies" >&2 ;;
  *)  echo "dashboard.grafana.app probe failed (HTTP $PROBE_CODE) — refusing to publish a snapshot" >&2
      echo "in the other format; the staged copy is discarded and $OUT is untouched." >&2
      exit 1 ;;
esac

# Everything fetched. Swap the staged trees in.
for d in dashboards instance; do
  rm -rf "$OUT/$d"; mv "$STAGE/$d" "$OUT/$d"
done

# ── what did we just write ──────────────────────────────────────────────────
SECRETS='chat\.googleapis\.com/v1/spaces/[^"]*key=|glsa_[A-Za-z0-9_]{20,}|"password": *"[^"]+"|hooks\.slack\.com/services/'
mapfile -t hits < <(/usr/bin/grep -rIlE "$SECRETS" "$OUT/dashboards" "$OUT/instance" || true)
if [[ ${#hits[@]} -gt 0 ]]; then
  if [[ $DECRYPT == true ]]; then
    printf 'secrets present (decrypt=true) — PRIVATE REPO ONLY:\n'; printf '  %s\n' "${hits[@]}"
  else
    printf 'SECRET-LOOKING STRING FOUND — not safe to commit:\n' >&2; printf '  %s\n' "${hits[@]}" >&2; exit 1
  fi
fi
echo "ok: $(jq length <<<"$index") dashboards, $(jq length "$OUT/instance/folders.json") folders, $(jq length "$OUT/instance/datasources.json") datasources, decrypt=$DECRYPT -> $OUT"
echo "note: alerting/ is not part of this snapshot — run scripts/grafana-sync.py pull" >&2
