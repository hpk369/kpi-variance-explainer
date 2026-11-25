"""Asserts that the generated data contains the four planted stories, the planted DQ defects and clean keys.

Run after generate.py (and optionally daily_feed.py):  python generator/checks.py
Exit code 1 if anything fails.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import ROOT, constants  # noqa: E402

OUT = ROOT / "generator" / "out"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))


def main() -> int:
    C = constants()
    ST, hurdle = C["stories"], C["raroc"]["hurdle_rate"]
    t = {p.stem: pd.read_parquet(p) for p in OUT.glob("*.parquet")}
    e = json.loads((OUT / "expected_answers.json").read_text())

    # Story 1
    s1 = e["story1_industrials_q3"]
    fees = [m["fee"] for m in ST["industrials_q3"]["mandates"]]
    want = -(sum(fees) + ST["industrials_q3"]["other_products_shortfall"])
    check(abs(s1["variance"] - want) < 1000, f"Story 1: Industrials {s1['quarter']} variance {s1['variance']:,.0f} ≈ {want:,.0f}")
    ma = next(p for p in s1["variance_by_product"] if p["product_id"] == "P02")
    check(abs(ma["variance"] + sum(fees)) < 1000, f"Story 1: M&A variance {ma['variance']:,.0f} = -{sum(fees):,.0f}")
    check(0.80 <= s1["ma_share_of_variance"] <= 0.90, f"Story 1: M&A share {s1['ma_share_of_variance']:.0%} in 80–90%")
    check(sorted(d["expected_fee"] for d in s1["slipped_deals"]) == sorted(fees), "Story 1: exactly the 2 planted mandates are slipped")
    check({c["client_id"] for c in s1["top_clients"][:2]} == {d["client_id"] for d in s1["slipped_deals"]}, "Story 1: mandate clients are the top 2 contributors")

    # Story 2
    s2 = e["story2_whitespace"]
    n2 = ST["whitespace"]["lending_only_mid_market"]
    lo, hi = ST["whitespace"]["raroc_range"]
    check(s2["count"] == n2, f"Story 2: {s2['count']} Mid-Market clients below {hurdle:.0%} hurdle (want {n2})")
    check(s2["clients_below_hurdle_all_segments"] == n2, f"Story 2: no other client below hurdle ({s2['clients_below_hurdle_all_segments']} total)")
    check(all(c["products_held"] == ["Lending"] for c in s2["clients"]), "Story 2: all of them are lending-only")
    check(all(lo - 0.01 <= c["raroc"] <= hi + 0.01 for c in s2["clients"]), f"Story 2: RAROC within {lo:.0%}–{hi:.0%}")
    check(0.15 <= s2["portfolio_raroc"] <= 0.30, f"Portfolio RAROC {s2['portfolio_raroc']:.1%} plausible")

    # Story 3
    teams = {x["team"]: x for x in e["story3_capacity"]["teams"]}
    e2 = teams[ST["capacity"]["team"]]
    check(e2["over_capacity"] and e2["clients_per_banker"] == 14, f"Story 3: {e2['team']} {e2['clients_per_banker']} clients/banker vs {e2['capacity_target']}")
    check(sum(x["over_capacity"] for x in teams.values()) == 1, "Story 3: only one team over capacity")
    wr = e2["win_rate_by_quarter"]
    check(all(abs(wr[q] - v) <= 0.05 for q, v in ST["capacity"]["win_rate_by_quarter"].items()), f"Story 3: win rate {wr} follows the planted trend")

    # Story 4
    br = e["story4_recon"]["breaks"]
    rb = ST["recon_break"]
    check(len(br) == 1 and br[0]["product_id"] == rb["product_id"] and br[0]["month"] == rb["month"][:7]
          and abs(br[0]["delta"] - rb["amount"]) < 1, f"Story 4: exactly one break {br}")

    # Keys and planted DQ defects
    cl, bk, pr = set(t["dim_client"].client_id), set(t["dim_banker"].banker_id), set(t["dim_product"].product_id)
    for name in ["fact_revenue", "fact_plan", "fact_capital", "fact_pipeline", "fact_coverage"]:
        df = t[name]
        check(df.client_id.isin(cl).all(), f"{name}: every client_id exists in dim_client")
        if "product_id" in df:
            check(df.product_id.isin(pr).all(), f"{name}: every product_id exists in dim_product")
    check(t["dim_client"].coverage_banker_id.isin(bk).all(), "dim_client: every coverage banker exists")
    check(t["fact_pipeline"].banker_id.isin(bk).all(), "fact_pipeline: every banker exists")
    dq = e["data_quality"]
    check(int(t["fact_coverage"].banker_id.isna().sum()) == dq["null_banker_id_rows_in_coverage"], f"DQ: {dq['null_banker_id_rows_in_coverage']} null banker_id rows planted in coverage")
    check(int(t["fact_pipeline"].duplicated().sum()) == 3, "DQ: 3 duplicate deal rows planted")
    check((t["fact_pipeline"].expected_fee < 0).sum() == 1, "DQ: 1 negative fee planted")
    check((t["fact_revenue"].revenue > 0).all(), "fact_revenue: all revenue positive")

    # Feed (optional)
    feed = OUT / "feed" / "revenue_daily"
    if feed.exists():
        from generator.daily_feed import Feed

        files = sorted(feed.glob("*.parquet"))
        check(len(files) > 0, f"Feed: {len(files)} day(s) present, last {files[-1].stem}")
        last = date.fromisoformat(files[-1].stem)
        regen = Feed(OUT).day_tables(last)["revenue_daily"].reset_index(drop=True)
        saved = pd.read_parquet(files[-1]).reset_index(drop=True)
        check(regen.revenue.round(2).equals(saved.revenue.round(2)), "Feed: re-generating a day gives identical rows")
        mand = [m for m in ST["industrials_q3"]["mandates"] if date.fromisoformat(m["actual_close"]) <= last]
        if mand:
            ev = pd.concat(pd.read_parquet(p) for p in (OUT / "feed" / "pipeline_events").glob("*.parquet"))
            won = set(ev[ev.event_type == "won"].actual_fee.round())
            check(all(m["fee"] in won for m in mand), f"Feed: {len(mand)} slipped mandate(s) closed in November")

    for ok, label in results:
        print(("PASS  " if ok else "FAIL  ") + label)
    failed = sum(not ok for ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
