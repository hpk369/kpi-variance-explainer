"""The agent graph with a scripted LLM and canned tool results (no Databricks): answer, retry, blocked, refusal."""
from types import SimpleNamespace as NS

import pytest

from agent import graph

KPI = {"tool": "get_kpi", "args": {"metric": "revenue", "period": "2026-Q3", "segment": "Industrials"},
       "sql": "SELECT * FROM cib_demo.gold.get_kpi('revenue', '2026-Q3', 'Industrials')", "columns": [], "row_count": 1,
       "rows": [{"metric": "revenue", "actual": 38036512.05, "plan": 46836512.03, "var_vs_plan": -8799999.98,
                 "pct_vs_plan": -0.1879, "as_of_date": "2026-11-23"}], "error": None, "notes": [], "seconds": 0.1}
DRIVERS = {"tool": "variance_drivers", "args": {"metric": "revenue", "period": "2026-Q3"}, "sql": "SELECT ...",
           "columns": [], "row_count": 2, "error": None, "notes": [], "seconds": 0.1,
           "rows": [{"dimension_value": "M&A Advisory", "delta": -7500000.0, "share_of_total_delta": 0.8523},
                    {"dimension_value": "Lending", "delta": -1279359.87, "share_of_total_delta": 0.1454}]}
GOOD = """**Headline:** Industrials revenue was $8.8M (18.8%) below plan in 2026-Q3.

**Drivers**
- M&A Advisory: −$7.5M, 85% of the gap.
- Lending: −$1.3M, 15% of the gap.

**Executive paragraph**
Industrials missed plan by $8.8M; M&A Advisory timing explains 85%.

**Definitions used**
- Revenue vs Plan: booked revenue minus plan (Source: CIB Business Definitions Glossary)

_Synthetic data · Northlake Bank (fictional). Data as of 2026-11-23._"""
BAD = GOOD.replace("$7.5M", "$9.4M")


def call(name, args):
    return {"id": name, "type": "function", "function": NS(name=name, arguments=args)}


def tool_msg(*calls):
    return NS(content="", tool_calls=[NS(id=c["id"], function=c["function"]) for c in calls])


@pytest.fixture
def fake(monkeypatch):
    script = {"plan": [], "draft": [], "claims": [], "calls": 0}

    def llm(messages, **kw):
        script["calls"] += 1
        return script["plan"].pop(0), "fake-model"

    def draft_llm(messages):
        script["calls"] += 1
        text = script["draft"].pop(0).content
        return text, ("fake-model" if text else None), [{"model": "fake-model", "max_tokens": 6000,
                                                          "finish_reason": "stop" if text else "length",
                                                          "chars": len(text), "completion_tokens": 1}]

    def verify_claims(question, answer, evidence):
        return script["claims"].pop(0) if script["claims"] else {"unsupported": [], "model": "fake-checker"}

    canned = {"get_kpi": KPI, "variance_drivers": DRIVERS}
    monkeypatch.setattr(graph, "llm", llm)
    monkeypatch.setattr(graph, "draft_llm", draft_llm)
    monkeypatch.setattr(graph, "verify_claims", verify_claims)
    monkeypatch.setattr(graph.tools, "call", lambda name, args: dict(canned[name]))
    return script


def run(q="Why did Industrials revenue miss plan in Q3?", **kw):
    return graph.run(q, use_vector_search=False, **kw)


def plan_two_tools(script):
    script["plan"] += [tool_msg(call("get_kpi", '{"metric":"revenue","period":"2026-Q3","segment":"Industrials"}'),
                                call("variance_drivers", '{"metric":"revenue","period":"2026-Q3"}')),
                       NS(content="READY", tool_calls=None)]


def test_answer_passes_number_check(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None)]
    w = run()
    assert w["outcome"] == "answered" and w["answer"] == GOOD
    assert [c["name"] for c in w["tool_calls"]] == ["get_kpi", "variance_drivers"]
    assert w["number_check"] == {**w["number_check"], "passed": True, "retries": 0}
    assert w["as_of"] == "2026-11-23" and w["retrieval_mode"] == "local_keyword"
    assert any(r["doc_id"] == "glossary" for r in w["retrieved"])


