"""Ask the agent from the terminal.

    python -m agent.cli "Why did Industrials revenue miss plan in Q3?"
    python -m agent.cli "Why did Industrials revenue miss plan in Q3?" --show-work
    python -m agent.cli --inject-wrong-number "Why did Industrials revenue miss plan in Q3?"   # blocked-answer demo
    python -m agent.cli --demo                     # the six Gate 6 questions, one after another
    python -m agent.cli "..." --trace              # also send an MLflow trace to Databricks
    python -m agent.cli "..." --json               # the full payload as JSON

Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO = [
    "Why did Industrials revenue miss plan in Q3?",
    "Which clients are below the RAROC hurdle but have cross-sell whitespace?",
    "Which coverage teams are over capacity, and how is their win rate trending?",
    "Are there any reconciliation breaks?",
    "How is revenue tracking against plan this month?",
    "What's TD's real Q3 revenue?",
]


def show_work(w: dict) -> str:
    out = ["", "─" * 72, "SHOW YOUR WORK", "─" * 72]
    for i, c in enumerate(w["tool_calls"], 1):
        out.append(f"[tool {i}] {c['name']}({', '.join(f'{k}={v!r}' for k, v in c['params'].items())})"
                   f"  → {c['row_count']} rows in {c['seconds']}s" + (f"  ERROR: {c['error']}" if c["error"] else ""))
        if c["sql"]:
            out.append(f"         {c['sql']}")
        for r in c["rows_preview"][:3]:
            out.append("         · " + ", ".join(f"{k}={v}" for k, v in list(r.items())[:7] if v is not None))
    if not w["tool_calls"]:
        out.append("[tools] none called")
    out.append(f"[retrieval] {w['retrieval_mode'] or 'skipped'}" + (f"  ({w['retrieval_note']})" if w.get("retrieval_note") else ""))
    for r in w["retrieved"]:
        score = f"{r['score']:.3f}" if isinstance(r.get("score"), (int, float)) else r.get("score")
        out.append(f"         {r['doc']} › {r['section']}  score={score}")
    nc = w["number_check"]
    status = "passed" if nc["passed"] else ("blocked" if w["outcome"] == "blocked" else "not run")
    out.append(f"[number_check] {status} · {nc['checked']} figures checked · retries {nc['retries']}")
    for i, a in enumerate(nc["attempts"], 1):
        out.append(f"         draft {i}: {'pass' if a['passed'] else 'fail'}" + (f" · unmatched {a['unmatched']}" if a["unmatched"] else ""))
    cc = w.get("claim_check") or {}
    for r in cc.get("runs", []):
        state = "error: " + r["error"] if r.get("error") else ("pass" if not r["unsupported"] else
                                                               "fail · " + "; ".join(x["claim"] for x in r["unsupported"]))
        out.append(f"[claim_check {r['attempt']}] {r.get('model')} {state}")
    for d in w.get("draft_diagnostics", []):
        calls = "; ".join(f"{c['model']} max_tokens={c['max_tokens']} " +
                          (f"error {c['error']}" if c.get("error") else
                           f"finish={c['finish_reason']} chars={c['chars']} tokens={c['completion_tokens']}")
                          for c in d["calls"])
        out.append(f"[draft {d['attempt']}] {calls}")
    if w["outcome"] == "answered" and not w["answer"].strip():
        out.append("[warning] empty answer")
    out.append(f"[outcome] {w['outcome']} · models {', '.join(w['models']) or '-'} · {w['latency_ms'] / 1000:.1f}s"
               + (f" · trace {w['trace_id']}" if w.get("trace_id") else ""))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask the Northlake insights agent (synthetic data).")
    ap.add_argument("question", nargs="?", help="the question, in quotes")
    ap.add_argument("--show-work", action="store_true", help="print tool calls, SQL, retrieved definitions and the number check")
    ap.add_argument("--inject-wrong-number", action="store_true", help="corrupt a figure in every draft (blocked-answer demo)")
    ap.add_argument("--demo", action="store_true", help="run the six Gate 6 questions")
    ap.add_argument("--no-vector-search", action="store_true", help="use local keyword search instead of the index")
    ap.add_argument("--trace", action="store_true", help="send MLflow traces to Databricks")
    ap.add_argument("--json", action="store_true", help="print the full payload as JSON")
    args = ap.parse_args()
    if not args.question and not args.demo:
        ap.error("give a question in quotes, or --demo")

    from agent import tracing

    if args.trace:
        print(f"Tracing to Databricks experiment {tracing.setup_databricks()}", file=sys.stderr)
        tracing.autolog_llm_calls()
    from agent.graph import run

    questions = DEMO if args.demo else [args.question]
    for q in questions:
        w = run(q, inject_wrong_number=args.inject_wrong_number, use_vector_search=not args.no_vector_search)
        if args.json:
            print(json.dumps(w, indent=2, default=str))
            continue
        print("=" * 72 + f"\nQ: {q}\n" + "=" * 72)
        print(w["answer"])
        if args.show_work or args.demo:
            print(show_work(w))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
