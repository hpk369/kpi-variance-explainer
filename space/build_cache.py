"""Builds space/cache.json: recorded answers to the Space's 4 suggested questions, shown when the live backend is down.

Each question is answered by the live agent and saved only if it passes the same checks as the evaluation
(answered, number check passed, every expected figure from expected_answers.json present, groundedness judge).
A failing answer is retried up to 2 times; if it still fails the script stops and nothing is written.

Run: python space/build_cache.py            (about 3 minutes)
     python space/build_cache.py --check    (print what's in cache.json)
     python space/build_cache.py --no-judge (skip the groundedness judge)
     python space/build_cache.py --only 2   (re-record question 2 only; the others are kept from cache.json)
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import scorers  # noqa: E402
from eval.evaluation import load_set  # noqa: E402

CACHE = Path(__file__).resolve().parent / "cache.json"

# Suggested question → the eval row whose expected figures it must contain (plus extra figures for this wording)
SUGGESTED = [
    ("Why did Industrials revenue miss plan in Q3?", "s1_why_miss", []),
    ("Which clients are below the RAROC hurdle but have cross-sell whitespace?", None,
     [{"label": "lending-only Mid-Market clients below hurdle", "value": 18, "kind": "count"},
      {"label": "hurdle rate", "value": 0.15, "kind": "rate"}]),
    ("Which coverage teams are over capacity, and how is their win rate trending?", "s3_over_capacity",
     [{"label": "win rate 2025-Q4", "value": 0.4375, "kind": "rate"}, {"label": "win rate 2026-Q3", "value": 0.25, "kind": "rate"}]),
    ("Are there any reconciliation breaks?", "s4_recon", []),
]


def passes(work: dict, expected: list[dict], question: str, judge: bool = True) -> tuple[bool, str]:
    if work.get("outcome") != "answered":
        return False, f"outcome {work.get('outcome')}"
    if not work["number_check"]["passed"]:
        return False, f"number check failed: {work['number_check']['unmatched']}"
    nm = scorers.number_match(work["answer"], expected)
    if nm and nm["value"] < 1:
        return False, nm["rationale"]
    if judge:   # a recorded answer is shown to everyone when the backend is down: it must be fully grounded
        g = scorers.groundedness(question, work["answer"], work["outcome"], work.get("evidence", {}))
        if g and g["value"] is not True:
            return False, f"groundedness: {g['rationale']}"
    return True, "ok"


def build(judge: bool = True, only: int | None = None) -> int:
    from agent.graph import run

    rows = {r["id"]: r for r in load_set()}
    old = json.loads(CACHE.read_text())["questions"] if only and CACHE.exists() else []
    if only and len(old) != len(SUGGESTED):
        print("--only needs a complete cache.json: run python space/build_cache.py first")
        return 1
    entries = []
    for n, (question, row_id, extra) in enumerate(SUGGESTED, 1):
        if only and n != only:
            entries.append(old[n - 1])
            print(f"KEPT  {question}")
            continue
        expected = (rows[row_id]["expected_numbers"] if row_id else []) + extra
        for attempt in (1, 2, 3):
            work = run(question)
            ok, why = passes(work, expected, question, judge)
            print(f"{'PASS' if ok else 'FAIL'}  {question}  (attempt {attempt}, {work['latency_ms'] / 1000:.0f}s) {'' if ok else why}")
            if ok:
                break
        else:
            print("\nStopped: an answer failed 3 times, cache.json not written. Send Claude the line above.")
            return 1
        entries.append({"question": question, "answer": work["answer"],
                        "work": {k: v for k, v in work.items() if k not in ("evidence", "answer")},
                        "recorded_at": dt.datetime.now().isoformat(timespec="seconds")})
    payload = {"built_at": dt.datetime.now().isoformat(timespec="seconds"),
               "as_of": next((e["work"].get("as_of") for e in entries if e["work"].get("as_of")), None),
               "note": "Recorded answers from the live agent (Northlake Bank, fictional, synthetic data).",
               "questions": entries}
    CACHE.write_text(json.dumps(payload, indent=1, default=str))
    print(f"\nWrote {CACHE} with {len(entries)} answers (data as of {payload['as_of']}).")
    return 0


def check() -> int:
    if not CACHE.exists():
        print("No cache.json yet: run python space/build_cache.py")
        return 1
    payload = json.loads(CACHE.read_text())
    print(f"Built {payload['built_at']} · data as of {payload['as_of']}")
    for e in payload["questions"]:
        print(f"- {e['question']}  [{e['work']['outcome']}, number check {e['work']['number_check']['passed']}]")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="show what's in cache.json")
    ap.add_argument("--no-judge", action="store_true", help="skip the groundedness judge")
    ap.add_argument("--only", type=int, choices=range(1, len(SUGGESTED) + 1), help="re-record one question (1-4)")
    a = ap.parse_args()
    sys.exit(check() if a.check else build(judge=not a.no_judge, only=a.only))
