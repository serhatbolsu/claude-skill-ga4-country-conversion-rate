#!/usr/bin/env python3
"""Op 3 — Day-0 purchases by country (purchase on the same date as first_open).

Two GA4 runReport requests + post-join (numerator and denominator are different
events). Same purchase-event fallback as Op 1.

Usage:
  ga-day0.py --start 2026-04-01 --end 2026-05-01
             [--purchase-event auto|purchase_v2|in_app_purchase]
             [--min-users 50]
             [--property-id 123]
             [--project-id <fb-project-id>]
"""
import argparse, datetime, json, os, subprocess, sys, tempfile, pathlib
from collections import defaultdict

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GA_FETCH   = SCRIPT_DIR / "ga-fetch.sh"


def run_ga(*args):
    res = subprocess.run([str(GA_FETCH), *args], capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit(res.returncode)
    return res.stdout


def resolve_property(cli):
    return cli or run_ga("resolve-property").strip()


def probe_count(prop, event, start, end):
    return int(run_ga("probe-event", prop, event, start, end).strip() or "0")


def resolve_purchase_event(prop, choice, start, end):
    if choice in ("purchase_v2", "in_app_purchase"):
        return choice, None
    if choice != "auto":
        sys.exit(f"invalid --purchase-event: {choice}")
    v2 = probe_count(prop, "purchase_v2", start, end)
    if v2 > 0:
        return "purchase_v2", None
    return ("in_app_purchase", {
        "from":   "purchase_v2",
        "to":     "in_app_purchase",
        "reason": "probe returned 0 events for purchase_v2 in the window",
    })


def run_report(prop, body):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        f.flush()
        try:
            return json.loads(run_ga("runreport", prop, f.name))
        finally:
            os.unlink(f.name)


def extract_metadata(resp):
    md = resp.get("metadata") or {}
    sb = md.get("samplingMetadatas") or []
    return {
        "is_sampled":     bool(sb),
        "samples_read":   (sb[0].get("samplesReadCount") if sb else None),
        "sampling_space": (sb[0].get("samplingSpaceSize") if sb else None),
        "data_loss_from_other_row": bool(md.get("dataLossFromOtherRow")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--purchase-event", default="auto",
                    choices=["auto", "purchase_v2", "in_app_purchase"])
    ap.add_argument("--min-users", type=int, default=0,
                    help="suppress rows with fewer day-0 users (default: 0)")
    ap.add_argument("--property-id", default=None)
    ap.add_argument("--project-id", default=os.environ.get("FIREBASE_PROJECT_ID"),
                    help="Firebase project id for output JSON header "
                         "(defaults to $FIREBASE_PROJECT_ID; only used for labeling)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prop = resolve_property(args.property_id)
    purchase_event, fallback = resolve_purchase_event(prop, args.purchase_event, args.start, args.end)

    # Request A — purchases with date + firstSessionDate, filter to same-day in Python
    body_a = {
        "dateRanges": [{"startDate": args.start, "endDate": args.end}],
        "dimensions": [
            {"name": "countryId"},
            {"name": "date"},
            {"name": "firstSessionDate"},
        ],
        "metrics": [{"name": "eventCount"}],
        "dimensionFilter": {"filter": {
            "fieldName":    "eventName",
            "stringFilter": {"value": purchase_event},
        }},
        "limit": 100000,
    }
    resp_a = run_report(prop, body_a)
    if "error" in resp_a:
        sys.exit(f"GA4 Data API error (purchases): {json.dumps(resp_a['error'], indent=2)}")

    day0_purchases = defaultdict(int)
    for row in resp_a.get("rows", []) or []:
        dv = row["dimensionValues"]
        country_id = (dv[0].get("value") or "").lower()
        date       = dv[1].get("value") or ""
        first_sess = dv[2].get("value") or ""
        if date and first_sess and date == first_sess:
            day0_purchases[country_id] += int(row["metricValues"][0]["value"] or "0")

    # Request B — total users (= installs) per (country, date) for the day-0 pool
    body_b = {
        "dateRanges": [{"startDate": args.start, "endDate": args.end}],
        "dimensions": [
            {"name": "countryId"},
            {"name": "date"},
        ],
        "metrics": [{"name": "totalUsers"}],
        "dimensionFilter": {"filter": {
            "fieldName":    "eventName",
            "stringFilter": {"value": "first_open"},
        }},
        "limit": 100000,
    }
    resp_b = run_report(prop, body_b)
    if "error" in resp_b:
        sys.exit(f"GA4 Data API error (installs): {json.dumps(resp_b['error'], indent=2)}")

    day0_users = defaultdict(int)
    for row in resp_b.get("rows", []) or []:
        dv = row["dimensionValues"]
        country_id = (dv[0].get("value") or "").lower()
        day0_users[country_id] += int(row["metricValues"][0]["value"] or "0")

    # Join
    countries = set(day0_users) | set(day0_purchases)
    rows = []
    for cid in countries:
        u = day0_users.get(cid, 0)
        p = day0_purchases.get(cid, 0)
        if u < args.min_users and p < args.min_users:
            continue
        rows.append({
            "country_id":      cid,
            "day0_users":      u,
            "day0_purchases":  p,
            "day0_rate":       round(p / u, 6) if u else None,
        })
    rows.sort(key=lambda r: r["day0_users"], reverse=True)

    md_a = extract_metadata(resp_a)
    md_b = extract_metadata(resp_b)
    out = {
        "property_id":         prop,
        "firebase_project_id": args.project_id,
        "operation":           "day0",
        "date_range":          {"start": args.start, "end": args.end},
        "fetched_at":          datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "purchase_event_used": purchase_event,
        "fallback":            fallback,
        "sampling": {
            "is_sampled":     md_a["is_sampled"] or md_b["is_sampled"],
            "samples_read":   md_a["samples_read"] or md_b["samples_read"],
            "sampling_space": md_a["sampling_space"] or md_b["sampling_space"],
        },
        "data_loss_from_other_row": md_a["data_loss_from_other_row"] or md_b["data_loss_from_other_row"],
        "row_count_total":     len(rows),
        "rows":                rows,
    }

    out_path = args.out or f"/tmp/cc-day0-{args.start}-{args.end}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(out_path)


if __name__ == "__main__":
    main()