def test_wrong_number_is_fixed_on_retry(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=BAD, tool_calls=None), NS(content=GOOD, tool_calls=None)]
    w = run()
    assert w["outcome"] == "answered" and w["number_check"]["retries"] == 1
    assert w["number_check"]["attempts"][0]["unmatched"] == ["$9.4M"]


def test_injected_wrong_number_is_blocked_after_two_retries(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None) for _ in range(3)]
    w = run(inject_wrong_number=True)
    assert w["outcome"] == "blocked" and w["number_check"]["retries"] == 2 and not w["number_check"]["passed"]
    assert w["answer"].startswith("**Commentary withheld: figures could not be verified.**")
    assert "M&A Advisory" in w["answer"] and "get_kpi(" in w["answer"]


def test_real_bank_question_is_refused_without_any_call(fake):
    w = run("What's TD's real Q3 revenue?")
    assert w["outcome"] == "refused" and w["tool_calls"] == [] and fake["calls"] == 0
    assert "fictional" in w["answer"] and "synthetic" in w["answer"]
    assert "TD" not in w["answer"] and "real bank" in w["answer"]   # the reply never names a real bank


def test_real_estate_is_not_mistaken_for_real_data(fake):
    fake["plan"] += [NS(content="OUT_OF_SCOPE: test", tool_calls=None)]
    w = run("How did real estate revenue do in Q3?")
    assert fake["calls"] == 1                  # the guard let it through to the planner
    assert w["outcome"] == "refused"           # (the scripted planner then refused)


def test_planner_is_nudged_once_to_use_tools(fake):
    fake["plan"] += [NS(content="Revenue was fine.", tool_calls=None)]
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None)]
    w = run()
    assert w["outcome"] == "answered" and len(w["tool_calls"]) == 2


def test_bad_tool_arguments_go_back_to_the_model(fake):
    fake["plan"] += [tool_msg(call("get_kpi", "{not json")), ]
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None)]
    w = run()
    assert w["tool_calls"][0]["error"].startswith("could not read the arguments")
    assert w["outcome"] == "answered"


def test_empty_draft_never_passes(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content="", tool_calls=None), NS(content=GOOD, tool_calls=None)]
    w = run()
    assert w["outcome"] == "answered" and w["answer"] == GOOD and w["number_check"]["retries"] == 1
    assert w["number_check"]["attempts"][0]["unmatched"][0].startswith("(empty draft")


def test_always_empty_is_blocked(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content="", tool_calls=None) for _ in range(3)]
    w = run()
    assert w["outcome"] == "blocked" and w["answer"].startswith("**Commentary withheld")


def test_missing_section_and_long_paragraph_are_sent_back():
    assert graph.format_problems(GOOD) == []
    assert "(missing section: Drivers)" in graph.format_problems(GOOD.replace("**Drivers**", "**Why**"))
    long = GOOD.replace("M&A Advisory timing explains 85%.", "word " * 120)
    assert any("executive paragraph has" in p for p in graph.format_problems(long))


def test_python_summary_of_whitespace_list():
    from agent.summaries import cross_sell
    rows = [{"segment": "Mid-Market", "products_held": "Lending", "raroc": 0.06 + i / 1000} for i in range(18)] + \
           [{"segment": "Financial Institutions", "products_held": "Rates & FX Hedging", "raroc": 0.048}]
    s = cross_sell(rows)
    assert s[0]["group_by"] == "all" and s[0]["clients"] == 19
    assert s[0]["raroc_lowest"] == 0.048 and s[0]["raroc_highest"] == 0.077
    mm = next(r for r in s if r.get("group_by") == "lending_only_by_segment" and r["segment"] == "Mid-Market")
    assert mm["clients"] == 18 and mm["raroc_min"] == 0.06 and mm["raroc_max"] == 0.077


def test_footer_is_added_when_missing(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD.split("_Synthetic")[0], tool_calls=None)]
    w = run()
    assert w["answer"].rstrip().endswith("Data as of 2026-11-23._")


def test_definition_question_needs_no_tools(fake):
    fake["plan"] += [NS(content="READY", tool_calls=None)]
    fake["draft"] += [NS(content=GOOD.replace("$8.8M (18.8%) below plan in 2026-Q3", "a definition")
                         .replace("−$7.5M, 85% of the gap", "a").replace("−$1.3M, 15% of the gap", "b")
                         .replace("Industrials missed plan by $8.8M; M&A Advisory timing explains 85%.", "c"), tool_calls=None)]
    w = run("What is RAROC?")
    assert w["outcome"] == "answered" and w["tool_calls"] == [] and fake["calls"] == 2
    assert "evidence" in w and w["evidence"]["tool_results"] == []


