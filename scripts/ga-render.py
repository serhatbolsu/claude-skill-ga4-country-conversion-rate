#!/usr/bin/env python3
"""Human render for the JSON outputs of ga-country.py / ga-day0.py / ga-paywall.py.

Usage:
  ga-render.py country  /tmp/cc-country-<...>.json
  ga-render.py day0     /tmp/cc-day0-<...>.json
  ga-render.py paywall  /tmp/cc-paywall-<...>.json
"""
import json, sys


def pct(r):
    return "—" if r is None else f"{r*100:.2f}%"


def banner(doc):
    out = []
    fb = doc.get("fallback")
    if fb:
        out.append(f"WARNING: {fb['from']} had 0 events in window — fell back to {fb['to']} (Firebase auto event).")
        out.append(f"   Reason: {fb['reason']}")
    samp = doc.get("sampling") or {}
    if samp.get("is_sampled"):
        sr = samp.get("samples_read")
        ss = samp.get("sampling_space")
        out.append(f"WARNING: report sampled — {sr} of {ss} events read.")
    if doc.get("data_loss_from_other_row"):
        out.append("WARNING: dataLossFromOtherRow = true — high-cardinality rows collapsed into (other). Filter narrower or break by fewer dims.")
    return "\n".join(out)


def header(doc):
    dr = doc["date_range"]
    return (
        f"property: {doc['property_id']}   project: {doc['firebase_project_id']}\n"
        f"window:   {dr['start']} → {dr['end']}   op: {doc['operation']}   "
        f"purchase_event: {doc['purchase_event_used']}\n"
        f"fetched:  {doc['fetched_at']}   rows: {doc.get('row_count_total', len(doc.get('rows', [])))}"
    )


def render_country(doc):
    print(header(doc))
    b = banner(doc)
    if b: print(b)
    print()
    print(f"{'country_id':<10}  {'country':<28}  {'installs':>10}  {'purchases':>10}  {'rate':>8}")
    print("-" * 72)
    for r in doc["rows"]:
        print(f"{r['country_id']:<10}  {r['country'][:28]:<28}  {r['installs']:>10}  {r['purchases']:>10}  {pct(r['rate']):>8}")


def render_day0(doc):
    print(header(doc))
    b = banner(doc)
    if b: print(b)
    print()
    print(f"{'country_id':<10}  {'day0_users':>12}  {'day0_purch':>12}  {'day0_rate':>10}")
    print("-" * 50)
    for r in doc["rows"]:
        print(f"{r['country_id']:<10}  {r['day0_users']:>12}  {r['day0_purchases']:>12}  {pct(r['day0_rate']):>10}")


def render_paywall(doc):
    print(header(doc))
    b = banner(doc)
    if b: print(b)
    print()
    print(f"{'paywall_type':<22}  {'paywall_source':<22}  {'shown':>8}  {'purch':>8}  {'rate':>8}")
    print("-" * 76)
    for r in doc["rows"]:
        print(f"{r['paywall_type'][:22]:<22}  {r['paywall_source'][:22]:<22}  {r['shown']:>8}  {r['purchases']:>8}  {pct(r['rate']):>8}")


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: ga-render.py {country|day0|paywall} <path>")
    op, path = sys.argv[1], sys.argv[2]
    with open(path) as f:
        doc = json.load(f)
    if op == "country": render_country(doc)
    elif op == "day0":  render_day0(doc)
    elif op == "paywall": render_paywall(doc)
    else: sys.exit(f"unknown op: {op}")


if __name__ == "__main__":
    main()
