"""Synthetic banking data for Northlake Bank (fictional), with four planted stories.

Run:   python generator/generate.py            → writes generator/out/*.parquet + expected_answers.json
Check: python generator/checks.py

Monthly history runs Oct 2024 → Sep 2026 (the stories live in Q3 2026). Plan figures run to Dec 2027.
From Oct 2026 the daily feed (generator/daily_feed.py) adds one synthetic day at a time.

Stories (numbers in config/constants.yaml):
  1. Industrials misses plan in Q3 2026: two M&A mandates slip from Aug to Nov 2026, rest of Industrials is a bit short.
  2. 18 lending-only Mid-Market clients sit below the RAROC hurdle (cross-sell whitespace).
  3. Coverage East 2 is over capacity (14 clients per banker vs 10) and its win rate falls 45% → 25% over 4 quarters.
  4. The GL control total for Transaction Banking, Jun 2026 is $1.2M above booked revenue (reconciliation break).
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import ROOT, constants  # noqa: E402
from generator import names  # noqa: E402

OUT = ROOT / "generator" / "out"

PRODUCTS = pd.DataFrame([
    ("P01", "Lending", "Recurring", "High", "Committed and term facilities; revenue is net interest income plus arrangement fees"),
    ("P02", "M&A Advisory", "Event", "Low", "Sell-side and buy-side advisory; success fees at close"),
    ("P03", "Debt Capital Markets", "Event", "Low", "Bond and loan syndication; underwriting fees at pricing"),
    ("P04", "Equity Capital Markets", "Event", "Low", "IPOs and follow-ons; underwriting fees at pricing"),
    ("P05", "Rates & FX Hedging", "Recurring", "Medium", "Interest-rate and currency hedging; trading fees"),
    ("P06", "Transaction Banking", "Recurring", "Low", "Cash management and trade finance; fees and deposit income"),
], columns=["product_id", "product_name", "revenue_type", "capital_intensity", "description"])
PRODUCT_NAME = dict(zip(PRODUCTS.product_id, PRODUCTS.product_name))
RECURRING = ["P01", "P05", "P06"]
EVENT = ["P02", "P03", "P04"]

TEAMS = {  # team: (capacity target, regions)
    "Coverage East 1": (8, ["Canada East"]),
    "Coverage East 2": (10, ["Canada East"]),
    "Coverage West": (8, ["Canada West"]),
    "Coverage US": (8, ["US Northeast", "US Midwest"]),
    "Coverage FI & Sponsors": (12, ["Canada East", "Canada West", "US Northeast"]),
}
ROLES = ["Managing Director", "Director", "Vice President", "Associate", "Analyst"]
SEGMENTS = {"Large Corporate": 50, "Mid-Market": 90, "Financial Institutions": 35, "Sponsor": 25}
CORP_INDUSTRIES = (["Industrials", "Energy", "Technology", "Healthcare", "Consumer", "Real Estate"],
                   [0.24, 0.16, 0.16, 0.13, 0.17, 0.14])
STAGES = ["Origination", "Pitch", "Mandate", "Execution"]
STAGE_PROB = {"Origination": 0.10, "Pitch": 0.25, "Mandate": 0.60, "Execution": 0.85}

# Monthly recurring revenue base (CAD) by segment and product: (low, high)
RECURRING_BASE = {
    "Large Corporate": {"P01": (250e3, 600e3), "P05": (60e3, 180e3), "P06": (60e3, 160e3)},
    "Mid-Market": {"P01": (40e3, 120e3), "P05": (10e3, 35e3), "P06": (10e3, 40e3)},
    "Financial Institutions": {"P01": (80e3, 200e3), "P05": (80e3, 220e3), "P06": (70e3, 180e3)},
    "Sponsor": {"P01": (100e3, 250e3), "P05": (20e3, 60e3), "P06": (10e3, 30e3)},
}
HOLD_PROB = {  # probability a client holds each recurring product
    "Large Corporate": {"P01": 1.0, "P05": 0.70, "P06": 0.85},
    "Mid-Market": {"P01": 0.95, "P05": 0.35, "P06": 0.70},
    "Financial Institutions": {"P01": 0.40, "P05": 0.90, "P06": 0.90},
    "Sponsor": {"P01": 0.90, "P05": 0.40, "P06": 0.30},
}
DEAL_RATE = {"Large Corporate": 0.16, "Mid-Market": 0.05, "Financial Institutions": 0.10, "Sponsor": 0.19}  # deals / client / month
DEAL_MIX = {
    "Large Corporate": {"P02": 0.30, "P03": 0.30, "P04": 0.20, "P01": 0.20},
    "Mid-Market": {"P01": 0.50, "P02": 0.30, "P03": 0.20},
    "Financial Institutions": {"P03": 0.60, "P04": 0.20, "P01": 0.20},
    "Sponsor": {"P02": 0.50, "P03": 0.30, "P01": 0.20},
}
DEAL_FEE = {  # (low, high) CAD
    "P02": {"Large Corporate": (1.5e6, 6e6), "Mid-Market": (0.5e6, 2e6), "Sponsor": (1.5e6, 5e6), "Financial Institutions": (1e6, 3e6)},
    "P03": (0.3e6, 1.5e6), "P04": (0.8e6, 3e6), "P01": (0.1e6, 0.6e6),
}
TEAM_WIN_RATE = {"Coverage East 1": 0.50, "Coverage East 2": 0.47, "Coverage West": 0.47, "Coverage US": 0.46, "Coverage FI & Sponsors": 0.49}
COST_RATIO = {"Large Corporate": 0.45, "Mid-Market": 0.50, "Financial Institutions": 0.42, "Sponsor": 0.40}
TARGET_RAROC = {"Large Corporate": (0.16, 0.26), "Mid-Market": (0.16, 0.26), "Financial Institutions": (0.18, 0.30), "Sponsor": (0.17, 0.28)}


def d(s: str) -> date:
    return date.fromisoformat(s)


def quarter_label(ts) -> str:
    ts = pd.Timestamp(ts)
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def month_start(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).to_period("M").to_timestamp()


def seasonality(month: int) -> float:
    return {12: 0.97, 1: 0.98, 3: 1.02, 6: 1.02, 9: 1.02}.get(month, 1.0)


def el_rate(rating: int) -> float:
    """Expected loss as a share of lending revenue, by internal rating (1 best, 10 worst)."""
    return 0.01 + 0.012 * rating


def build(C: dict | None = None) -> dict:
    C = C or constants()
    rng = np.random.default_rng(C["seed"])
    D = C["dates"]
    ST = C["stories"]
    h_start, h_end, plan_end = pd.Timestamp(D["history_start"]), pd.Timestamp(D["history_end"]), pd.Timestamp(D["plan_end"])
    hist_months = pd.date_range(h_start, h_end, freq="MS")
    plan_months = pd.date_range(h_start, plan_end, freq="MS")
    ttm_months = hist_months[-12:]
    q3 = ST["industrials_q3"]["quarter"]

    # ---------------- Bankers ----------------
    bankers = []
    n = 1
    for team, (target, _) in TEAMS.items():
        for role in ROLES:
            bankers.append({"banker_id": f"B{n:02d}", "banker_name": f"{names.FIRST[n - 1]} {names.LAST[n - 1]}",
                            "team": team, "role": role, "capacity_target": target})
            n += 1
    dim_banker = pd.DataFrame(bankers)

    # ---------------- Clients ----------------
    rows = []
    stems = list(names.CLIENT_STEMS)
    rng.shuffle(stems)
    used = set()
    k = 0
    for seg, count in SEGMENTS.items():
        for i in range(count):
            if seg == "Financial Institutions":
                industry = "Financials"
            elif seg == "Sponsor":
                industry = "Financials"
            elif seg == "Large Corporate" and i < 4:
                industry = "Industrials"  # guarantees the planted mandate clients exist
            else:
                industry = rng.choice(CORP_INDUSTRIES[0], p=CORP_INDUSTRIES[1])
            suffix_key = "Sponsor" if seg == "Sponsor" else industry
            while True:
                name = f"{stems[k % len(stems)]} {rng.choice(names.INDUSTRY_SUFFIX[suffix_key])}"
                k += 1
                if name not in used:
                    used.add(name)
                    break
            rating = int({"Large Corporate": rng.integers(2, 6), "Mid-Market": rng.integers(4, 9),
                          "Financial Institutions": rng.integers(2, 6), "Sponsor": rng.integers(4, 8)}[seg])
            rows.append({"client_id": f"C-{1001 + len(rows)}", "client_name": name + " (fictional)", "segment": seg,
                         "industry": industry, "internal_rating": rating})
    dim_client = pd.DataFrame(rows)

    # Story 2: 18 lending-only Mid-Market clients (not Financials)
    mm = dim_client[(dim_client.segment == "Mid-Market")].index.to_numpy()
    story2_idx = rng.choice(mm, ST["whitespace"]["lending_only_mid_market"], replace=False)
    dim_client["story_lending_only"] = False
    dim_client.loc[story2_idx, "story_lending_only"] = True
    dim_client.loc[story2_idx, "internal_rating"] = rng.integers(5, 9, len(story2_idx))

    # Teams: FI + Sponsors → FI & Sponsors; LC + MM split 70 / 25 / 25 / 20
    dim_client["team"] = ""
    dim_client.loc[dim_client.segment.isin(["Financial Institutions", "Sponsor"]), "team"] = "Coverage FI & Sponsors"
    corp = dim_client[dim_client.team == ""].index.to_numpy().copy()
    planted_clients = dim_client[(dim_client.segment == "Large Corporate") & (dim_client.industry == "Industrials")].index[:2]
    corp = np.array([c for c in corp if c not in planted_clients])
    rng.shuffle(corp)
    e2_n = ST["capacity"]["clients"]
    split = {"Coverage East 2": e2_n, "Coverage East 1": 25, "Coverage West": 25, "Coverage US": 20}
    pos = 0
    for team, cnt in split.items():
        take = corp[pos:pos + cnt] if team != "Coverage East 1" else corp[pos:pos + cnt - len(planted_clients)]
        dim_client.loc[take, "team"] = team
        pos += len(take)
    dim_client.loc[planted_clients, "team"] = "Coverage East 1"
    assert (dim_client.team != "").all()
    region_choice = {t: TEAMS[t][1] for t in TEAMS}
    dim_client["region"] = [rng.choice(region_choice[t]) for t in dim_client.team]
    # coverage banker: round robin within team
    dim_client["coverage_banker_id"] = ""
    for team, grp in dim_client.groupby("team"):
        ids = dim_banker[dim_banker.team == team].banker_id.tolist()
        dim_client.loc[grp.index, "coverage_banker_id"] = [ids[i % len(ids)] for i in range(len(grp))]
    dim_client["relationship_start"] = [pd.Timestamp("2015-01-01") + pd.Timedelta(days=int(x)) for x in rng.integers(0, 3200, len(dim_client))]

    seg_of = dict(zip(dim_client.client_id, dim_client.segment))
    ind_of = dict(zip(dim_client.client_id, dim_client.industry))
    team_of = dict(zip(dim_client.client_id, dim_client.team))
    banker_of = dict(zip(dim_client.client_id, dim_client.coverage_banker_id))
    rating_of = dict(zip(dim_client.client_id, dim_client.internal_rating))
    story2_ids = set(dim_client.loc[story2_idx, "client_id"])
    planted_ids = list(dim_client.loc[planted_clients, "client_id"])

    # ---------------- Recurring products: plan and actual ----------------
    rec_rows = []
    for c in dim_client.itertuples():
        held = []
        for p in RECURRING:
            if c.client_id in story2_ids:
                if p == "P01":
                    held.append(p)
            elif rng.random() < HOLD_PROB[c.segment][p]:
                held.append(p)
        if not held:
            held = ["P06"]
        for p in held:
            lo, hi = RECURRING_BASE[c.segment][p]
            if c.client_id in story2_ids:
                lo, hi = 50e3, 110e3
            base = rng.uniform(lo, hi)
            growth = rng.uniform(0.02, 0.06)
            for i, m in enumerate(plan_months):
                rec_rows.append((c.client_id, p, m, base * (1 + growth) ** (i / 12) * seasonality(m.month)))
    rec_plan = pd.DataFrame(rec_rows, columns=["client_id", "product_id", "month_start", "plan_revenue"])
    rec_hist = rec_plan[rec_plan.month_start <= h_end].copy()
    rec_hist["revenue"] = rec_hist.plan_revenue * rng.normal(1.0, 0.03, len(rec_hist))

    # ---------------- Pipeline ----------------
    deals = []
    exp_months = pd.date_range(h_start, pd.Timestamp("2027-03-01"), freq="MS")
    for c in dim_client.itertuples():
        if c.client_id in story2_ids:
            continue
        mix = DEAL_MIX[c.segment]
        for m in exp_months:
            for _ in range(rng.poisson(DEAL_RATE[c.segment])):
                p = rng.choice(list(mix), p=list(mix.values()))
                fee_rng = DEAL_FEE[p][c.segment] if p == "P02" else DEAL_FEE[p]
                exp_close = m + pd.Timedelta(days=int(rng.integers(0, 28)))
                open_d = exp_close - pd.Timedelta(days=int(rng.integers(90, 271)))
                if open_d > h_end:
                    continue  # created later by the daily feed
                deals.append({"client_id": c.client_id, "product_id": p, "expected_fee": round(rng.uniform(*fee_rng), -3),
                              "open_date": open_d, "expected_close_date": exp_close, "planted": False})
    for (cfg, cid) in zip(ST["industrials_q3"]["mandates"], planted_ids):
        exp_close = pd.Timestamp(cfg["expected_close"])
        deals.append({"client_id": cid, "product_id": "P02", "expected_fee": float(cfg["fee"]),
                      "open_date": exp_close - pd.Timedelta(days=200), "expected_close_date": exp_close, "planted": True,
                      "planted_close": pd.Timestamp(cfg["actual_close"])})
    pl = pd.DataFrame(deals).sort_values(["expected_close_date", "client_id"]).reset_index(drop=True)
    pl["deal_id"] = [f"D-{20000 + i}" for i in range(len(pl))]
    pl["banker_id"] = pl.client_id.map(banker_of)
    pl["team"] = pl.client_id.map(team_of)
    pl["industry"] = pl.client_id.map(ind_of)
    pl["segment"] = pl.client_id.map(seg_of)
    dur = (pl.expected_close_date - pl.open_date)
    pl["pitch_date"] = pl.open_date + dur * 0.25
    pl["mandate_date"] = pl.open_date + dur * 0.50
    pl["execution_date"] = pl.open_date + dur * 0.75
    for col in ["pitch_date", "mandate_date", "execution_date"]:
        pl[col] = pl[col].dt.normalize()

    # Industrials M&A deals around Q3 2026 never slip and close at the expected fee (keeps story 1 exact)
    guard = (pl.industry == "Industrials") & (pl.product_id == "P02") & \
            (pl.expected_close_date >= pd.Timestamp("2026-06-01")) & (pl.expected_close_date <= h_end)
    e2_story_q = (pl.team == ST["capacity"]["team"]) & (pl.expected_close_date >= pd.Timestamp("2025-07-01"))

    # Outcome per deal. Win counts are exact per (team, quarter of expected close).
    pl["fate_status"] = "Lost"
    pl["close_quarter"] = pl.expected_close_date.map(quarter_label)
    e2_rates = ST["capacity"]["win_rate_by_quarter"]
    for (team, qtr), grp in pl[~pl.planted].groupby(["team", "close_quarter"]):
        if team == ST["capacity"]["team"] and qtr in e2_rates:
            rate = e2_rates[qtr]
        elif team == ST["capacity"]["team"] and qtr > max(e2_rates):
            rate = 0.27
        else:
            rate = TEAM_WIN_RATE[team] + rng.uniform(-0.03, 0.03)
        wins = int(round(rate * len(grp)))
        win_idx = rng.choice(grp.index.to_numpy(), wins, replace=False)
        pl.loc[win_idx, "fate_status"] = "Won"
    pl.loc[pl.planted, "fate_status"] = "Won"

    # Close dates and fees
    fate_date, actual_fee = [], []
    for r in pl.itertuples():
        if r.planted:
            fate_date.append(r.planted_close)
            actual_fee.append(r.expected_fee)
            continue
        if r.fate_status == "Won":
            slip = (not guard[r.Index]) and (not e2_story_q[r.Index]) and rng.random() < 0.08
            fd = r.expected_close_date + pd.Timedelta(days=int(rng.integers(25, 51))) if slip else r.expected_close_date
            fee = r.expected_fee if guard[r.Index] else round(r.expected_fee * rng.normal(1.0, 0.05), -3)
        else:
            q_start = pd.Period(r.expected_close_date, "Q").start_time
            fd = max(r.expected_close_date - pd.Timedelta(days=int(rng.integers(0, 21))), q_start, r.open_date + pd.Timedelta(days=10))
            fee = 0.0
        fate_date.append(fd)
        actual_fee.append(fee)
    pl["fate_date"] = fate_date
    pl["fate_fee"] = actual_fee
    # stage at which lost deals were lost
    lost_stage = rng.choice(["Pitch", "Mandate", "Execution"], len(pl), p=[0.45, 0.35, 0.20])
    pl["lost_at_stage"] = np.where(pl.fate_status == "Lost", lost_stage, None)

    # Snapshot as of history end
    closed = pl.fate_date <= h_end
    pl["status"] = np.where(closed, pl.fate_status, "Open")
    pl["actual_close_date"] = pl.fate_date.where(closed)
    pl["actual_fee"] = np.where(closed & (pl.fate_status == "Won"), pl.fate_fee, np.nan)

    def stage_asof(r, asof):
        if r.status != "Open":
            return "Closed Won" if r.status == "Won" else "Closed Lost"
        stage = "Origination"
        for s, col in [("Pitch", "pitch_date"), ("Mandate", "mandate_date"), ("Execution", "execution_date")]:
            if getattr(r, col) <= asof:
                stage = s
        return stage
    pl["stage"] = [stage_asof(r, h_end) for r in pl.itertuples()]
    pl["probability"] = [STAGE_PROB.get(s, 1.0 if s == "Closed Won" else 0.0) for s in pl.stage]
    # Stage dates only up to what had happened by history end (and up to the stage a lost deal reached)
    order = {"Origination": 0, "Pitch": 1, "Mandate": 2, "Execution": 3}
    for s, col in [("Pitch", "pitch_date"), ("Mandate", "mandate_date"), ("Execution", "execution_date")]:
        reached_by_loss = pl.lost_at_stage.isna() | (pl.lost_at_stage.map(order).fillna(9) >= order[s])
        happened = pl[col] <= h_end
        pl[col + "_snap"] = pl[col].where(happened & reached_by_loss & ~((pl.status == "Lost") & (pl[col] > pl.actual_close_date)))
    pl["deal_name"] = [f"{PRODUCT_NAME[p]} · {cid}" for p, cid in zip(pl.product_id, pl.client_id)]

    fact_pipeline = pd.DataFrame({
        "deal_id": pl.deal_id, "deal_name": pl.deal_name, "client_id": pl.client_id, "product_id": pl.product_id,
        "banker_id": pl.banker_id, "stage": pl.stage, "status": pl.status, "probability": pl.probability,
        "expected_fee": pl.expected_fee, "actual_fee": pl.actual_fee,
        "open_date": pl.open_date.dt.date, "pitch_date": pl.pitch_date_snap.dt.date, "mandate_date": pl.mandate_date_snap.dt.date,
        "execution_date": pl.execution_date_snap.dt.date, "expected_close_date": pl.expected_close_date.dt.date,
        "actual_close_date": pl.actual_close_date.dt.date,
    })

    # ---------------- Revenue (actual + plan) ----------------
    won_hist = pl[(pl.status == "Won")].copy()
    won_hist["month_start"] = won_hist.actual_close_date.map(month_start)
    fees_actual = won_hist.groupby(["client_id", "product_id", "month_start"], as_index=False).actual_fee.sum().rename(columns={"actual_fee": "deal_fees"})
    # deal-based plan for history months: every deal that is (eventually) won, at its expected close month
    plan_deals = pl[(pl.fate_status == "Won") & (pl.expected_close_date <= h_end)].copy()
    plan_deals["month_start"] = plan_deals.expected_close_date.map(month_start)
    fees_plan_hist = plan_deals.groupby(["client_id", "product_id", "month_start"], as_index=False).expected_fee.sum().rename(columns={"expected_fee": "plan_revenue"})
    # run-rate plan for future months: trailing-12-month deal fees x 1.05, spread evenly
    ttm_fees = fees_actual[fees_actual.month_start.isin(ttm_months)].groupby(["client_id", "product_id"], as_index=False).deal_fees.sum()
    fut_months = plan_months[plan_months > h_end]
    fees_plan_fut = ttm_fees.merge(pd.DataFrame({"month_start": fut_months}), how="cross")
    fees_plan_fut["plan_revenue"] = fees_plan_fut.deal_fees / 12 * 1.05
    fees_plan_fut = fees_plan_fut[["client_id", "product_id", "month_start", "plan_revenue"]]

    fact_plan = pd.concat([rec_plan, fees_plan_hist, fees_plan_fut]).groupby(["client_id", "product_id", "month_start"], as_index=False).plan_revenue.sum()
    fact_plan["plan_revenue"] = fact_plan.plan_revenue.round(2)

    rev = rec_hist[["client_id", "product_id", "month_start", "revenue"]].merge(fees_actual, how="outer", on=["client_id", "product_id", "month_start"]).fillna(0.0)
    rev["recurring"] = rev.revenue
    rev["revenue"] = rev.recurring + rev.deal_fees

    # Story 1 calibration: non-M&A Industrials Q3 variance = -shortfall exactly (applied to Lending NII)
    rev["industry"] = rev.client_id.map(ind_of)
    rev["quarter"] = rev.month_start.map(quarter_label)
    plan_q = fact_plan.assign(industry=fact_plan.client_id.map(ind_of), quarter=fact_plan.month_start.map(quarter_label))
    sel_a = (rev.industry == "Industrials") & (rev.quarter == q3) & (rev.product_id != "P02")
    sel_p = (plan_q.industry == "Industrials") & (plan_q.quarter == q3) & (plan_q.product_id != "P02")
    v0 = rev.loc[sel_a, "revenue"].sum() - plan_q.loc[sel_p, "plan_revenue"].sum()
    delta = -ST["industrials_q3"]["other_products_shortfall"] - v0
    lend = sel_a & (rev.product_id == "P01")
    rev.loc[lend, "recurring"] += delta * rev.loc[lend, "recurring"] / rev.loc[lend, "recurring"].sum()
    rev["revenue"] = rev.recurring + rev.deal_fees

    nii_share = rev.product_id.map({"P01": 0.90, "P05": 0.0, "P06": 0.45}).fillna(0.0)
    rev["nii"] = (rev.recurring * nii_share).round(2)
    rev["fees"] = (rev.revenue - rev.nii).round(2)
    rev["revenue"] = (rev.fees + rev.nii).round(2)
    fact_revenue = rev[rev.revenue != 0][["client_id", "product_id", "month_start", "revenue", "fees", "nii"]].sort_values(["month_start", "client_id", "product_id"]).reset_index(drop=True)

    # ---------------- Capital (RAROC inputs) ----------------
    plan_tot = fact_plan.groupby(["client_id", "month_start"]).plan_revenue.sum()
    plan_lend = fact_plan[fact_plan.product_id == "P01"].groupby(["client_id", "month_start"]).plan_revenue.sum()
    cap = pd.DataFrame(index=pd.MultiIndex.from_product([dim_client.client_id, plan_months], names=["client_id", "month_start"])).reset_index()
    cap["segment"] = cap.client_id.map(seg_of)
    cap["cost_allocation"] = (cap.set_index(["client_id", "month_start"]).index.map(plan_tot).fillna(0).to_numpy()
                              * cap.segment.map(COST_RATIO).to_numpy())
    cap["expected_loss"] = (cap.set_index(["client_id", "month_start"]).index.map(plan_lend).fillna(0).to_numpy()
                            * cap.client_id.map(rating_of).map(el_rate).to_numpy())
    act_tot = fact_revenue.groupby(["client_id", "month_start"]).revenue.sum()
    cap["revenue"] = cap.set_index(["client_id", "month_start"]).index.map(act_tot).fillna(0).to_numpy()
    tax = C["raroc"]["tax_rate"]
    cap["rar"] = (cap.revenue - cap.cost_allocation - cap.expected_loss) * (1 - tax)
    ttm_rar = cap[cap.month_start.isin(ttm_months)].groupby("client_id").rar.sum()
    lo_s, hi_s = ST["whitespace"]["raroc_range"]
    target = {c: (rng.uniform(lo_s, hi_s) if c in story2_ids else rng.uniform(*TARGET_RAROC[seg_of[c]])) for c in dim_client.client_id}
    k_level = {c: (ttm_rar[c] if ttm_rar[c] > 0 else 1e4) / target[c] for c in dim_client.client_id}
    assert (ttm_rar[[c for c in dim_client.client_id if c not in story2_ids]] > 0).all(), "a non-story client has negative risk-adjusted return"
    cap["allocated_capital"] = cap.client_id.map(k_level) * rng.normal(1.0, 0.01, len(cap))
    for col in ["cost_allocation", "expected_loss", "allocated_capital"]:
        cap[col] = cap[col].round(2)
    fact_capital = cap[cap.month_start <= h_end][["client_id", "month_start", "allocated_capital", "expected_loss", "cost_allocation"]].reset_index(drop=True)
    capital_future = cap[cap.month_start > h_end][["client_id", "month_start", "allocated_capital", "expected_loss", "cost_allocation"]].reset_index(drop=True)

    # ---------------- Coverage activity ----------------
    cov = []
    lam = {"Large Corporate": 2.0, "Mid-Market": 1.0, "Financial Institutions": 1.5, "Sponsor": 1.5}
    for c in dim_client.itertuples():
        for m in hist_months:
            busy = 0.6 if (c.team == ST["capacity"]["team"] and m >= pd.Timestamp("2025-10-01")) else 1.0
            meet, calls = rng.poisson(lam[c.segment] * busy), rng.poisson(2 * lam[c.segment] * busy)
            if meet + calls:
                cov.append((c.coverage_banker_id, c.client_id, m, int(meet), int(calls)))
    fact_coverage = pd.DataFrame(cov, columns=["banker_id", "client_id", "month_start", "meetings", "calls"])

    # ---------------- GL control totals (story 4) ----------------
    gl = fact_revenue.groupby(["product_id", "month_start"], as_index=False).revenue.sum().rename(columns={"revenue": "control_total"})
    rb = ST["recon_break"]
    brk = (gl.product_id == rb["product_id"]) & (gl.month_start == pd.Timestamp(rb["month"]))
    gl.loc[brk, "control_total"] += rb["amount"]
    gl["control_total"] = gl.control_total.round(2)
    gl["source_system"] = "GL"
    gl_control = gl

    # ---------------- Months ----------------
    dim_month = pd.DataFrame({"month_start": plan_months})
    dim_month["month_end"] = dim_month.month_start + pd.offsets.MonthEnd(0)
    dim_month["year"] = dim_month.month_start.dt.year
    dim_month["month"] = dim_month.month_start.dt.month
    dim_month["month_label"] = dim_month.month_start.dt.strftime("%b %Y")
    dim_month["quarter"] = dim_month.month_start.map(quarter_label)
    dim_month["fiscal_year"] = np.where(dim_month.month >= 11, dim_month.year + 1, dim_month.year)
    fq = ((dim_month.month - 11) % 12) // 3 + 1
    dim_month["fiscal_quarter"] = "FY" + dim_month.fiscal_year.astype(str) + "-Q" + fq.astype(str)
    dim_month["is_history"] = dim_month.month_start <= h_end

    # ---------------- Clean tables done; expected answers from clean data ----------------
    clean = {"dim_client": dim_client, "dim_banker": dim_banker, "dim_product": PRODUCTS, "dim_month": dim_month,
             "fact_revenue": fact_revenue, "fact_plan": fact_plan, "fact_capital": fact_capital,
             "fact_pipeline": fact_pipeline, "fact_coverage": fact_coverage, "gl_control": gl_control}

    # ---------------- Planted data-quality defects (silver must catch these) ----------------
    neg_candidates = pl[(pl.status == "Lost") & (pl.team != ST["capacity"]["team"]) & (pl.industry != "Industrials")
                        & (pl.expected_close_date < pd.Timestamp("2025-04-01"))].deal_id
    neg_deal = neg_candidates.iloc[0]
    raw_pipeline = fact_pipeline.copy()
    raw_pipeline.loc[raw_pipeline.deal_id == neg_deal, "expected_fee"] = -abs(raw_pipeline.loc[raw_pipeline.deal_id == neg_deal, "expected_fee"])
    dup_src = raw_pipeline[raw_pipeline.status == "Won"].sample(3, random_state=C["seed"])
    raw_pipeline = pd.concat([raw_pipeline, dup_src], ignore_index=True)
    raw_coverage = fact_coverage.copy()
    null_n = int(round(len(raw_coverage) * 0.01))
    null_idx = rng.choice(raw_coverage.index.to_numpy(), null_n, replace=False)
    raw_coverage.loc[null_idx, "banker_id"] = None

    raw = dict(clean)
    raw["fact_pipeline"] = raw_pipeline
    raw["fact_coverage"] = raw_coverage
    raw["dim_client"] = dim_client.drop(columns=["story_lending_only"])

    # silver-equivalent pipeline for answers: negative-fee deal is quarantined, duplicates removed
    silver_pipeline = fact_pipeline[fact_pipeline.deal_id != neg_deal]
    expected = expected_answers(C, clean, silver_pipeline, raw_coverage)
    expected["data_quality"] = {"null_banker_id_rows_in_coverage": int(null_n), "duplicate_deal_rows": 3,
                                "negative_fee_deal_ids": [neg_deal]}

    internal = {
        "recurring_plan": rec_plan[rec_plan.month_start > h_end].reset_index(drop=True),
        "capital_future": capital_future,
        "pipeline_schedule": pl[pl.status == "Open"][["deal_id", "client_id", "product_id", "banker_id", "team", "pitch_date", "mandate_date",
                                                      "execution_date", "expected_close_date", "fate_status", "fate_date", "fate_fee", "lost_at_stage"]].reset_index(drop=True),
        "client_meta": dim_client[["client_id", "segment", "team", "coverage_banker_id", "story_lending_only"]],
    }
    return {"raw": raw, "clean": clean, "internal": internal, "expected": expected}


# ---------------------------------------------------------------------------------------------
def expected_answers(C, t, pipeline, raw_coverage) -> dict:
    ST = C["stories"]
    hurdle = C["raroc"]["hurdle_rate"]
    tax = C["raroc"]["tax_rate"]
    q3 = ST["industrials_q3"]["quarter"]
    cl = t["dim_client"]
    name_of = dict(zip(cl.client_id, cl.client_name))
    ind_of = dict(zip(cl.client_id, cl.industry))

    def q(df):
        return df.month_start.map(quarter_label)

    # Story 1
    a = t["fact_revenue"][(t["fact_revenue"].client_id.map(ind_of) == "Industrials") & (q(t["fact_revenue"]) == q3)]
    p = t["fact_plan"][(t["fact_plan"].client_id.map(ind_of) == "Industrials") & (q(t["fact_plan"]) == q3)]
    by_prod = a.groupby("product_id").revenue.sum().to_frame("actual").join(p.groupby("product_id").plan_revenue.sum().to_frame("plan"), how="outer").fillna(0)
    by_prod["variance"] = by_prod.actual - by_prod.plan
    total_var = by_prod.variance.sum()
    by_prod["share_of_total_variance"] = by_prod.variance / total_var
    by_client = a.groupby("client_id").revenue.sum().to_frame("actual").join(p.groupby("client_id").plan_revenue.sum().to_frame("plan"), how="outer").fillna(0)
    by_client["variance"] = by_client.actual - by_client.plan
    top = by_client.sort_values("variance").head(5)
    h_end = pd.Timestamp(C["dates"]["history_end"]).date()
    slipped = pipeline[(pipeline.status == "Open") & (pipeline.expected_close_date <= h_end)]
    slipped_ind = slipped[(slipped.client_id.map(ind_of) == "Industrials") & (pd.to_datetime(slipped.expected_close_date).map(quarter_label) == q3)]
    story1 = {
        "quarter": q3, "industry": "Industrials",
        "revenue_actual": round(by_prod.actual.sum(), 2), "revenue_plan": round(by_prod.plan.sum(), 2), "variance": round(total_var, 2),
        "variance_by_product": [{"product_id": pid, "product_name": PRODUCT_NAME[pid], "actual": round(r.actual, 2), "plan": round(r.plan, 2),
                                 "variance": round(r.variance, 2), "share_of_total_variance": round(r.share_of_total_variance, 4)}
                                for pid, r in by_prod.sort_values("variance").iterrows()],
        "ma_share_of_variance": round(by_prod.loc["P02", "share_of_total_variance"], 4),
        "top_clients": [{"client_id": cid, "client_name": name_of[cid], "variance": round(r.variance, 2)} for cid, r in top.iterrows()],
        "slipped_deals": [{"deal_id": r.deal_id, "client_id": r.client_id, "client_name": name_of[r.client_id], "product": PRODUCT_NAME[r.product_id],
                           "expected_fee": r.expected_fee, "expected_close_date": str(r.expected_close_date), "stage": r.stage}
                          for r in slipped_ind.itertuples()],
        "all_open_slipped_deals_count": int(len(slipped)),
    }

    # Story 2 (RAROC trailing 12 months to Sep 2026)
    ttm = pd.date_range(end=pd.Timestamp(C["dates"]["history_end"]).to_period("M").to_timestamp(), periods=12, freq="MS")
    capm = t["fact_capital"][t["fact_capital"].month_start.isin(ttm)]
    revm = t["fact_revenue"][t["fact_revenue"].month_start.isin(ttm)]
    r_by = revm.groupby("client_id").revenue.sum()
    k = capm.groupby("client_id").agg(cap=("allocated_capital", "mean"), el=("expected_loss", "sum"), cost=("cost_allocation", "sum"))
    k["revenue"] = r_by.reindex(k.index).fillna(0)
    k["raroc"] = (k.revenue - k.cost - k.el) * (1 - tax) / k.cap
    held = revm[revm.revenue > 0].groupby("client_id").product_id.apply(lambda s: sorted(set(s)))
    below = k[k.raroc < hurdle].join(cl.set_index("client_id")[["client_name", "segment"]])
    below["products_held"] = held.reindex(below.index)
    mm_below = below[below.segment == "Mid-Market"].sort_values("raroc")
    all_products = list(PRODUCT_NAME)
    seg_raroc = k.join(cl.set_index("client_id").segment).groupby("segment").apply(
        lambda g: (g.revenue - g.cost - g.el).sum() * (1 - tax) / g.cap.sum(), include_groups=False)
    story2 = {
        "as_of_month": str(ttm[-1].date())[:7], "window": "trailing 12 months", "hurdle_rate": hurdle, "segment": "Mid-Market",
        "count": int(len(mm_below)), "average_raroc": round(float(mm_below.raroc.mean()), 4),
        "clients": [{"client_id": cid, "client_name": r.client_name, "raroc": round(float(r.raroc), 4),
                     "products_held": [PRODUCT_NAME[x] for x in r.products_held],
                     "missing_products": [PRODUCT_NAME[x] for x in all_products if x not in r.products_held],
                     "revenue_ttm": round(float(r.revenue), 2)} for cid, r in mm_below.iterrows()],
        "clients_below_hurdle_all_segments": int(len(below)),
        "raroc_by_segment": {s: round(float(v), 4) for s, v in seg_raroc.items()},
        "portfolio_raroc": round(float(((k.revenue - k.cost - k.el).sum() * (1 - tax)) / k.cap.sum()), 4),
    }

    # Story 3
    bk = t["dim_banker"]
    cap_rows = []
    rev_by_team = revm.assign(team=revm.client_id.map(dict(zip(cl.client_id, cl.team)))).groupby("team").revenue.sum()
    pq = pipeline[pipeline.status.isin(["Won", "Lost"])].copy()
    pq["team"] = pq.banker_id.map(dict(zip(bk.banker_id, bk.team)))
    pq["close_quarter"] = pd.to_datetime(pq.actual_close_date).map(quarter_label)
    last4 = ["2025-Q4", "2026-Q1", "2026-Q2", "2026-Q3"]
    for team, grp in bk.groupby("team"):
        n_cl = int((cl.team == team).sum())
        target = int(grp.capacity_target.iloc[0])
        wr = {}
        for qq in last4:
            g = pq[(pq.team == team) & (pq.close_quarter == qq)]
            wr[qq] = round(float((g.status == "Won").mean()), 4) if len(g) else None
        cap_rows.append({"team": team, "bankers": int(len(grp)), "clients": n_cl, "clients_per_banker": round(n_cl / len(grp), 2),
                         "capacity_target": target, "over_capacity": bool(n_cl / len(grp) > target),
                         "revenue_ttm": round(float(rev_by_team.get(team, 0)), 2),
                         "revenue_per_banker_ttm": round(float(rev_by_team.get(team, 0)) / len(grp), 2), "win_rate_by_quarter": wr})
    story3 = {"team": ST["capacity"]["team"], "teams": cap_rows}

    # Story 4
    f = t["fact_revenue"].groupby(["product_id", "month_start"]).revenue.sum()
    g = t["gl_control"].set_index(["product_id", "month_start"]).control_total
    diff = (g - f.reindex(g.index).fillna(0))
    breaks = diff[diff.abs() > 1000]
    story4 = {"tolerance": 1000, "breaks": [{"product_id": pid, "product_name": PRODUCT_NAME[pid], "month": str(m.date())[:7],
                                             "control_total": round(float(g[(pid, m)]), 2), "fact_total": round(float(f.get((pid, m), 0)), 2),
                                             "delta": round(float(v), 2)} for (pid, m), v in breaks.items()]}

    # Headline figures
    fr = t["fact_revenue"]
    fy = fr.assign(fy=np.where(fr.month_start.dt.month >= 11, fr.month_start.dt.year + 1, fr.month_start.dt.year))
    headline = {
        "revenue_ttm": round(float(revm.revenue.sum()), 2),
        "revenue_by_product_ttm": {PRODUCT_NAME[p]: round(float(v), 2) for p, v in revm.groupby("product_id").revenue.sum().items()},
        "revenue_by_segment_ttm": {s: round(float(v), 2) for s, v in revm.assign(s=revm.client_id.map(dict(zip(cl.client_id, cl.segment)))).groupby("s").revenue.sum().items()},
        "revenue_by_calendar_quarter": {qq: round(float(v), 2) for qq, v in fr.groupby(q(fr)).revenue.sum().items()},
        "products_per_client_ttm": round(float(revm[revm.revenue > 0].groupby("client_id").product_id.nunique().mean()), 3),
        "open_deals": int((pipeline.status == "Open").sum()),
        "weighted_pipeline": round(float((pipeline[pipeline.status == "Open"].expected_fee * pipeline[pipeline.status == "Open"].probability).sum()), 2),
    }
    return {"generated_from": "generator/generate.py", "constants_version": C["version"], "bank": C["bank"],
            "currency": C["currency"], "story1_industrials_q3": story1, "story2_whitespace": story2,
            "story3_capacity": story3, "story4_recon": story4, "headline": headline}


def write(result: dict, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "_internal").mkdir(exist_ok=True)
    for name, df in result["raw"].items():
        df.to_parquet(out / f"{name}.parquet", index=False)
    for name, df in result["internal"].items():
        df.to_parquet(out / "_internal" / f"{name}.parquet", index=False)
    with open(out / "expected_answers.json", "w", encoding="utf-8") as f:
        json.dump(result["expected"], f, indent=2, default=str)


def summary(result: dict) -> str:
    e = result["expected"]
    h, s1, s2, s3, s4 = e["headline"], e["story1_industrials_q3"], e["story2_whitespace"], e["story3_capacity"], e["story4_recon"]
    m = lambda v: f"${v / 1e6:,.1f}M"  # noqa: E731
    lines = ["Northlake Bank (fictional) · synthetic data summary", "",
             f"Revenue, trailing 12 months to Sep 2026: {m(h['revenue_ttm'])}   products per client: {h['products_per_client_ttm']}",
             "  by segment: " + ", ".join(f"{k} {m(v)}" for k, v in h["revenue_by_segment_ttm"].items()),
             "  by product: " + ", ".join(f"{k} {m(v)}" for k, v in h["revenue_by_product_ttm"].items()),
             f"Open deals {h['open_deals']}, weighted pipeline {m(h['weighted_pipeline'])}",
             "RAROC by segment (TTM): " + ", ".join(f"{k} {v:.1%}" for k, v in s2["raroc_by_segment"].items()) + f"   portfolio {s2['portfolio_raroc']:.1%}",
             "", "Story 1 · Industrials Q3 2026",
             f"  actual {m(s1['revenue_actual'])} vs plan {m(s1['revenue_plan'])} → {m(s1['variance'])}; M&A share {s1['ma_share_of_variance']:.0%}",
             "  slipped: " + "; ".join(f"{d['client_name']} {m(d['expected_fee'])} (exp. {d['expected_close_date']})" for d in s1["slipped_deals"]),
             "", "Story 2 · Mid-Market below the 15% hurdle (TTM)",
             f"  {s2['count']} clients, average RAROC {s2['average_raroc']:.1%}; all segments below hurdle: {s2['clients_below_hurdle_all_segments']}",
             "", "Story 3 · Coverage capacity and win rate (last 4 quarters)"]
    for t in s3["teams"]:
        wr = " → ".join(f"{v:.0%}" if v is not None else "n/a" for v in t["win_rate_by_quarter"].values())
        lines.append(f"  {t['team']:24} {t['clients_per_banker']:>5} clients/banker (target {t['capacity_target']}){' OVER' if t['over_capacity'] else '     '}  win rate {wr}")
    lines += ["", "Story 4 · Reconciliation breaks"]
    lines += [f"  {b['product_name']} {b['month']}: control {m(b['control_total'])} vs booked {m(b['fact_total'])} → delta {m(b['delta'])}" for b in s4["breaks"]]
    dq = e["data_quality"]
    lines += ["", f"Planted DQ defects: {dq['null_banker_id_rows_in_coverage']} null banker_id rows, {dq['duplicate_deal_rows']} duplicate deals, negative fee on {dq['negative_fee_deal_ids'][0]}"]
    return "\n".join(lines)


if __name__ == "__main__":
    res = build()
    write(res)
    print(summary(res))
    print(f"\nWrote {len(res['raw'])} tables + _internal/ + expected_answers.json to {OUT.relative_to(ROOT)}/")
