---
name: ga4-country-conversion-rate
description: Pull country-level install→purchase conversion, paywall-shown→purchase conversion (by paywall_type × paywall_source), and Day-0 same-session purchases from Firebase Analytics via the GA4 Data API. Uses `first_open` (auto) as the install denominator and `purchase_v2` (custom) as the purchase numerator, with automatic fallback to Firebase's `in_app_purchase` when `purchase_v2` is absent in the window. Outputs structured JSON suitable for downstream impact analysis (joinable with firebase-remote-config outputs by `date_range`). Use when the user asks for install-to-purchase conversion, paywall conversion, Day-0 purchases, or wants to measure the metric effect of a Remote Config change after the fact.
---

# GA4 Country Conversion Rate — Country / Paywall / Day-0

Read-only skill that answers:

1. **Country conversion** — `first_open` (installs) vs purchase event, by country, over `[A, B]`.
2. **Paywall conversion** — `paywall_shown` vs `purchase_v2`, broken down by event-scoped custom dimensions `paywall_type` × `paywall_source`.
3. **Day-0 purchases** — purchases whose event date equals the user's `firstSessionDate`, by country.

For (1) and (3) the skill **probes `purchase_v2` first** and **auto-falls back to Firebase's `in_app_purchase`** when the custom event has 0 rows in the window — when this happens the output JSON sets `purchase_event_used: "in_app_purchase"` plus a `fallback` block, and the human render prints a `⚠` banner so the user knows the auto event includes renewals/restorations and is not directly comparable to historical `purchase_v2` numbers.

Lives alongside [[firebase-remote-config]]: investigate "what changed in RC" with that skill, then "did conversion actually move" with this one — they share `/tmp/cc-*.json` ↔ `/tmp/rc-*.json` joinable by `date_range`.

## Scripts (already installed, do not rewrite)

All helper scripts live in `~/.claude/skills/ga4-country-conversion-rate/scripts/`. They are **persistent and idempotent** — do not re-create them in `/tmp/` each session.

Refer to this dir as `$SKILL_DIR` below:
```bash
SKILL_DIR="$HOME/.claude/skills/ga4-country-conversion-rate/scripts"
```

Files in `$SKILL_DIR/`:
- `ga-fetch.sh`    — auth resolution (env `$TOKEN` → gcloud ADC → service-account JWT), token cache at `/tmp/ga-token.json`, and three subcommands: `token`, `runreport <prop> <body.json>`, `probe-event <prop> <event> <start> <end>`, `resolve-property`.
- `ga-country.py`  — Op 1: single `runReport`, pivot, optional `--min-installs`. Integrates purchase-event fallback.
- `ga-paywall.py`  — Op 2: queries `customEvent:paywall_type` / `customEvent:paywall_source`; clear errors when those aren't registered or when `purchase_v2` is absent (no fallback applies).
- `ga-day0.py`     — Op 3: two `runReport` calls + post-join. Integrates purchase-event fallback.
- `ga-render.py`   — human render with subcommand dispatch (`country | paywall | day0`); prints fallback / sampling / `(other)`-collapse banners.

Output files all land in `/tmp/`:
- `/tmp/cc-country-<start>-<end>.json`
- `/tmp/cc-paywall-<start>-<end>.json`
- `/tmp/cc-day0-<start>-<end>.json`
- `/tmp/ga-token.json` (auth cache; safe to delete to force re-mint)

`ga-fetch.sh` reuses the cached bearer until `exp - 60 s`. Delete the cache file to force a fresh mint.

## Prerequisites — resolve in this order

1. **Firebase project ID** — read `projects.default` from `.firebaserc` in cwd, or `PROJECT_ID` from a `GoogleService-Info.plist` / `google-services.json`. If neither is present, ask the user.

2. **GA4 property ID** — numeric. No reliable CLI mapping from Firebase project → GA4 property. Export once per shell:
   ```bash
   export GA4_PROPERTY_ID=<numeric-property-id>
   ```
   Find it in the Firebase Console → Project Settings → Integrations → Google Analytics → "Property ID", or fetch programmatically via the Firebase Management API's `analyticsDetails` endpoint with a Firebase-scope bearer:
   ```bash
   curl -sS -H "Authorization: Bearer $TOKEN" \
     "https://firebase.googleapis.com/v1beta1/projects/<project_id>/analyticsDetails" \
     | jq '.analyticsProperty'
   ```

