#!/usr/bin/env bash
# Restore this repo's snapshot into a Grafana instance: datasources (same uids, DUMMY
# passwords), folders, dashboards. Alerting is NOT restored here — contact points
# carry secrets this snapshot may not hold; use scripts/grafana-sync.py push.
#
#   scripts/grafana-restore.sh http://localhost:3011 admin:password
set -euo pipefail
G="${1%/}"; AUTH="$2"; ROOT="$(cd "$(dirname "$0")/.." && pwd)"
api() { curl -sS -m 60 -u "$AUTH" -H 'Content-Type: application/json' "$G$1" "${@:2}"; }
jq -c '.[] | {uid,name,type,url,access,database,jsonData,isDefault, secureJsonData:{password:"dummy"}}' "$ROOT/instance/datasources.json" | while read -r ds; do
  api /api/datasources -X POST -d "$ds" | jq -r '"datasource \(.datasource.uid // .message)"'; done
jq -c '.[] | select(.uid!="sharedwithme") | {uid,title}' "$ROOT/instance/folders.json" | while read -r f; do
  api /api/folders -X POST -d "$f" | jq -r '"folder \(.uid // .message)"'; done
# Dashboards. A file is either a classic body or Grafana's Git Sync wrapper
# ({apiVersion,kind,metadata,spec}) — `.spec // .` takes the body from either, and
# the uid comes from metadata.name in the wrapper, where the body no longer carries one.
for f in "$ROOT"/dashboards/*/*.json; do
  uid=$(jq -r '.metadata.name // .uid // ""' "$f")
  fuid=$(jq -r '.folderUid // ""' "$ROOT/instance/dashboard-meta/$uid.json" 2>/dev/null || echo "")
  jq -c --arg fu "$fuid" --arg uid "$uid" \
     '{dashboard:((.spec // .) | .id=null | .uid=$uid), folderUid:$fu, overwrite:true}' "$f" \
    | api /api/dashboards/db -X POST -d @- | jq -r '"dashboard \(.uid // .message) \(.status // "")"'
done
