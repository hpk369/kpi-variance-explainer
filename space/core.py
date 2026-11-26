"""Everything the Space does apart from drawing the page: input checks, rate limits, the live call, the cached
fallback and the "show your work" panel. No Gradio here, so it can be unit-tested.

    from core import respond
    r = respond("Why did Industrials revenue miss plan in Q3?", used=0)
    r.answer, r.work, r.used, r.mode          # mode: live | cached | limited | rejected | off | error

Settings (Space secrets or variables): FORCE_CACHE=1 (recorded answers only), LIVE_TIMEOUT_S (default 45),
SESSION_LIMIT (default 5), HOURLY_CAP (default 30 questions an hour across all visitors).
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent):           # the Space keeps agent/ next to app.py; the repo keeps it one level up
    if (p / "agent").is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import fallback  # noqa: E402

SUGGESTED = [
    "Why did Industrials revenue miss plan in Q3?",
    "Which clients are below the RAROC hurdle but have cross-sell whitespace?",
    "Which coverage teams are over capacity, and how is their win rate trending?",
    "Are there any reconciliation breaks?",
]
MAX_CHARS = 300


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SESSION_LIMIT = _int_env("SESSION_LIMIT", 5)
HOURLY_CAP = _int_env("HOURLY_CAP", 30)
LIVE_TIMEOUT_S = _int_env("LIVE_TIMEOUT_S", 45)

SYNTHETIC = "_Northlake Bank is fictional. Every client, deal and figure in this demo is synthetic._"
MSG = {
    "empty": "Type a question about Northlake's performance, or pick one of the suggested questions.",
    "too_long": f"Please keep the question under {MAX_CHARS} characters.",
    "session": ("That's the {n}-question limit for one visit: the demo runs on a small free-tier Databricks "
                "workspace. The code, the build guide and the evaluation scorecard are on GitHub."),
    "hourly": "The live agent has had a lot of questions in the last hour. Please try again in a few minutes.",
    "off": ("**Live mode is off right now.** Only the four suggested questions can be answered, from recorded "
            "answers. Try one of the buttons above."),
    "slow": "**The live agent took too long to answer.** Please try again in a minute, or pick a suggested question.",
    "error": "**Something went wrong while answering.** Please try again, or pick a suggested question.",
}


@dataclass
class Reply:
    answer: str
    work: str
    used: int
    mode: str          # live | cached | limited | rejected | off | error
    detail: str = ""   # for the Space logs only


# ---------------------------------------------------------------- input and limits

_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_CODE = re.compile(r"```.*?(?:```|$)|`[^`]*`|<[^>]{1,200}>", re.S)


def clean_question(text: str) -> tuple[str, str | None]:
    """Returns (question, problem). Strips URLs, code and markup; rejects empty or over-long input."""
    raw = (text or "").strip()
    if len(raw) > MAX_CHARS:
        return "", "too_long"
    q = _URL.sub(" ", raw)
    q = _CODE.sub(" ", q)
    q = re.sub(r"[\x00-\x1f\x7f]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return (q, None) if len(re.sub(r"[^A-Za-z]", "", q)) >= 3 else ("", "empty")


class HourlyCap:
    """At most `cap` live questions in any 60 minutes, across all visitors (in memory; resets when the Space restarts)."""

    def __init__(self, cap: int, window_s: int = 3600):
        self.cap, self.window, self.hits, self.lock = cap, window_s, deque(), threading.Lock()

    def take(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            while self.hits and now - self.hits[0] >= self.window:
                self.hits.popleft()
            if len(self.hits) >= self.cap:
                return False
            self.hits.append(now)
            return True


HOURLY = HourlyCap(HOURLY_CAP)
_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")


def _agent_run(question: str) -> dict:
    from agent.graph import run   # imported on first use, so the page loads even if the backend is broken

    return run(question)


# ---------------------------------------------------------------- the answer

def respond(text: str, used: int, run_fn=None, cap: HourlyCap | None = None, timeout_s: float | None = None) -> Reply:
    run_fn = run_fn or _agent_run
    cap = cap or HOURLY
    timeout_s = LIVE_TIMEOUT_S if timeout_s is None else timeout_s

    question, problem = clean_question(text)
    if problem:
        return Reply(MSG[problem], "", used, "rejected", problem)
    if used >= SESSION_LIMIT:
        return Reply(MSG["session"].format(n=SESSION_LIMIT), "", used, "limited", "session")
    used += 1

    if fallback.force_cache():
        return _cached_or_off(question, used, "FORCE_CACHE is on")
    if not cap.take():
        entry = fallback.lookup(question)
        if entry:
            return _cached(entry, used, "the hourly limit is reached")
        return Reply(MSG["hourly"], "", used, "limited", "hourly")

    t0 = time.time()
    future = _POOL.submit(run_fn, question)
    try:
        work = future.result(timeout=timeout_s)
    except cf.TimeoutError:
        return _cached_or_off(question, used, f"no answer within {timeout_s:.0f}s", MSG["slow"])
    except Exception as ex:   # noqa: BLE001 - every failure becomes a message, never a stack trace
        err = f"{type(ex).__name__}: {ex}"
        if fallback.backend_down(err):
            return _cached_or_off(question, used, err[:300])
        return Reply(MSG["error"], "", used, "error", err[:300])

    err = fallback.work_backend_error(work)
    if err:
        return _cached_or_off(question, used, err[:300])
    return Reply(work.get("answer") or MSG["error"], render_work(work), used, "live",
                 f"{work.get('outcome')} in {time.time() - t0:.1f}s")


def _cached(entry: dict, used: int, why: str) -> Reply:
    note = (f"> **Live backend unavailable, showing a recorded answer from {fallback.recorded_on(entry)}.** "
            "It passed the same checks as the evaluation when it was recorded.\n\n")
    return Reply(note + entry["answer"], render_work(entry.get("work") or {}, cached=True), used, "cached", why)


def _cached_or_off(question: str, used: int, why: str, off_message: str | None = None) -> Reply:
    entry = fallback.lookup(question)
    if entry:
        return _cached(entry, used, why)
    return Reply(off_message or MSG["off"], "", used, "off", why)


def remaining_text(used: int) -> str:
    left = max(0, SESSION_LIMIT - used)
    return f"Questions left this visit: **{left} of {SESSION_LIMIT}**"


# ---------------------------------------------------------------- show your work

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        v = f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.2f}"
    elif isinstance(v, list):
        v = ", ".join(map(str, v))
    return str(v).replace("|", "/").replace("\n", " ")[:60]


def _table(rows: list[dict], max_cols: int = 7) -> str:
    if not rows:
        return "_no rows_"
    cols = [c for c in rows[0] if c not in ("error",)][:max_cols]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def render_work(work: dict, cached: bool = False) -> str:
    if not work:
        return ""
    out = []
    if cached:
        out.append("_Recorded run: these are the tool calls and checks from when this answer was recorded._\n")
    nc = work.get("number_check") or {}
    outcome = work.get("outcome", "")
    status = "passed" if nc.get("passed") else ("blocked" if outcome == "blocked" else "not run")
    secs = (work.get("latency_ms") or 0) / 1000
    out.append(f"**Outcome:** {outcome} · **number check:** {status} ({nc.get('checked', 0)} figures, "
               f"{nc.get('retries', 0)} retries) · **{secs:.0f}s** · data as of {work.get('as_of') or '–'}")
    if work.get("models"):
        out.append(f"**Models:** {', '.join(work['models'])}")

    calls = work.get("tool_calls") or []
    out.append(f"\n### Governed tools ({len(calls)})" if calls else "\n### Governed tools\nNone called.")
    for i, c in enumerate(calls, 1):
        args = ", ".join(f"{k}={v!r}" for k, v in (c.get("params") or {}).items())
        n = c.get("row_count") or 0
        head = f"**{i}. `{c.get('name')}({args})`** → {n} row{'' if n == 1 else 's'}"
        head += f" in {c['seconds']}s" if c.get("seconds") is not None else ""
        out.append(head)
        if c.get("error"):
            out.append(f"> Tool said: {c['error'][:300]}")
        if c.get("sql"):
            out.append(f"```sql\n{c['sql']}\n```")
        if c.get("rows_preview"):
            out.append(_table(c["rows_preview"]))
            if (c.get("row_count") or 0) > len(c["rows_preview"]):
                out.append(f"_first {len(c['rows_preview'])} of {c['row_count']} rows_")

    docs = work.get("retrieved") or []
    mode = work.get("retrieval_mode")
    out.append("\n### Definitions retrieved" + (f" ({mode})" if mode else ""))
    if docs:
        for d in docs:
            score = f" · score {d['score']:.3f}" if isinstance(d.get("score"), (int, float)) else ""
            out.append(f"- {d.get('doc')} › {d.get('section')}{score}")
    else:
        out.append("None.")

    attempts = nc.get("attempts") or []
    if attempts:
        out.append("\n### Number check")
        out.append("Every figure in the draft must match a tool result (allowing for display rounding). "
                   "A mismatch sends the draft back; after 2 retries the commentary is withheld.")
        for i, a in enumerate(attempts, 1):
            bad = a.get("unmatched") or []
            out.append(f"- Draft {i}: {'pass' if a.get('passed') else 'fail'}"
                       + (f" · unmatched: {', '.join(map(str, bad))}" if bad else ""))
    out.append("\n" + SYNTHETIC)
    return "\n\n".join(out)
