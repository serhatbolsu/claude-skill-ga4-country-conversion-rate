#!/usr/bin/env bash
# Thin wrapper around the GA4 Data API (analyticsdata.googleapis.com:runReport).
# Handles auth (service-account JWT minting with token cache) and a probe helper
# to check whether a given event has any rows in a date window.
#
# Usage:
#   ga-fetch.sh token
#   ga-fetch.sh runreport <property_id> <body_json_path>
#   ga-fetch.sh probe-event <property_id> <event_name> <start> <end>
#   ga-fetch.sh resolve-property
#
# Environment:
#   GA4_PROPERTY_ID                numeric GA4 property id (e.g. 123456789)
#   GOOGLE_APPLICATION_CREDENTIALS path to a service-account JSON
#   TOKEN                          bearer token (skips minting; useful for testing)
#   GA_SCOPE                       OAuth scope (defaults to analytics.readonly)
#
# Token cache:
#   /tmp/ga-token.json   {"access_token": "...", "exp": <unix_ts>}
#   Re-mints when exp - 60s <= now. Delete the file to force re-mint.

set -euo pipefail

OP="${1:?op required: token|runreport|probe-event|resolve-property}"
shift

TOKEN_CACHE="/tmp/ga-token.json"
GA_SCOPE="${GA_SCOPE:-https://www.googleapis.com/auth/analytics.readonly}"

mint_token() {
  if [ -n "${TOKEN:-}" ]; then
    # Caller supplied a bearer directly. Cache for 50 minutes so subsequent
    # calls in the same shell don't re-process.
    jq -n --arg t "$TOKEN" --argjson e "$(($(date +%s) + 3000))" \
      '{access_token: $t, exp: $e}' > "$TOKEN_CACHE"
    return
  fi
  if command -v gcloud >/dev/null 2>&1; then
    local t
    t=$(gcloud auth application-default print-access-token 2>/dev/null || true)
    if [ -n "$t" ]; then
      jq -n --arg t "$t" --argjson e "$(($(date +%s) + 3000))" \
        '{access_token: $t, exp: $e}' > "$TOKEN_CACHE"
      return
    fi
  fi
  # Allow GOOGLE_ANALYTICS_CREDENTIALS_PATH as an alias for GOOGLE_APPLICATION_CREDENTIALS.
  if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -n "${GOOGLE_ANALYTICS_CREDENTIALS_PATH:-}" ]; then
    export GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_ANALYTICS_CREDENTIALS_PATH}"
  fi
  if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] || [ ! -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
    echo "ERROR: cannot mint token." >&2
    echo "  Set GOOGLE_APPLICATION_CREDENTIALS (or GOOGLE_ANALYTICS_CREDENTIALS_PATH) to a" >&2
    echo "  service-account JSON path, or set TOKEN to a pre-minted bearer, or install + auth gcloud." >&2
    echo "  The SA email must also be granted Viewer in GA4 Admin → Property Access Management." >&2
    exit 1
  fi
  GA_SCOPE="$GA_SCOPE" python3 <<'PY' > "$TOKEN_CACHE"
import json, time, base64, urllib.request, urllib.parse, os, sys
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception as e:
    sys.stderr.write("ERROR: `cryptography` not installed. Run: pip install cryptography\n")
    sys.exit(1)
c = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))
now = int(time.time())
header = {"alg": "RS256", "typ": "JWT", "kid": c["private_key_id"]}
claim  = {
    "iss":   c["client_email"],
    "scope": os.environ["GA_SCOPE"],
    "aud":   "https://oauth2.googleapis.com/token",
    "iat":   now,
    "exp":   now + 3600,
}
b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")
si  = b64(header) + b"." + b64(claim)
key = serialization.load_pem_private_key(c["private_key"].encode(), password=None)
sig = key.sign(si, padding.PKCS1v15(), hashes.SHA256())
jwt = (si + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()
data = urllib.parse.urlencode({
    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
    "assertion":  jwt,
}).encode()
try:
    resp = json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data).read())
except urllib.error.HTTPError as e:
    sys.stderr.write("ERROR minting token: " + e.read().decode() + "\n")
    sys.exit(1)
print(json.dumps({"access_token": resp["access_token"], "exp": now + resp.get("expires_in", 3600)}))
PY
}

resolve_token() {
  if [ -f "$TOKEN_CACHE" ]; then
    local exp; exp=$(jq -r '.exp // 0' "$TOKEN_CACHE")
    local now; now=$(date +%s)
    if [ "$exp" -gt "$((now + 60))" ]; then
      jq -r '.access_token' "$TOKEN_CACHE"
      return
    fi
  fi
  mint_token
  jq -r '.access_token' "$TOKEN_CACHE"
}

op_token() {
  resolve_token
}

op_resolve_property() {
  if [ -z "${GA4_PROPERTY_ID:-}" ]; then
    echo "ERROR: GA4_PROPERTY_ID is unset." >&2
    echo "  One-time lookup: Firebase Console → Project Settings → Integrations →" >&2
    echo "  Google Analytics → 'Property ID' (numeric). Export as GA4_PROPERTY_ID." >&2
    exit 1
  fi
  echo "$GA4_PROPERTY_ID"
}

op_runreport() {
  local prop="${1:?property_id required}"
  local body="${2:?body_json_path required}"
  [ -f "$body" ] || { echo "ERROR: body file not found: $body" >&2; exit 1; }
  local tok; tok=$(resolve_token)
  curl -sS -X POST \
    -H "Authorization: Bearer ${tok}" \
    -H "Content-Type: application/json" \
    --data-binary "@${body}" \
    "https://analyticsdata.googleapis.com/v1beta/properties/${prop}:runReport"
}

op_probe_event() {
  local prop="${1:?property_id required}"
  local event="${2:?event_name required}"
  local start="${3:?start (YYYY-MM-DD) required}"
  local end="${4:?end (YYYY-MM-DD) required}"
  local body
  body=$(mktemp -t ga-probe.XXXXXX.json)
  jq -n --arg ev "$event" --arg s "$start" --arg e "$end" '{
    dateRanges: [{startDate: $s, endDate: $e}],
    dimensions: [{name: "eventName"}],
    metrics:    [{name: "eventCount"}],
    dimensionFilter: { filter: {
      fieldName: "eventName",
      stringFilter: {value: $ev}
    }},
    limit: 1
  }' > "$body"
  local resp; resp=$(op_runreport "$prop" "$body")
  rm -f "$body"
  # Surface API errors instead of silently returning 0
  if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
    echo "ERROR from GA4 Data API:" >&2
    echo "$resp" | jq '.error' >&2
    exit 1
  fi
  # eventCount is in metricValues[0].value as a string; emit "0" if no rows
  echo "$resp" | jq -r '
    if (.rows // []) | length == 0 then "0"
    else (.rows[0].metricValues[0].value // "0") end'
}

case "$OP" in
  token)             op_token "$@";;
  runreport)         op_runreport "$@";;
  probe-event)       op_probe_event "$@";;
  resolve-property)  op_resolve_property "$@";;
  *) echo "unknown op: $OP" >&2; exit 2;;
esac