def test_future_period_skips_retrieval_and_passes(fake, monkeypatch):
    err = dict(KPI, rows=[], row_count=0, error="Period starts after the last day of data (2026-11-24).")
    monkeypatch.setattr(graph.tools, "call", lambda name, args: dict(err))
    monkeypatch.setattr(graph, "_as_of", lambda calls: "2026-11-24")
    fake["plan"] += [tool_msg(call("get_kpi", '{"metric":"revenue","period":"2027-Q3","segment":"Industrials"}')),
                     NS(content="READY", tool_calls=None)]
    no_data = ("**Headline:** There is no data for Q3 2027 yet; data runs to 2026‑11‑24.\n\n**Drivers**\n- none\n\n"
               "**Executive paragraph**\nNo figures exist for that period.\n\n**Definitions used**\n- none")
    fake["draft"] += [NS(content=no_data, tool_calls=None)]
    w = run("What was Industrials revenue in Q3 2027?")
    assert w["outcome"] == "answered" and w["number_check"]["passed"]
    assert w["retrieved"] == [] and w["retrieval_mode"].startswith("skipped")


def test_footer_is_replaced_with_the_real_as_of_date(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD.replace("Data as of 2026-11-23", "Data as of 2026-10-31"), tool_calls=None)]
    w = run()
    assert w["answer"].count("Synthetic data") == 1 and w["answer"].rstrip().endswith("Data as of 2026-11-23._")


def test_trend_words_are_computed_not_guessed():
    from agent.summaries import coverage, direction
    assert direction([0.4375, 0.4, 0.3, 0.25]) == "falling every quarter"
    assert direction([16, 15, 20, 16]).startswith("mixed")
    rows = [{"team": "Coverage East 2", "quarter": q, "win_rate": w, "deals_closed": d, "clients_per_banker": 14.0,
             "capacity_target": 10, "over_capacity": True}
            for q, w, d in [("2025-Q4", 0.4375, 16), ("2026-Q1", 0.4, 15), ("2026-Q2", 0.3, 20), ("2026-Q3", 0.25, 16)]]
    s = coverage(rows)[0]
    assert s["win_rate_trend"] == "falling every quarter" and s["deals_closed_trend"].startswith("mixed")


def test_tidy_removes_leaked_internals_but_keeps_prose():
    raw = ("- All 18 hold only Lending (products_held = Lending).\n- Range 6.1% to 9.0% (raroc_min = 0.0606).\n"
           "- 18 clients (cross‑sell whitespace totals).\n- Top is Riverbend (raroc 0.0899).\n"
           "- Missed by $8.8M (‑19%)【get_kpi】; Coverage East 2 (Canada East) is over capacity.")
    assert graph.tidy(raw) == ("- All 18 hold only Lending.\n- Range 6.1% to 9.0%.\n- 18 clients.\n- Top is Riverbend.\n"
                               "- Missed by $8.8M (‑19%); Coverage East 2 (Canada East) is over capacity.")



def test_unsupported_claim_is_sent_back_and_fixed(fake):
    plan_two_tools(fake)
    wrong = GOOD.replace("85% of the gap.", "85% of the gap; all other products beat plan.")
    fake["draft"] += [NS(content=wrong, tool_calls=None), NS(content=GOOD, tool_calls=None)]
    fake["claims"] += [{"unsupported": [{"claim": "all other products beat plan", "evidence_says": "Lending missed plan"}],
                        "model": "fake-checker"}]
    w = run()
    assert w["outcome"] == "answered" and w["answer"].startswith(GOOD.split("_Synthetic")[0].strip()[:40])
    first = w["number_check"]["attempts"][0]
    assert not first["passed"] and first["claims_failed"] and "all other products beat plan" in first["unmatched"][0]
    assert w["number_check"]["retries"] == 1 and w["claim_check"]["passed"] and len(w["claim_check"]["runs"]) == 2


def test_claims_failing_three_times_are_blocked(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None) for _ in range(3)]
    fake["claims"] += [{"unsupported": [{"claim": "x", "evidence_says": "y"}], "model": "fake-checker"} for _ in range(3)]
    w = run()
    assert w["outcome"] == "blocked" and w["number_check"]["retries"] == 2 and not w["claim_check"]["passed"]


