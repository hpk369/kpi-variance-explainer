"""Eval set and code scorers, without Databricks or an LLM."""
import json
from pathlib import Path

from eval import scorers
from eval.build_eval_set import build
from eval.evaluation import load_set, scorecard

ROOT = Path(__file__).resolve().parents[1]
ROWS = load_set()


def test_eval_set_has_15_rows_in_the_planned_groups():
    assert len(ROWS) == 15 and len({r["id"] for r in ROWS}) == 15
    groups = [r["group"] for r in ROWS]
    assert {g: groups.count(g) for g in set(groups)} == {"story1": 3, "story2": 3, "story3": 3, "story4": 1,
                                                         "definitions": 3, "guardrail": 2}


def test_eval_set_file_matches_the_builder():
    ea = json.loads((ROOT / "generator" / "out" / "expected_answers.json").read_text())
    assert build(ea) == ROWS


def test_number_match_formats_and_tolerances():
    nums = [{"label": "miss", "value": 8799999.98, "kind": "money"}, {"label": "share", "value": 0.8523, "kind": "rate"},
            {"label": "clients", "value": 18, "kind": "count"}, {"label": "target", "value": 10, "kind": "count"}]
    assert scorers.number_match("Missed by $8.8M; M&A is 85% of it; eighteen clients; target of 10.", nums)["value"] == 1.0
    r = scorers.number_match("Missed by $9.4M; M&A is 80%; 17 clients; target 10.", nums)
    assert r["value"] == 0.25 and "miss" in r["rationale"] and "share" in r["rationale"]
    assert scorers.number_match("anything", []) is None


def test_tool_selection_and_guardrails():
    assert scorers.tool_selection(["get_kpi", "variance_drivers"], ["get_kpi"], "answer")["value"] is True
    assert scorers.tool_selection(["get_kpi"], ["pipeline_conversion"], "answer")["value"] is False
    assert scorers.tool_selection([], [], "refuse")["value"] is True
    assert scorers.tool_selection(["get_kpi"], [], "refuse")["value"] is False
    assert scorers.tool_selection([], [], "answer") is None
    assert scorers.guardrail("refused", "Northlake is fictional; synthetic data.", "refuse")["value"] is True
    assert scorers.guardrail("answered", "There is no data for Q3 2027 yet; data runs to 2026-11-23.", "no_data")["value"] is True
    assert scorers.guardrail("answered", "Q3 2027 revenue will be about $40.1M.", "no_data")["value"] is False


def test_scorecard_counts_only_applicable_rows():
    res = [{"group": "story1", "outcome": "answered", "latency_ms": 1000,
            "scores": {"number_match": {"value": 1.0}, "tool_selection": {"value": True}, "guardrail": None,
                       "groundedness": {"value": True}, "retrieval_relevance": {"value": 0.8}}},
           {"group": "story2", "outcome": "answered", "latency_ms": 3000,
            "scores": {"number_match": {"value": 0.5}, "tool_selection": {"value": False}, "guardrail": None,
                       "groundedness": {"value": None, "error": True, "rationale": "x"}, "retrieval_relevance": {"value": 1.0}}},
           {"group": "guardrail", "outcome": "refused", "latency_ms": 0,
            "scores": {"number_match": None, "tool_selection": {"value": True}, "guardrail": {"value": True}}}]
    c = scorecard(res)
    assert c["number_accuracy"] == 0.5 and c["number_accuracy_stories_1_3"] == 0.5
    assert c["tool_selection"] == round(2 / 3, 4) and c["groundedness"] == 1.0 and c["retrieval_relevance"] == 0.9
    assert (c["guardrails_passed"], c["guardrails_total"], c["judge_errors"]) == (1, 1, 1)


def test_judge_is_not_the_writer(monkeypatch):
    from agent.graph import draft_models
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    for k in ("LLM_ENDPOINT", "LLM_FALLBACK_ENDPOINT", "DRAFT_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    assert draft_models()[0] == "gpt-oss-120b"
    assert scorers.judge_model() == "qwen35-122b-a10b"


def test_slipped_deal_lookup_does_not_fetch_win_rate():
    from agent.retriever import terms_for
    assert terms_for([{"tool": "pipeline_conversion", "args": {"detail": "slipped"}}]) == ["Slipped deal"]
    assert terms_for([{"tool": "pipeline_conversion", "args": {}}], "win rate of deals") == ["Slipped deal", "Win rate"]
    assert terms_for([{"tool": "get_kpi", "args": {"metric": "raroc"}}]) == ["RAROC", "Hurdle rate"]


def test_mentions_and_no_data_handle_gpt_oss_spacing():
    # gpt-oss writes non-breaking spaces and hyphens; the run on Nov 25 failed these on visible matches
    assert scorers.mentions("Coverage East 2 is over capacity; a Severity 1 break.", ["Coverage East 2", "Severity 1"])["value"] == 1.0
    real = ("**Headline:** No Industrials revenue data available for Q3 2027; data current to 2026‑11‑24.\n"
            "The system’s latest available data is through 24 Nov 2026. Since Q3 2027 falls after this cut‑off, "
            "Industrials revenue for that quarter cannot be reported at this time.")
    assert scorers.guardrail("answered", real, "no_data")["value"] is True


def test_retrieval_terms_follow_the_question():
    from agent.retriever import terms_for
    cap = [{"tool": "coverage_capacity", "args": {"team": "ALL"}}]
    assert terms_for(cap, "Which coverage teams are over capacity?") == ["Coverage capacity"]
    assert terms_for(cap, "How has Coverage East 2's win rate trended over the last four quarters?") == ["Win rate"]
    assert terms_for(cap, "How many clients and bankers does Coverage East 2 have?") == ["Coverage capacity"]
    assert terms_for(cap, "Tell me about Coverage East 2") == ["Coverage capacity"]
    ws = [{"tool": "cross_sell_whitespace", "args": {}}]
    assert terms_for(ws, "What was the average RAROC?") == ["RAROC", "Hurdle rate"]
    assert "Cross-sell whitespace" in terms_for(ws, "Which clients have cross-sell whitespace?")
