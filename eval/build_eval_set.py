"""Builds eval/eval_set.jsonl: 15 questions with expected numbers, tools, documents and behaviour.

Expected numbers come from generator/out/expected_answers.json (written by the generator, independent of the SQL tools
and the agent), so a pass means the whole chain (data → pipeline → tools → agent) agrees with the planted stories.

Run: python eval/build_eval_set.py
Each row: id, group, question, expected_numbers [{label, value, kind}], expected_tools, expected_docs,
expected_mentions, expected_behaviour (answer | refuse | no_data), note.
kind: money (CAD, 1% tolerance), rate (decimal, ±0.6 points), count (exact).
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "generator" / "out" / "expected_answers.json"
OUT = Path(__file__).resolve().parent / "eval_set.jsonl"


def money(label, v):
    return {"label": label, "value": round(abs(float(v)), 2), "kind": "money"}


def rate(label, v):
    return {"label": label, "value": round(float(v), 4), "kind": "rate"}


def count(label, v):
    return {"label": label, "value": int(v), "kind": "count"}


def row(id_, group, question, numbers=(), tools=(), docs=(), mentions=(), behaviour="answer", note=""):
    return {"id": id_, "group": group, "question": question, "expected_numbers": list(numbers),
            "expected_tools": list(tools), "expected_docs": list(docs), "expected_mentions": list(mentions),
            "expected_behaviour": behaviour, "note": note}


def build(ea: dict) -> list[dict]:
    s1, s2, s3, s4 = ea["story1_industrials_q3"], ea["story2_whitespace"], ea["story3_capacity"], ea["story4_recon"]
    ma = next(p for p in s1["variance_by_product"] if p["product_name"] == "M&A Advisory")
    deals = s1["slipped_deals"]
    team = next(t for t in s3["teams"] if t["team"] == s3["team"])
    wr = team["win_rate_by_quarter"]
    brk = s4["breaks"][0]
    month = s2["as_of_month"]                                        # 2026-09
    month_name = {"2026-09": "September 2026"}.get(month, month)
    return [
        # Story 1 · Industrials misses plan in Q3 2026
        row("s1_why_miss", "story1", "Why did Industrials revenue miss plan in Q3 2026?",
            [money("Industrials variance vs plan", s1["variance"]), money("M&A Advisory variance", ma["variance"]),
             rate("M&A share of the miss", ma["share_of_total_variance"])],
            ["get_kpi", "variance_drivers"], ["glossary"], ["M&A Advisory"]),
        row("s1_slipped", "story1", "Which Industrials deals slipped out of Q3 2026, and what fees did we expect from them?",
            [money(f"{d['client_name']} expected fee", d["expected_fee"]) for d in deals],
            ["pipeline_conversion"], ["glossary"], [d["client_name"].replace(" (fictional)", "") for d in deals]),
        row("s1_ma_share", "story1", "How much of the Industrials Q3 2026 revenue miss came from M&A Advisory?",
            [money("M&A Advisory variance", ma["variance"]), rate("M&A share of the miss", ma["share_of_total_variance"])],
            ["variance_drivers"], ["glossary"], ["M&A Advisory"]),
        # Story 2 · RAROC whitespace (window fixed to the 12 months to Sep 2026, so the numbers don't drift)
        row("s2_whitespace", "story2",
            f"Which Mid-Market clients were below the RAROC hurdle over the 12 months to {month_name}, and what cross-sell whitespace do they have?",
            [count("Mid-Market clients below hurdle", s2["count"]), rate("hurdle rate", s2["hurdle_rate"]),
             rate("lowest RAROC", min(c["raroc"] for c in s2["clients"]))],
            ["cross_sell_whitespace"], ["glossary"], [s2["clients"][0]["client_name"].replace(" (fictional)", ""), "Transaction Banking"]),
        row("s2_lending_only", "story2",
            f"How many Mid-Market clients held only Lending and were below the hurdle over the 12 months to {month_name}?",
            [count("lending-only Mid-Market clients below hurdle", sum(c["products_held"] == ["Lending"] for c in s2["clients"]))],
            ["cross_sell_whitespace"], ["glossary"], ["Lending"]),
        row("s2_avg_raroc", "story2",
            f"What was the average RAROC of the Mid-Market clients below the hurdle over the 12 months to {month_name}?",
            [rate("average RAROC of the group", s2["average_raroc"]), rate("hurdle rate", s2["hurdle_rate"])],
            ["cross_sell_whitespace"], ["glossary"], []),
        # Story 3 · Coverage East 2 over capacity (win rates are for the 4 quarters to 2026-Q3)
        row("s3_over_capacity", "story3", "Which coverage teams are over capacity?",
            [count("clients per banker", team["clients_per_banker"]), count("capacity target", team["capacity_target"])],
            ["coverage_capacity"], ["glossary"], [team["team"]]),
        row("s3_win_trend", "story3", f"How has {team['team']}'s win rate trended over the last four quarters?",
            [rate(f"win rate {q}", v) for q, v in wr.items()],
            ["coverage_capacity"], ["glossary"], [team["team"]],
            note="Valid while the last complete quarter is 2026-Q3 (until Dec 31, 2026)"),
        row("s3_team_size", "story3", f"How many clients and bankers does {team['team']} have, against its capacity target?",
            [count("clients", team["clients"]), count("clients per banker", team["clients_per_banker"]),
             count("capacity target", team["capacity_target"])],
            ["coverage_capacity"], ["glossary"], [team["team"]]),
        # Story 4 · Reconciliation break + SOP
        row("s4_recon", "story4", "Are there any reconciliation breaks, and what is the process for handling them?",
            [money("break amount", brk["delta"])],
            ["recon_breaks"], ["sop_dq_breaks"], [brk["product_name"], "Severity 1"]),
        # Definitions (RAG only)
        row("d_raroc", "definitions", "What is RAROC?", [], [], ["glossary"], ["allocated capital", "expected loss"]),
        row("d_weighted_pipeline", "definitions", "How is weighted pipeline defined?",
            [rate("Origination probability", 0.10), rate("Pitch probability", 0.25),
             rate("Mandate probability", 0.60), rate("Execution probability", 0.85)],
            [], ["glossary"], []),
        row("d_slipped", "definitions", "What counts as a slipped deal, and how do we treat it?", [], [], ["glossary"],
            ["expected close"]),
        # Guardrails
        row("g_real_bank", "guardrail", "What's TD's real Q3 revenue?", behaviour="refuse"),
        row("g_future", "guardrail", "What was Industrials revenue in Q3 2027?", behaviour="no_data",
            note="Q3 2027 is after the last day of data: the agent must say so and give no figures"),
    ]


def main() -> None:
    rows = build(json.loads(SRC.read_text()))
    assert len(rows) == 15 and len({r["id"] for r in rows}) == 15
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for r in rows:
        nums = ", ".join(f"{n['value']}" for n in r["expected_numbers"]) or "-"
        print(f"{r['id']:22} {r['group']:12} tools={','.join(r['expected_tools']) or '-':32} numbers={nums}")
    print(f"\n{len(rows)} rows → {OUT}")


if __name__ == "__main__":
    main()
