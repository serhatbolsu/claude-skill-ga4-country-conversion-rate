#!/usr/bin/env python3
"""Op 1 — country-level install→purchase conversion over a date window.

Counts `first_open` (installs) and a purchase event (default `purchase_v2`,
auto-falls back to Firebase's `in_app_purchase` if the former has 0 events).
Pivots by countryId and writes /tmp/cc-country-<start>-<end>.json.

Usage:
  ga-country.py --start 2026-04-01 --end 2026-05-01
                [--min-installs 50]
                [--purchase-event auto|purchase_v2|in_app_purchase]
                [--property-id 123]  (else uses $GA4_PROPERTY_ID)
                [--project-id <fb-project-id>]
"""
import argparse, datetime, json, os, subprocess, sys, tempfile, pathlib

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GA_FETCH   = SCRIPT_DIR / "ga-fetch.sh"


def run_ga(*args):
    res = subprocess.run([str(GA_FETCH), *args], capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit(res.returncode)
    return res.stdout


def resolve_property(cli):
    if cli:
        return cli
    return run_ga("resolve-property").strip()


def probe_count(prop, event, start, end):
    return int(run_ga("probe-event", prop, event, start, end).strip() or "0")


def resolve_purchase_event(prop, choice, start, end):
    """Returns (event_name, fallback_dict_or_None)."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--min-installs", type=int, default=0,
                    help="suppress rows with fewer installs (default: 0)")
    ap.add_argument("--purchase-event", default="auto",
                    choices=["auto", "purchase_v2", "in_app_purchase"])
    ap.add_argument("--property-id", default=None)
    ap.add_argument("--project-id", default=os.environ.get("FIREBASE_PROJECT_ID"),
                    help="Firebase project id for output JSON header "
                         "(defaults to $FIREBASE_PROJECT_ID; only used for labeling)")
    ap.add_argument("--out", default=None,
                    help="output path (default: /tmp/cc-country-<start>-<end>.json)")
    args = ap.parse_args()

    prop = resolve_property(args.property_id)
    purchase_event, fallback = resolve_purchase_event(prop, args.purchase_event, args.start, args.end)

    body = {
        "dateRanges": [{"startDate": args.start, "endDate": args.end}],
        "dimensions": [{"name": "countryId"}, {"name": "country"}],
        "metrics":    [{"name": "eventCount"}],
        "dimensionFilter": {"filter": {
            "fieldName": "eventName",
            "inListFilter": {"values": ["first_open", purchase_event]},
        }},
        "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}],
        "limit": 250,
    }
    # We need rows per (country, eventName) — include eventName as a dimension
    body["dimensions"].append({"name": "eventName"})

    resp = run_report(prop, body)
    if "error" in resp:
        sys.exit(f"GA4 Data API error: {json.dumps(resp['error'], indent=2)}")

    # Pivot rows by countryId
    pivot = {}
    for row in resp.get("rows", []) or []:
        dvs = row["dimensionValues"]
        country_id = (dvs[0].get("value") or "").lower()
        country    = dvs[1].get("value") or ""
        event_name = dvs[2].get("value") or ""
        count      = int(row["metricValues"][0]["value"] or "0")
        entry = pivot.setdefault(country_id, {
            "country_id": country_id,
            "country":    country,
            "installs":   0,
            "purchases":  0,
        })
        if not entry["country"]:
            entry["country"] = country
        if event_name == "first_open":
            entry["installs"] += count
        elif event_name == purchase_event:
            entry["purchases"] += count

    # Compute rate and apply threshold
    rows = []
    for r in pivot.values():
        if r["installs"] < args.min_installs:
            continue
        r["rate"] = round(r["purchases"] / r["installs"], 6) if r["installs"] else None
        rows.append(r)
    rows.sort(key=lambda r: r["installs"], reverse=True)

    # Sampling / cardinality metadata
    md = resp.get("metadata") or {}
    sampling_blocks = md.get("samplingMetadatas") or []
    is_sampled = bool(sampling_blocks)
    samples_read = sampling_blocks[0].get("samplesReadCount") if sampling_blocks else None
    sampling_space = sampling_blocks[0].get("samplingSpaceSize") if sampling_blocks else None

    out = {
        "property_id":          prop,
        "firebase_project_id":  args.project_id,
        "operation":            "country",
        "date_range":           {"start": args.start, "end": args.end},
        "fetched_at":           datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "purchase_event_used":  purchase_event,
        "fallback":             fallback,
        "sampling":             {"is_sampled": is_sampled, "samples_read": samples_read, "sampling_space": sampling_space},
        "data_loss_from_other_row": bool(md.get("dataLossFromOtherRow")),
        "row_count_total":      len(rows),
        "rows":                 rows,
    }

    out_path = args.out or f"/tmp/cc-country-{args.start}-{args.end}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(out_path)


if __name__ == "__main__":
    main()