def test_claim_checker_outage_does_not_block(fake):
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None)]
    fake["claims"] += [{"unsupported": [], "model": "fake-checker", "error": "timeout"}]
    w = run()
    assert w["outcome"] == "answered" and w["claim_check"]["runs"][0]["error"] == "timeout"


def test_claim_check_switch(fake, monkeypatch):
    monkeypatch.setattr(graph, "CLAIM_CHECK", False)
    plan_two_tools(fake)
    fake["draft"] += [NS(content=GOOD, tool_calls=None)]
    fake["claims"] += [{"unsupported": [{"claim": "x", "evidence_says": "y"}], "model": "fake-checker"}]
    w = run()
    assert w["outcome"] == "answered" and w["claim_check"]["runs"] == []


def test_capacity_status_says_at_target_and_slipped_deals_name_both_gaps():
    # Nov 25 eval: "all other teams below target" (FI & Sponsors was at 12 vs 12) and "closed 84 days after the quarter"
    from agent.summaries import coverage, slipped
    rows = [{"team": t, "quarter": "2026-Q3", "clients_per_banker": c, "capacity_target": g, "win_rate": 0.3}
            for t, c, g in [("Coverage East 2", 14.0, 10), ("Coverage FI & Sponsors", 12.0, 12), ("Coverage East 1", 5.0, 8)]]
    s = {r.get("team"): r for r in coverage(rows)}
    assert [s[t]["capacity_status"] for t in ("Coverage East 2", "Coverage FI & Sponsors", "Coverage East 1")] == \
        ["over target", "at target", "under target"]
    assert s[None]["names_at_target"] == "Coverage FI & Sponsors" and s[None]["teams_under_target"] == 1
    d = slipped([{"deal_id": "D1", "client_name": "Meadowlark Fabrication (fictional)", "expected_close_date": "2026-08-20",
                  "actual_close_date": "2026-11-12", "period_end": "2026-09-30", "days_late": 84}, {"row_type": "summary"}])
    assert len(d) == 1 and d[0]["days_after_expected_close"] == 84 and d[0]["days_after_period_end"] == 43


def test_tidy_removes_flags_and_summary_row_labels():
    # Nov 25 recheck: "14 vs. target 10 (over capacity flag true)" and "Teams over target: 1 (Coverage East 2) – summary row."
    raw = ("- Clients per banker: 14 vs. target 10 (over capacity flag true) – Coverage East 2.\n"
           "- Teams over target: 1 (Coverage East 2) – summary row.")
    assert graph.tidy(raw) == ("- Clients per banker: 14 vs. target 10 – Coverage East 2.\n"
                               "- Teams over target: 1 (Coverage East 2).")


def test_tidy_removes_bare_flags_tool_and_table_names():
    raw = ("- Clients per banker: 14.0 (exceeds target 10) – over_capacity true.\n"
           "- Calculated by the Coverage COO office using `gold.fact_pipeline` (tool `get_kpi`).\n"
           "- Trend is falling every quarter in the totals row.")
    assert graph.tidy(raw) == ("- Clients per banker: 14.0 (exceeds target 10).\n"
                               "- Calculated by the Coverage COO office.\n"
                               "- Trend is falling every quarter.")


def test_cross_sell_extremes_name_the_right_clients():
    # Nov 25 15:58 eval: the writer named the wrong client as "largest allocated capital"
    from agent.summaries import cross_sell
    rows = [{"client_name": "Eastgate Fabrication", "segment": "Mid-Market", "products_held": "Lending", "raroc": 0.08,
             "revenue_ttm": 1.3e6, "allocated_capital_avg": 5.2e6},
            {"client_name": "Driftwood Life Sciences", "segment": "Mid-Market", "products_held": "Lending", "raroc": 0.07,
             "revenue_ttm": 0.65e6, "allocated_capital_avg": 5.5e6}]
    top = cross_sell(rows)[0]
    assert top["capital_highest_client"] == "Driftwood Life Sciences" and top["capital_highest"] == 5.5e6
    assert top["revenue_12m_lowest"] == 0.65e6 and top["raroc_lowest_client"] == "Driftwood Life Sciences"