3. **Auth** — `ga-fetch.sh` auto-selects in this order:
   - **`$TOKEN`** env var if set (use for one-off testing).
   - **`gcloud auth application-default print-access-token`** if `gcloud` is installed and authed.
   - **Service-account JWT** minted from `$GOOGLE_APPLICATION_CREDENTIALS` (or its alias `$GOOGLE_ANALYTICS_CREDENTIALS_PATH`) with scope `https://www.googleapis.com/auth/analytics.readonly` — see [Auth resolution — REST fallback](#auth-resolution--rest-fallback).
   The principal needs **`Viewer`** on the GA4 property — granted in **GA4 Admin → Property → Property Access Management** (NOT in the GCP IAM page; Property Access Management is a separate GA4-side ACL).

   **Do NOT try to reuse the Firebase CLI's cached token** at `~/.config/configstore/firebase-tools.json`. It carries `cloud-platform` scope, which sounds broad but the GA4 Data API specifically rejects it with `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`. Only `analytics.readonly` (or a token explicitly minted with that scope) works.

   **Example setup** (replace placeholders with your own values):
   ```bash
   export GA4_PROPERTY_ID=<numeric-property-id>
   export GOOGLE_APPLICATION_CREDENTIALS=<absolute-path-to-service-account.json>
   ```
   Note: a service account originally created for a different GCP project can still be used here — what matters is that its email has been granted Viewer in the target GA4 property's Access Management. Cross-project naming is fine; the GA4 ACL is what's checked.

4. **For Op 2 only**: `paywall_type` and `paywall_source` registered as **event-scoped custom dimensions** in GA4 Admin → Custom Definitions. 24–48 h latency before queryable; no backfill of older data.

5. **Tools** — `curl`, `jq`, `python3` (always). The SA-JSON token path needs `cryptography`; install once:
   ```bash
   python3 -m pip install --user cryptography
   ```
   On macOS this installs to `~/Library/Python/<ver>/lib/python/site-packages`; the system `/usr/bin/python3` picks it up automatically. If the import still fails, check `python3 -c "import sys; print(sys.path)"` — `~/Library/Python/.../site-packages` should appear.

If a prerequisite is missing, stop and tell the user the one specific thing that's blocked rather than dumping the full list.

## Core concept: events used

| Event | Source | Used as |
|---|---|---|
| `first_open` | Firebase auto (fires once per user/install) | install denominator (Op 1, Op 3) |
| `purchase_v2` | Custom event you log from your app; expected params: `paywall_type`, `paywall_source`, `value`, `currency`, `product_id`, `transaction_id` | purchase numerator (all ops) |
| `in_app_purchase` | Firebase auto on every successful StoreKit transaction (renewals, restorations included) | purchase fallback (Op 1, Op 3 only — auto event lacks paywall_* params so it can't fall back for Op 2) |
| `paywall_shown` | Custom event you log from your app; expected params: `paywall_type`, `paywall_source` | paywall denominator (Op 2) |

### Purchase-event fallback (Op 1 and Op 3)

Each script probes `eventCount` for `purchase_v2` in the window via `ga-fetch.sh probe-event` before issuing the main report. If 0, it switches to `in_app_purchase`. The output JSON records the resolved event in `purchase_event_used` and writes a `fallback` block when the fallback fired. `ga-render.py` surfaces this with a top-of-output `⚠` banner. Override with `--purchase-event {auto|purchase_v2|in_app_purchase}` (default `auto`).

## Operations

In all examples below: `PROP="${GA4_PROPERTY_ID}"`, `SKILL_DIR=$HOME/.claude/skills/ga4-country-conversion-rate/scripts`.

### Op 1 — Country conversion

```bash
START=2026-04-01
END=2026-05-01

python3 "$SKILL_DIR/ga-country.py" \
  --start "$START" --end "$END" \
  --min-installs 50

python3 "$SKILL_DIR/ga-render.py" country "/tmp/cc-country-${START}-${END}.json"
```

Each row: `{country_id, country, installs, purchases, rate}`. Sorted by `installs` desc.

Edge cases:
- `--min-installs N` filters in Python — re-runnable without re-querying.
- Bypass the fallback with `--purchase-event purchase_v2`; force it with `--purchase-event in_app_purchase`.
- GA4 omits zero-count rows; the pivot defaults missing `purchases` to 0 so countries with installs and no purchases stay visible.

### Op 2 — Paywall conversion

```bash
python3 "$SKILL_DIR/ga-paywall.py" \
  --start "$START" --end "$END" \
  --min-shown 50

python3 "$SKILL_DIR/ga-render.py" paywall "/tmp/cc-paywall-${START}-${END}.json"
```

Each row: `{paywall_type, paywall_source, shown, purchases, rate}`. `(not set)` is a separate row — these are events where the param was absent or the custom dimension was unregistered at ingest time.

Edge cases:
- Errors out with a targeted message if `customEvent:paywall_type` / `customEvent:paywall_source` aren't registered yet (or if registered but no data has surfaced — wait 24–48 h).
- Errors out if `purchase_v2` has 0 events in the window — the `in_app_purchase` fallback is not applicable here because the auto event lacks paywall_* params.

### Op 3 — Day-0 purchases

```bash
python3 "$SKILL_DIR/ga-day0.py" \
  --start "$START" --end "$END" \
  --min-users 50

python3 "$SKILL_DIR/ga-render.py" day0 "/tmp/cc-day0-${START}-${END}.json"
```

Each row: `{country_id, day0_users, day0_purchases, day0_rate}`. Two GA4 requests + post-join: purchases filtered to `eventDate == firstSessionDate`, then divided by `first_open` `totalUsers` per `(country, date)`.

Edge cases:
- Users acquired before GA4 was enabled have `firstSessionDate = (not set)` — those rows are dropped (the `date == firstSessionDate` predicate can't hold).
- Same fallback semantics as Op 1.

## Auth resolution — REST fallback

`ga-fetch.sh` already handles all three auth paths. The inline minter below is what runs when only `$GOOGLE_APPLICATION_CREDENTIALS` (or its alias `$GOOGLE_ANALYTICS_CREDENTIALS_PATH`) is set (no gcloud, no `$TOKEN`). Same JWT shape as [[firebase-remote-config]] with one change: **scope is `analytics.readonly`** instead of `firebase`.

```bash
export TOKEN=$(python3 <<'PY'
import json, time, base64, urllib.request, urllib.parse, os
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
c = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))
now = int(time.time())
header = {"alg":"RS256","typ":"JWT","kid":c["private_key_id"]}
claim = {"iss":c["client_email"],
         "scope":"https://www.googleapis.com/auth/analytics.readonly",
         "aud":"https://oauth2.googleapis.com/token","iat":now,"exp":now+3600}
b64=lambda d: base64.urlsafe_b64encode(json.dumps(d,separators=(",",":")).encode()).rstrip(b"=")
si = b64(header)+b"."+b64(claim)
key = serialization.load_pem_private_key(c["private_key"].encode(), password=None)
sig = key.sign(si, padding.PKCS1v15(), hashes.SHA256())
jwt = (si+b"."+base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()
data = urllib.parse.urlencode({"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer","assertion":jwt}).encode()
print(json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data).read())["access_token"])
PY
)
```

The SA email must be granted **`Viewer` in GA4 Property Access Management** — *not* GCP IAM. Project-scope IAM roles do not propagate to GA4.

The minter requires the Python `cryptography` package. If absent, the script exits with `ModuleNotFoundError: No module named 'cryptography'`. Install once:
```bash
python3 -m pip install --user cryptography
```

If none of the three auth paths are available, prompt the user for an SA JSON path or to install gcloud (`gcloud auth application-default login`). Do not proceed without auth.

### What does NOT work as auth

- **Firebase CLI's cached OAuth token** (`~/.config/configstore/firebase-tools.json`). Its declared scopes include `cloud-platform` and `firebase`, but the GA4 Data API specifically rejects it with `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT` — only `analytics.readonly` is accepted. Don't read that file as a shortcut; you'll silently get authentication failures (`probe-event` returns 0 with no rows; fixed in the current script to surface the API error instead).
- **Tokens scoped to `cloud-platform` only**, even from gcloud. When using gcloud, request the analytics scope explicitly:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/cloud-platform
  ```

## Output style for the user

After running, summarize from `ga-render.py`'s output. The render script already prints a header + fallback / sampling banner — surface that, then add a one-liner with the headline finding:

```
property: <id>   project: <firebase-project-id>
window:   <start> → <end>   op: country   purchase_event: <event>
fetched:  <ts>              rows: <N>

⚠ purchase_v2 had 0 events in window — fell back to in_app_purchase (Firebase auto event).
   Reason: probe returned 0 events for purchase_v2 in the window

country_id  country                         installs   purchases      rate
------------------------------------------------------------------------
us          United States                      12034         421     3.50%
…

(headline: e.g. "US is at 3.5%; Day-0 at 1.7%. Top mover vs prior window: …")
```

If `fallback` is non-null, lead with the fallback note so the user sees it before the numbers.

## For downstream impact analysis

`/tmp/cc-country-*.json`, `/tmp/cc-paywall-*.json`, `/tmp/cc-day0-*.json` are the canonical inputs. Each file carries:

- `property_id`, `firebase_project_id` — join keys.
- `date_range: {start, end}` — match the same field on `/tmp/rc-diff-*.json` / `/tmp/rc-changelog-*.json` from [[firebase-remote-config]] for before/after triage.
- `purchase_event_used` + `fallback` — distinguish "purchase_v2 baseline" from "in_app_purchase fallback" (the latter inflates counts due to renewals/restorations).
- `sampling` + `data_loss_from_other_row` — accuracy flags; treat results as approximate when either is set.
- `rows` — per-bucket counts and rates (zero rows are kept as 0 in the pivot, not dropped).

When the user follows up with "now measure the impact," **do not re-fetch** — read the existing JSON from `/tmp/`. If older than 24 h or missing, re-run.

## Triage heuristics for the impact layer

- **Fallback in effect**: `purchase_event_used == "in_app_purchase"` → purchase counts are over-inflated relative to a historical `purchase_v2` baseline. Compare windows that both fell back, or both did not — never mix.
- **Sampled report**: `sampling.is_sampled == true` → conversion rate is an approximation. Tighten the date window or scope by country to reduce event volume below the sampling threshold.
- **`(other)` collapse**: `data_loss_from_other_row == true` → country-level accuracy is lost. Most relevant for Op 3 (3-dim request); shorten the window or split by month.
- **`(not set)` row in Op 2 dominates**: most purchases lack `paywall_type` / `paywall_source` → either custom dimensions are not registered, or events were ingested before registration completed. Wait 24–48 h after registration and re-run; older data is not backfilled.

## Gotchas

- GA4 free-tier sampling kicks in past ~10 M events per property per window. Watch `sampling.is_sampled` and the render banner.
- `(other)` row appears when row cardinality exceeds the limit (~50 k free tier). Watch `data_loss_from_other_row`.
- Quota: free tier has tokens-per-day / per-hour / concurrent-request limits per property. The skill issues 2–3 requests per op; safe under normal use.
- Custom dimensions take 24–48 h after registration before queryable, and **older event data is not backfilled**.
- Use `country_id` (ISO-3166-1 alpha-2 lowercase, e.g. `us`) as the join key — `country` is a localized display name and can vary.
- `IS_ANALYTICS_ENABLED=false` in `GoogleService-Info.plist` (iOS) / `google-services.json` (Android) disables auto-collection at the SDK level. If it's genuinely off at runtime (no `Analytics.setAnalyticsCollectionEnabled(true)` after consent), event volume will be near zero and every report will look empty.
- Date format: `--start` / `--end` are `YYYY-MM-DD` inclusive on both ends. **Not** RFC3339 like [[firebase-remote-config]] — different API.
- `purchase_v2` (custom, per `Store.swift:362`) and `in_app_purchase` (Firebase auto) are not equivalent: the auto event fires on every successful StoreKit transaction including renewals and restorations, while `purchase_v2` fires on a curated set. Expect inflated counts when the fallback is in effect.
- `firstSessionDate (not set)` rows in Op 3 are dropped (the `date == firstSessionDate` predicate can't hold).
- `/tmp/ga-token.json` caches the bearer until `exp - 60 s`. To force re-mint: `rm /tmp/ga-token.json`.
- GA4 omits zero-count rows. The skill's pivot defaults missing columns to 0 so countries with installs and no purchases remain visible.
