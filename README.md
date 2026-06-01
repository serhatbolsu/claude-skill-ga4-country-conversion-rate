# ga4-country-conversion-rate

A Claude Code skill that pulls install-to-purchase conversion data from Firebase Analytics via the GA4 Data API, broken down by country, paywall variant, and same-session (Day-0) cohort.

It is designed to be paired with [firebase-remote-config](https://github.com/serhatbolsu/claude-skill-firebase-remote-config) for impact analysis — "what changed in Remote Config" + "did conversion actually move" — joinable by `date_range`.

## What it does

Three read-only operations against your GA4 property:

1. **Country conversion** — `first_open` (install denominator) vs. purchase event, by country.
2. **Paywall conversion** — `paywall_shown` (denominator) vs. `purchase_v2` (numerator), broken down by `paywall_type` × `paywall_source` event-scoped custom dimensions.
3. **Day-0 purchases** — purchases where the event date equals the user's first session date, by country.

Each operation writes a structured JSON file under `/tmp/cc-*.json` and prints a human-readable summary. The JSON is intended to be the input for downstream analysis (e.g., before/after comparisons around a Remote Config change).

When `purchase_v2` is absent from the window, ops 1 and 3 automatically fall back to Firebase's auto-collected `in_app_purchase` event and flag the substitution in the output (since `in_app_purchase` includes renewals and restorations, the counts are not directly comparable to a historical `purchase_v2` baseline).

## Installation

Drop the directory into Claude Code's user skills folder and (optionally) symlink for development:

```bash
git clone git@github.com:serhatbolsu/claude-skill-ga4-country-conversion-rate.git \
  ~/.claude/skills/ga4-country-conversion-rate
```

Claude Code picks up skills under `~/.claude/skills/<name>/SKILL.md` automatically. After cloning, you can invoke it from a Claude Code session with `/ga4-country-conversion-rate` (or any natural-language phrasing that matches its description).

## Requirements

### Tools

- `curl`, `jq`, `python3` — present on macOS/Linux by default.
- Python `cryptography` package — only required for the service-account JWT path:
  ```bash
  python3 -m pip install --user cryptography
  ```
- `gcloud` CLI — **optional**. If installed and ADC-authed with the `analytics.readonly` scope, the skill will use it; otherwise it falls back to a service-account JWT.

### A GA4 property tied to your Firebase project

You need:

- **Firebase project id** (e.g. `my-app-12345`) — used for labeling output JSON only.
- **GA4 numeric property id** (e.g. `123456789`) — used to query the Data API. Find it in **Firebase Console → Project Settings → Integrations → Google Analytics → "Property ID"**, or fetch it programmatically:
  ```bash
  curl -sS -H "Authorization: Bearer $TOKEN" \
    "https://firebase.googleapis.com/v1beta1/projects/<project-id>/analyticsDetails" \
    | jq '.analyticsProperty'
  ```

### Authentication

The skill auto-selects from three paths, in order:

1. **`$TOKEN`** environment variable, if set (an already-minted bearer token).
2. **`gcloud auth application-default print-access-token`**, if `gcloud` is installed and ADC is authed.
3. **Service-account JWT** minted from `$GOOGLE_APPLICATION_CREDENTIALS` (or its alias `$GOOGLE_ANALYTICS_CREDENTIALS_PATH`), with scope `https://www.googleapis.com/auth/analytics.readonly`.

The principal (user or service account) needs **`Viewer` on the GA4 property** — granted in **GA4 Admin → Property → Property Access Management** (this is a GA4-side ACL, separate from GCP IAM — Project-level IAM roles do not propagate).

> **Do not** try to reuse the Firebase CLI's cached token at `~/.config/configstore/firebase-tools.json`. Its scopes look broad but the GA4 Data API rejects it with `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`. Only `analytics.readonly` is accepted.

> If you go the gcloud route, request the analytics scope explicitly:
> ```bash
> gcloud auth application-default login \
>   --scopes=openid,https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/cloud-platform
> ```

### Events your app must log

The skill assumes your app emits:

| Event | Source | Used as |
|---|---|---|
| `first_open` | Firebase auto (fires once per install) | install denominator (Op 1, Op 3) |
| `purchase_v2` | Custom event you log; expected params: `paywall_type`, `paywall_source`, `value`, `currency`, `product_id`, `transaction_id` | purchase numerator (all ops) |
| `in_app_purchase` | Firebase auto (every StoreKit / Play Billing transaction, including renewals) | purchase fallback (Op 1, Op 3 only) |
| `paywall_shown` | Custom event you log; expected params: `paywall_type`, `paywall_source` | paywall denominator (Op 2) |

For **Op 2**, `paywall_type` and `paywall_source` must be registered as **event-scoped custom dimensions** in GA4 Admin → Custom Definitions. There is a 24–48 h latency before they become queryable, and **older event data is not backfilled** when you register them.

If you don't log `purchase_v2`, ops 1 and 3 will silently use `in_app_purchase` instead and flag it in the output. Op 2 has no fallback because the auto event lacks the paywall params.

## Quick start

```bash
# 1. Set required env (per shell)
export GA4_PROPERTY_ID=<numeric-property-id>
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export FIREBASE_PROJECT_ID=<firebase-project-id>   # optional, used only for labeling

# 2. Country conversion over a date range
SKILL_DIR="$HOME/.claude/skills/ga4-country-conversion-rate/scripts"
python3 "$SKILL_DIR/ga-country.py" --start 2026-04-01 --end 2026-05-01 --min-installs 50
python3 "$SKILL_DIR/ga-render.py" country "/tmp/cc-country-2026-04-01-2026-05-01.json"
```

Or just ask Claude inside a session that has the skill enabled — e.g. "Pull country conversion for last month."

## Output format

Each op writes to `/tmp/cc-*.json` with:

- `property_id`, `firebase_project_id` — join keys.
- `date_range: {start, end}` — match against other tools' outputs (e.g. firebase-remote-config) for impact triage.
- `purchase_event_used` + `fallback` — distinguishes `purchase_v2` baseline from `in_app_purchase` fallback.
- `sampling` + `data_loss_from_other_row` — accuracy flags.
- `rows` — per-bucket counts and rates.

## Gotchas

- GA4 free-tier **sampling** kicks in past ~10M events per property per window. The skill flags this via `sampling.is_sampled` and shows a banner.
- GA4 free-tier **row cap** is ~50k; excess rows collapse into `(other)`. Flagged via `data_loss_from_other_row`.
- `purchase_v2` vs. `in_app_purchase` are **not equivalent**. The auto event fires on every StoreKit/Play transaction including renewals and restorations; the custom event fires on whatever subset your code chooses. Don't mix windows where one fell back and one didn't.
- Custom dimensions take 24–48 h to become queryable after registration, and older data is **not backfilled**.
- Date arguments (`--start`, `--end`) are `YYYY-MM-DD`, inclusive on both ends.
- The bearer token is cached at `/tmp/ga-token.json` for ~50 minutes. `rm` it to force re-mint.

For the full operational reference, including all flags, edge cases, and triage heuristics, see [`SKILL.md`](./SKILL.md).

## License

MIT.
