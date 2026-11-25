"""Deterministic summaries of tool results, computed in Python (not by the LLM), so the answer can say
"20 clients, 18 of them Mid-Market, RAROC 6.0%-9.4%" without the model doing arithmetic.
The summary rows are shown to the drafting model and are allowed values for the number check.
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

from collections import defaultdict


def _group(rows: list[dict], key: str, metric: str | None = None) -> list[dict]:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r.get(key)].append(r)
    out = []
    for k, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        row = {"group_by": key, key: k, "clients": len(rs)}
        vals = [r[metric] for r in rs if isinstance(r.get(metric), (int, float))] if metric else []
        if vals:
            row.update({f"{metric}_min": min(vals), f"{metric}_max": max(vals), f"{metric}_avg": round(sum(vals) / len(vals), 4)})
        out.append(row)
    return out


def _extremes(rows: list[dict], metric: str, label: str) -> dict:
    vals = [r for r in rows if isinstance(r.get(metric), (int, float))]
    if not vals:
        return {}
    lo, hi = min(vals, key=lambda r: r[metric]), max(vals, key=lambda r: r[metric])
    return {f"{label}_lowest": lo[metric], f"{label}_lowest_client": lo.get("client_name"),
            f"{label}_highest": hi[metric], f"{label}_highest_client": hi.get("client_name")}


def cross_sell(rows: list[dict]) -> list[dict]:
    out = [{"group_by": "all", "clients": len(rows), **_extremes(rows, "raroc", "raroc"),
            **_extremes(rows, "revenue_ttm", "revenue_12m"), **_extremes(rows, "allocated_capital_avg", "capital")}]
    out += _group(rows, "segment", "raroc")
    out += _group(rows, "products_held", "raroc")
    lending_only = [r for r in rows if r.get("products_held") == "Lending"]
    out += [{**g, "group_by": "lending_only_by_segment"} for g in _group(lending_only, "segment", "raroc")]
    return out


def direction(values: list) -> str | None:
    """'falling every quarter', 'rising every quarter', 'flat' or 'mixed': computed here so the writer never guesses."""
    v = [x for x in values if isinstance(x, (int, float))]
    if len(v) < 2:
        return None
    steps = [b - a for a, b in zip(v, v[1:])]
    if all(s < 0 for s in steps):
        return "falling every quarter"
    if all(s > 0 for s in steps):
        return "rising every quarter"
    if all(s == 0 for s in steps):
        return "flat"
    return "mixed (up in some quarters, down in others)"


def coverage(rows: list[dict]) -> list[dict]:
    out = []
    for team in sorted({r.get("team") for r in rows if r.get("team")}):
        rs = sorted((r for r in rows if r.get("team") == team), key=lambda r: r.get("quarter") or "")
        rates = [(r.get("quarter"), r.get("win_rate")) for r in rs if r.get("win_rate") is not None]
        out.append({"team": team, "clients_per_banker": rs[-1].get("clients_per_banker"),
                    "capacity_target": rs[-1].get("capacity_target"), "over_capacity": rs[-1].get("over_capacity"),
                    "capacity_status": capacity_status(rs[-1].get("clients_per_banker"), rs[-1].get("capacity_target")),
                    "win_rate_first_quarter": rates[0][0] if rates else None, "win_rate_first": rates[0][1] if rates else None,
                    "win_rate_last_quarter": rates[-1][0] if rates else None, "win_rate_last": rates[-1][1] if rates else None,
                    "win_rate_trend": direction([r.get("win_rate") for r in rs]),
                    "deals_closed_trend": direction([r.get("deals_closed") for r in rs]),
                    "deals_won_trend": direction([r.get("deals_won") for r in rs])})
    by_status = {s: [r["team"] for r in out if r["capacity_status"] == s] for s in ("over target", "at target", "under target")}
    out.append({"group_by": "capacity_status", **{f"teams_{s.replace(' ', '_')}": len(t) for s, t in by_status.items()},
                **{f"names_{s.replace(' ', '_')}": ", ".join(t) or "none" for s, t in by_status.items()}})
    return out


def capacity_status(clients_per_banker, target) -> str | None:
    """'over target', 'at target' or 'under target': a team exactly at its target is not under it."""
    if not isinstance(clients_per_banker, (int, float)) or not isinstance(target, (int, float)):
        return None
    if abs(clients_per_banker - target) < 1e-9:
        return "at target"
    return "over target" if clients_per_banker > target else "under target"


def _date(v):
    import datetime as dt
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def slipped(rows: list[dict]) -> list[dict]:
    """One row per listed deal with both gaps named, so "84 days late" is never read as "84 days after the quarter"."""
    out = []
    for r in rows:
        if not r.get("deal_id"):
            continue
        exp, act, end = _date(r.get("expected_close_date")), _date(r.get("actual_close_date")), _date(r.get("period_end"))
        out.append({"client_name": r.get("client_name"), "deal_name": r.get("deal_name"),
                    "expected_close_date": r.get("expected_close_date"), "actual_close_date": r.get("actual_close_date"),
                    "days_after_expected_close": r.get("days_late"),
                    "days_after_period_end": (act - end).days if act and end else None,
                    "status_now": r.get("status_now")})
    return out


SUMMARIES = {"cross_sell_whitespace": cross_sell, "coverage_capacity": coverage, "pipeline_conversion": slipped}


def summarize(call: dict) -> list[dict] | None:
    fn = SUMMARIES.get(call.get("tool"))
    return fn(call["rows"]) if fn and call.get("rows") else None
