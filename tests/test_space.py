"""The Space's logic (core.py, fallback.py) without Gradio, Databricks or an LLM."""
import json
import sys
import time
from pathlib import Path

import pytest

SPACE = Path(__file__).resolve().parents[1] / "space"
sys.path.insert(0, str(SPACE))

import core  # noqa: E402
import fallback  # noqa: E402

WORK = {"question": "q", "outcome": "answered", "answer": "**Headline:** Industrials missed plan by $8.8M.",
        "as_of": "2026-11-24", "models": ["system.ai.qwen35-122b-a10b", "system.ai.gpt-oss-120b"], "latency_ms": 12000,
        "tool_calls": [{"name": "get_kpi", "params": {"metric": "revenue", "period": "2026-Q3", "segment": "Industrials"},
                        "sql": "SELECT * FROM cib_demo.gold.get_kpi(:metric, :period, :segment)", "row_count": 1,
                        "rows_preview": [{"value": 37900000.0, "plan": 46700000.0, "variance_vs_plan": -8800000.0}],
                        "error": None, "seconds": 1.2}],
        "retrieved": [{"doc": "Glossary", "doc_id": "glossary", "section": "Revenue vs Plan", "score": 0.71}],
        "retrieval_mode": "vector search",
        "number_check": {"passed": True, "unmatched": [], "retries": 0, "checked": 3,
                         "attempts": [{"passed": True, "unmatched": []}]}}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"built_at": "2026-11-25T14:00:00", "as_of": "2026-11-24", "questions": [
        {"question": core.SUGGESTED[0], "answer": "**Headline:** recorded answer", "work": WORK,
         "recorded_at": "2026-11-25T14:00:00"}]}))
    monkeypatch.setattr(fallback, "CACHE", path)
    monkeypatch.delenv("FORCE_CACHE", raising=False)
    return path


def free_cap():
    return core.HourlyCap(100)


def test_suggested_questions_match_build_cache():
    src = (SPACE / "build_cache.py").read_text()
    for q in core.SUGGESTED:
        assert f'"{q}"' in src


def test_clean_question_strips_urls_code_and_checks_length():
    assert core.clean_question("Why did Industrials miss? see https://x.io/a `rm -rf` <b>hi</b>")[0] == "Why did Industrials miss? see hi"
    assert core.clean_question("x" * 301) == ("", "too_long")
    assert core.clean_question("  https://evil.example  ")[1] == "empty"
    assert core.clean_question("What is RAROC?") == ("What is RAROC?", None)


def test_live_answer_and_show_your_work(cache):
    r = core.respond("Why did Industrials revenue miss plan in Q3?", 0, run_fn=lambda q: WORK, cap=free_cap())
    assert (r.mode, r.used) == ("live", 1) and r.answer == WORK["answer"]
    assert "get_kpi(metric='revenue'" in r.work and "```sql" in r.work and "Revenue vs Plan" in r.work
    assert "number check:** passed" in r.work and "Northlake Bank is fictional" in r.work


def test_session_limit(cache):
    r = core.respond("What is RAROC?", core.SESSION_LIMIT, run_fn=lambda q: WORK, cap=free_cap())
    assert r.mode == "limited" and r.used == core.SESSION_LIMIT and "limit" in r.answer


def test_auth_error_falls_back_to_the_recording(cache):
    def boom(q):
        raise RuntimeError("Model qwen35-122b-a10b failed on every route. gateway: Error code: 401 - invalid token")
    r = core.respond(core.SUGGESTED[0].lower() + "  ", 0, run_fn=boom, cap=free_cap())
    assert r.mode == "cached" and "Live backend unavailable, showing a recorded answer from Nov 25, 2026" in r.answer
    assert "Recorded run" in r.work
    r = core.respond("How did Energy do in Q2?", 1, run_fn=boom, cap=free_cap())
    assert r.mode == "off" and "Live mode is off" in r.answer and r.used == 2


def test_warehouse_errors_inside_a_finished_run_fall_back(cache):
    bad = dict(WORK, tool_calls=[dict(WORK["tool_calls"][0], error="query failed: Error during request to server: 403 Forbidden")])
    assert core.respond(core.SUGGESTED[0], 0, run_fn=lambda q: bad, cap=free_cap()).mode == "cached"


def test_other_errors_are_not_mistaken_for_an_outage(cache):
    def bug(q):
        raise KeyError("answer")
    r = core.respond(core.SUGGESTED[0], 0, run_fn=bug, cap=free_cap())
    assert r.mode == "error" and "Traceback" not in r.answer


def test_timeout_and_force_cache(cache, monkeypatch):
    r = core.respond(core.SUGGESTED[0], 0, run_fn=lambda q: time.sleep(1) or WORK, cap=free_cap(), timeout_s=0.1)
    assert r.mode == "cached"
    r = core.respond("Something else", 0, run_fn=lambda q: time.sleep(1) or WORK, cap=free_cap(), timeout_s=0.1)
    assert r.mode == "off" and "too long" in r.answer
    monkeypatch.setenv("FORCE_CACHE", "1")
    called = []
    r = core.respond(core.SUGGESTED[0], 0, run_fn=lambda q: called.append(q) or WORK, cap=free_cap())
    assert r.mode == "cached" and not called


def test_hourly_cap(cache):
    cap = core.HourlyCap(2, window_s=60)
    assert cap.take(0) and cap.take(1) and not cap.take(2) and cap.take(61)
    full = core.HourlyCap(0)
    assert core.respond(core.SUGGESTED[0], 0, run_fn=lambda q: WORK, cap=full).mode == "cached"
    assert core.respond("What is RAROC?", 0, run_fn=lambda q: WORK, cap=full).mode == "limited"


def test_no_cache_file_still_answers_politely(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "CACHE", tmp_path / "missing.json")
    monkeypatch.setenv("FORCE_CACHE", "1")
    assert core.respond(core.SUGGESTED[0], 0, run_fn=lambda q: WORK, cap=free_cap()).mode == "off"
