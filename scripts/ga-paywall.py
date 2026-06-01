#!/usr/bin/env python3
"""Op 2 — paywall conversion: paywall_shown → purchase_v2 broken down by the
custom event-scoped dimensions `paywall_type` and `paywall_source`.

No purchase_v2 → in_app_purchase fallback here: Firebase's auto in_app_purchase
event does not carry paywall_type / paywall_source, so paywall attribution is
undefined for it.

Usage:
  ga-paywall.py --start 2026-04-01 --end 2026-05-01
                [--min-shown 50]
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


def run_report(prop, body):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        f.flush()
        try:
            return json.loads(run_ga("runreport", prop, f.name))
        finally:
            os.unlink(f.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--min-shown", type=int, default=0,
                    help="suppress rows with fewer paywall_shown (default: 0)")
    ap.add_argument("--property-id", default=None)
    ap.add_argument("--project-id", default=os.environ.get("FIREBASE_PROJECT_ID"),
                    help="Firebase project id for output JSON header "
                         "(defaults to $FIREBASE_PROJECT_ID; only used for labeling)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prop = resolve_property(args.property_id)

    # Refuse to run if purchase_v2 has zero events — fallback doesn't apply here
    v2 = probe_count(prop, "purchase_v2", args.start, args.end)
    if v2 == 0:
        sys.exit(
            "ERROR: purchase_v2 has 0 events in the window. Paywall-conversion\n"
            "analysis requires purchase_v2 because it carries paywall_type /\n"
            "paywall_source params. The in_app_purchase fallback used by Op 1\n"
            "and Op 3 lacks those params and is not applicable here."
        )

    body = {
        "dateRanges": [{"startDate": args.start, "endDate": args.end}],
        "dimensions": [
            {"name": "eventName"},
            {"name": "customEvent:paywall_type"},
            {"name": "customEvent:paywall_source"},
        ],
        "metrics": [{"name": "eventCount"}],
        "dimensionFilter": {"filter": {
            "fieldName":   "eventName",
            "inListFilter": {"values": ["paywall_shown", "purchase_v2"]},
        }},
        "limit": 10000,
    }
    resp = run_report(prop, body)

    # Detect "dimension not registered" error with a clearer hint
    if "error" in resp:
        msg = json.dumps(resp["error"])
        if "customEvent:paywall_type" in msg or "customEvent:paywall_source" in msg \
                or "Field" in msg and "not found" in msg:
            sys.exit(
                "ERROR: GA4 says one of customEvent:paywall_type /\n"
                "customEvent:paywall_source is not registered as an event-scoped\n"
                "custom dimension. Register both in GA4 Admin → Custom Definitions\n"
                "(event-scope), then wait 24-48h for backfill before retrying.\n"
                f"Raw error: {msg}"
            )
        sys.exit(f"GA4 Data API error: {json.dumps(resp['error'], indent=2)}")

    pivot = {}
    for row in resp.get("rows", []) or []:
        dv = row["dimensionValues"]
        event_name = dv[0].get("value") or ""
        ptype      = dv[1].get("value") or "(not set)"
        psource    = dv[2].get("value") or "(not set)"
        count      = int(row["metricValues"][0]["value"] or "0")
        key = (ptype, psource)
        entry = pivot.setdefault(key, {
            "paywall_type":   ptype,
            "paywall_source": psource,
            "shown":          0,
            "purchases":      0,
        })
        if event_name == "paywall_shown":
            entry["shown"] += count
        elif event_name == "purchase_v2":
            entry["purchases"] += count

    rows = []
    for r in pivot.values():
        if r["shown"] < args.min_shown:
            continue
        r["rate"] = round(r["purchases"] / r["shown"], 6) if r["shown"] else None
        rows.append(r)
    rows.sort(key=lambda r: r["shown"], reverse=True)

    md = resp.get("metadata") or {}
    sb = md.get("samplingMetadatas") or []
    out = {
        "property_id":         prop,
        "firebase_project_id": args.project_id,
        "operation":           "paywall",
        "date_range":          {"start": args.start, "end": args.end},
        "fetched_at":          datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "purchase_event_used": "purchase_v2",
        "fallback":            None,
        "sampling": {
            "is_sampled":     bool(sb),
            "samples_read":   sb[0].get("samplesReadCount") if sb else None,
            "sampling_space": sb[0].get("samplingSpaceSize") if sb else None,
        },
        "data_loss_from_other_row": bool(md.get("dataLossFromOtherRow")),
        "row_count_total":     len(rows),
        "rows":                rows,
    }

    out_path = args.out or f"/tmp/cc-paywall-{args.start}-{args.end}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(out_path)


if __name__ == "__main__":
    main()
