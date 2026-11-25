"""Runs the agent over eval/eval_set.jsonl, scores every answer and builds the scorecard.

On the Mac (quick, no MLflow):
    python -m eval.evaluation                     # all 15, with the LLM judges
    python -m eval.evaluation --ids s1_why_miss,g_future --no-judges
    python -m eval.evaluation --limit 3           # pilot: first 3 rows
In Databricks: notebook eval/run_eval (same scorers through mlflow.genai.evaluate, logged to an MLflow experiment).

Writes eval/results/run_<time>.json (every answer, its "show your work" and every score), eval/results/latest.json
and eval/results/scorecard.md. Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import scorers  # noqa: E402

HERE = Path(__file__).resolve().parent
EVAL_SET = HERE / "eval_set.jsonl"
RESULTS = HERE / "results"
TARGETS = {"number_accuracy": 1.0, "tool_selection": 0.95, "groundedness": 0.90, "retrieval_relevance": 0.85}


def load_set(path: Path = EVAL_SET, ids: list[str] | None = None, limit: int | None = None) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if ids:
        rows = [r for r in rows if r["id"] in ids]
    return rows[:limit] if limit else rows


def _share(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(1 for v in vals if v is True or v == 1 or v == 1.0) / len(vals), 4) if vals else None


def _mean(vals: list) -> float | None:
    vals = [float(v) for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def scorecard(results: list[dict]) -> dict:
    def vals(name, group_filter=None):
        out = []
        for r in results:
            s = r["scores"].get(name)
            if s is None or (group_filter and not group_filter(r)):
                continue
            out.append(s["value"])
        return out

    story = lambda r: r["group"] in ("story1", "story2", "story3")  # noqa: E731
    nm = vals("number_match")
    card = {
        "rows": len(results),
        "number_accuracy": _share([v == 1.0 for v in nm]) if nm else None,
        "number_accuracy_stories_1_3": _share([v == 1.0 for v in vals("number_match", story)]),
        "tool_selection": _share(vals("tool_selection")),
        "groundedness": _share(vals("groundedness")),
        "retrieval_relevance": _mean(vals("retrieval_relevance")),
        "guardrails_passed": sum(1 for v in vals("guardrail") if v is True),
        "guardrails_total": len(vals("guardrail")),
        "expected_docs": _share(vals("expected_docs")),
        "mentions": _mean(vals("mentions")),
        "judge_errors": sum(1 for r in results for s in r["scores"].values() if s and s.get("error")),
        "agent_errors": sum(1 for r in results if r.get("agent_error")),
        "blocked": sum(1 for r in results if r.get("outcome") == "blocked"),
        "avg_latency_s": _mean([(r.get("latency_ms") or 0) / 1000 for r in results]),
    }
    return card


def _pct(v):
    return "–" if v is None else f"{v:.0%}"


def scorecard_md(card: dict, results: list[dict], meta: dict) -> str:
    def ok(name, v):
        return "" if v is None else (" ✅" if v >= TARGETS[name] else " ❌")
    lines = [f"# Evaluation scorecard · {meta['run_at']}", "",
             f"Northlake Bank (fictional), synthetic data. Planner `{meta['model']}`, writer `{meta.get('writer')}`, "
             f"judge `{meta['judge']}`, "
             f"{card['rows']} questions, average {card['avg_latency_s']}s per answer.", "",
             "| Metric | Target | Result |", "|---|---|---|",
             f"| Number accuracy | 100% | {_pct(card['number_accuracy'])}{ok('number_accuracy', card['number_accuracy'])} |",
             f"| Number accuracy, stories 1–3 (Gate 7) | 100% | {_pct(card['number_accuracy_stories_1_3'])}"
             f"{ok('number_accuracy', card['number_accuracy_stories_1_3'])} |",
             f"| Tool selection | ≥ 95% | {_pct(card['tool_selection'])}{ok('tool_selection', card['tool_selection'])} |",
             f"| Groundedness (LLM judge) | ≥ 90% | {_pct(card['groundedness'])}{ok('groundedness', card['groundedness'])} |",
             f"| Retrieval relevance (LLM judge) | ≥ 85% | {_pct(card['retrieval_relevance'])}"
             f"{ok('retrieval_relevance', card['retrieval_relevance'])} |",
             f"| Guardrails | 2/2 | {card['guardrails_passed']}/{card['guardrails_total']}"
             f"{'' if not card['guardrails_total'] else ' ✅' if card['guardrails_passed'] == card['guardrails_total'] else ' ❌'} |",
             f"| Expected documents retrieved | – | {_pct(card['expected_docs'])} |",
             f"| Key names mentioned | – | {_pct(card['mentions'])} |", "",
             f"Judge errors: {card['judge_errors']} · agent errors: {card['agent_errors']} · blocked answers: {card['blocked']}", "",
             "## Rows", "", "| id | outcome | numbers | tools | grounded | relevance | notes |", "|---|---|---|---|---|---|---|"]

    def cell(s):
        if s is None:
            return "–"
        v = s["value"]
        return "err" if v is None else ("✓" if v is True else "✗" if v is False else _pct(v))
    for r in results:
        notes = [s["rationale"] for k, s in r["scores"].items()
                 if s and (s["value"] is False or s.get("error") or (isinstance(s["value"], float) and s["value"] < 1))]
        lines.append(f"| {r['id']} | {r['outcome']} | {cell(r['scores'].get('number_match'))} | "
                     f"{cell(r['scores'].get('tool_selection'))} | {cell(r['scores'].get('groundedness'))} | "
                     f"{cell(r['scores'].get('retrieval_relevance'))} | {' / '.join(notes)[:300].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def record(row: dict, work: dict | None, scores: dict, error: str | None = None) -> dict:
    work = work or {}
    return {"id": row["id"], "group": row["group"], "question": row["question"], "outcome": work.get("outcome", "error"),
            "answer": work.get("answer", ""), "latency_ms": work.get("latency_ms"), "scores": scores,
            "agent_error": error, "work": work}


def save(results: list[dict], meta: dict) -> dict:
    RESULTS.mkdir(exist_ok=True)
    card = scorecard(results)
    payload = {**meta, "scorecard": card, "results": results}
    stamp = meta["run_at"].replace(":", "").replace("-", "").replace(" ", "_")
    for p in (RESULTS / f"run_{stamp}.json", RESULTS / "latest.json"):
        p.write_text(json.dumps(payload, indent=1, default=str))
    (RESULTS / "scorecard.md").write_text(scorecard_md(card, results, meta))
    return payload


def meta_now() -> dict:
    from common import config

    from agent.graph import draft_models

    return {"run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "model": config.model("llm"), "writer": draft_models()[0],
            "judge": scorers.judge_model(), "constants_version": config.constants()["version"]}


def run_local(rows: list[dict], judges: bool = True) -> dict:
    from agent.graph import run

    results = []
    for i, row in enumerate(rows, 1):
        t0 = time.time()
        try:
            work, err = run(row["question"]), None
        except Exception as ex:  # keep going: an agent error is a scored failure, not a crash
            work, err = None, f"{type(ex).__name__}: {str(ex)[:300]}"
        scores = scorers.score(row, work or {"answer": "", "outcome": "error"}, judges=judges)
        results.append(record(row, work, scores, err))
        flags = " ".join(f"{k}={'–' if s is None else s['value']}" for k, s in scores.items())
        print(f"[{i}/{len(rows)}] {row['id']:20} {results[-1]['outcome']:9} {time.time() - t0:5.0f}s  {flags}", flush=True)
    return save(results, meta_now())


def run_mlflow(rows: list[dict], experiment: str, judges: bool = True) -> dict:
    """Same scorers through mlflow.genai.evaluate (traces and per-row assessments in the MLflow UI)."""
    import mlflow
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import scorer

    from agent.graph import run

    by_id = {r["id"]: r for r in rows}
    outs: dict[str, dict] = {}
    scores: dict[str, dict] = {r["id"]: {} for r in rows}

    def predict_fn(question: str, id: str) -> dict:  # noqa: A002  (mlflow passes the inputs by name)
        work = run(question)
        outs[id] = work
        return {k: work.get(k) for k in ("answer", "outcome", "tool_calls", "retrieved", "number_check", "latency_ms")}

    def make(name: str):
        @scorer(name=name)
        def _s(inputs, outputs, expectations):
            row, work = by_id[inputs["id"]], outs.get(inputs["id"]) or {"answer": "", "outcome": "error"}
            s = scorers.score(row, work, only=name)[name]
            scores[inputs["id"]][name] = s
            if s is None:
                return None
            if s.get("error"):
                return Feedback(error=s["rationale"])
            return Feedback(value=s["value"], rationale=s["rationale"])
        return _s

    names = list(scorers.CODE_SCORERS) + (list(scorers.JUDGES) if judges else [])
    data = [{"inputs": {"question": r["question"], "id": r["id"]},
             "expectations": {k: r[k] for k in ("expected_numbers", "expected_tools", "expected_docs",
                                                "expected_mentions", "expected_behaviour")}} for r in rows]
    mlflow.set_experiment(experiment)
    result = mlflow.genai.evaluate(data=data, predict_fn=predict_fn, scorers=[make(n) for n in names])

    results = [record(r, outs.get(r["id"]), scores[r["id"]], None if r["id"] in outs else "no output") for r in rows]
    payload = save(results, meta_now())
    card = payload["scorecard"]
    with mlflow.start_run(run_id=result.run_id):
        mlflow.log_metrics({f"scorecard/{k}": float(v) for k, v in card.items() if isinstance(v, (int, float))})
        mlflow.log_text((RESULTS / "scorecard.md").read_text(), "scorecard.md")
        mlflow.log_dict({k: v for k, v in payload.items() if k != "results"}, "scorecard.json")
    payload["mlflow_run_id"] = result.run_id
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate the agent on eval/eval_set.jsonl")
    ap.add_argument("--ids", help="comma-separated row ids")
    ap.add_argument("--limit", type=int, help="first N rows only (pilot)")
    ap.add_argument("--no-judges", action="store_true", help="skip the LLM judges (faster, no judge calls)")
    args = ap.parse_args()
    rows = load_set(ids=args.ids.split(",") if args.ids else None, limit=args.limit)
    payload = run_local(rows, judges=not args.no_judges)
    print("\n" + (RESULTS / "scorecard.md").read_text())
    card = payload["scorecard"]
    return 0 if card["number_accuracy_stories_1_3"] in (None, 1.0) else 1


if __name__ == "__main__":
    sys.exit(main())
