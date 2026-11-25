"""The CIB insights agent as a LangGraph graph.

    guard ─► plan ⇄ tools ─► retrieve ─► draft ─► number_check ─► claim_check ─► answer
      │        │                            ▲          │                │
      └────────┴─► refuse                   └─ retry ──┴────────────────┤ (up to 2 retries)
                                                                        └─► blocked (tool tables only)

    from agent.graph import run
    work = run("Why did Industrials revenue miss plan in Q3?")
    print(work["answer"]); work["tool_calls"], work["retrieved"], work["number_check"]

Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from agent import number_check, prompts, retriever, summaries, tools, tracing  # noqa: E402
from common import config  # noqa: E402

MAX_ROUNDS = 4          # plan → tools rounds
MAX_RETRIES = 2         # extra drafts after a failed number check or claim check
CLAIM_CHECK = os.environ.get("CLAIM_CHECK", "1") != "0"   # CLAIM_CHECK=0 turns the claim check off
ROWS_TO_LLM = 60        # rows per tool result shown to the drafting model
PREVIEW_ROWS = 5        # rows per tool result in "show your work"

_REAL_BANK = re.compile(r"\bTD\b|\bToronto[- ]Dominion\b|\bRBC\b|\bBMO\b|\bCIBC\b", re.UNICODE)
_REAL_BANK_I = re.compile(r"\b(royal bank|bank of montreal|scotiabank|national bank of canada|jp ?morgan|goldman sachs|"
                          r"morgan stanley|citi(?:group|bank)?|bank of america|wells fargo|hsbc|barclays)\b", re.I)
_REAL_DATA = re.compile(r"\breal\b(?!\s+estate)[^?.]{0,40}\b(revenue|earnings|profit|numbers|figures|data|results)\b", re.I)


class State(TypedDict, total=False):
    question: str
    options: dict
    messages: list
    pending: list
    tool_calls: list
    rounds: int
    nudged: bool
    status: str          # planning | ready | out_of_scope
    reason: str
    retrieved: dict
    as_of: str
    draft: str
    draft_messages: list
    attempts: int
    checks: list
    answer: str
    outcome: str         # answered | blocked | refused
    models: list
    draft_diag: list
    claims: list


# ---------------------------------------------------------------- LLM

def _text(content: Any) -> str:
    if isinstance(content, list):  # some models return content parts (reasoning + text)
        content = "".join(p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") or ""
                          for p in content if (p.get("type") if isinstance(p, dict) else getattr(p, "type", "")) in ("text", "output_text"))
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    return content.strip()


def llm(messages: list[dict], **kwargs) -> tuple[Any, str]:
    """Chat completion on the main model, then the fallback model. Returns (message, model used)."""
    from common import dbx

    errors = []
    for model in dict.fromkeys(m for m in (config.model("llm"), config.model("llm_fallback")) if m):
        try:
            return dbx.chat(messages, model=model, **kwargs).choices[0].message, model
        except Exception as ex:
            errors.append(f"{model}: {str(ex)[:200]}")
    raise RuntimeError("Every model failed. " + " | ".join(errors))


DRAFT_TOKENS = (10000, 16000)   # reasoning models spend part of this budget thinking before they write


def draft_models() -> list[str]:
    """The writing model first (gpt-oss-120b by default), then the others as fallbacks."""
    return list(dict.fromkeys(m for m in (config.model("draft"), config.model("llm"), config.model("llm_fallback")) if m))


def draft_llm(messages: list[dict]) -> tuple[str, str | None, list[dict]]:
    """Asks for the answer text. A reasoning model can use its whole token budget thinking and return no text
    (finish_reason 'length'): then ask again with a bigger budget, then try the fallback model.
    Returns (text, model, diagnostics); text is '' if every attempt came back empty."""
    from common import dbx

    diag = []
    for model in draft_models():
        for max_tokens in DRAFT_TOKENS:
            try:
                resp = dbx.chat(messages, model=model, temperature=0.2, max_tokens=max_tokens, timeout=240)
            except Exception as ex:
                diag.append({"model": model, "max_tokens": max_tokens, "error": str(ex)[:200]})
                break
            choice = resp.choices[0]
            text = _text(choice.message.content)
            usage = getattr(resp, "usage", None)
            diag.append({"model": model, "max_tokens": max_tokens, "finish_reason": choice.finish_reason,
                         "content_type": type(choice.message.content).__name__, "chars": len(text),
                         "completion_tokens": getattr(usage, "completion_tokens", None)})
            if text:
                return text, model, diag
            if choice.finish_reason != "length":
                break
    return "", None, diag


SECTIONS = ("Headline", "Drivers", "Executive paragraph", "Definitions used")
MAX_EXEC_WORDS = 100   # the prompt asks for 90; a few words over is tolerated, more is sent back


def format_problems(text: str) -> list[str]:
    if not text.strip():
        return ["(empty draft: the model returned no text)"]
    problems = [f"(missing section: {s})" for s in SECTIONS if s.lower() not in text.lower()]
    m = re.search(r"executive paragraph\W*(.*?)(?:definitions used|$)", text, flags=re.I | re.S)
    if m:
        words = len(re.findall(r"\w[\w'’.,%$-]*", m.group(1)))
        if words > MAX_EXEC_WORDS:
            problems.append(f"(executive paragraph has {words} words; the limit is 90)")
    return problems


# ---------------------------------------------------------------- nodes

def guard(state: State) -> State:
    q = state["question"]
    if _REAL_BANK.search(q) or _REAL_BANK_I.search(q) or _REAL_DATA.search(q):
        return {"status": "out_of_scope", "reason": "asks about a real bank or real data"}
    return {"status": "planning"}


def plan(state: State) -> State:
    messages = list(state.get("messages") or [])
    if not messages:
        today = dt.date.today()
        messages = [{"role": "system", "content": prompts.PLAN_SYSTEM.format(
                        today=today.isoformat(), default_year=today.year, max_rounds=MAX_ROUNDS)},
                    {"role": "user", "content": state["question"]}]
    with tracing.span("plan", "LLM", {"round": state.get("rounds", 0) + 1}) as sp:
        msg, model = llm(messages, tools=tools.openai_tools(), tool_choice="auto", temperature=0, max_tokens=4000, timeout=150)
        text = _text(msg.content)
        calls = []
        for tc in msg.tool_calls or []:
            calls.append({"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "name": tc.function.name,
                          "arguments": tc.function.arguments or "{}"})
        sp.set_outputs({"text": text, "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in calls]})
    out: State = {"models": (state.get("models") or []) + [model]}
    if calls:
        assistant = {"role": "assistant", "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
        if text:
            assistant["content"] = text
        out.update(messages=messages + [assistant], pending=calls)
    elif text.upper().startswith("OUT_OF_SCOPE"):
        out.update(messages=messages, pending=[], status="out_of_scope", reason=text.split(":", 1)[-1].strip())
    elif not state.get("tool_calls") and not state.get("nudged") and not text.upper().startswith("READY"):
        out.update(messages=messages + [{"role": "assistant", "content": text or "(no answer)"},
                                        {"role": "user", "content": "Call the tools you need first. Every figure must come from a tool."}],
                   pending=[], nudged=True)
    else:
        out.update(messages=messages, pending=[], status="ready")
    return out


def run_tools(state: State) -> State:
    messages, done = list(state["messages"]), list(state.get("tool_calls") or [])
    for c in state.get("pending", []):
        try:
            args = json.loads(c["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as ex:
            args, result = {}, {"tool": c["name"], "args": {}, "sql": None, "columns": [], "rows": [], "row_count": 0,
                                "error": f"could not read the arguments: {ex}", "seconds": 0}
        else:
            with tracing.span(f"tool:{c['name']}", "TOOL", args) as sp:
                result = tools.call(c["name"], args)
                sp.set_outputs({"sql": result["sql"], "error": result["error"], "row_count": result["row_count"],
                                "rows": result["rows"][:PREVIEW_ROWS]})
        done.append({**result, "id": c["id"]})
        payload = {"error": result["error"], "row_count": result["row_count"], "rows": result["rows"][:ROWS_TO_LLM]}
        messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(payload, default=str)})
    rounds = state.get("rounds", 0) + 1
    return {"messages": messages, "tool_calls": done, "pending": [], "rounds": rounds,
            "status": "ready" if rounds >= MAX_ROUNDS else "planning"}


def _good_calls(state: State) -> list[dict]:
    return [c for c in state.get("tool_calls") or [] if not c.get("error") and c.get("rows")]


def _summaries(state: State) -> list[dict]:
    out = []
    for c in _good_calls(state):
        rows = summaries.summarize(c)
        if rows:
            out.append({"tool": f"{c['tool']} totals", "args": c["args"], "rows": rows})
    return out


def _checkable(state: State) -> list[dict]:
    """Tool results plus their Python summaries: everything a figure in the answer may come from."""
    return _good_calls(state) + _summaries(state)


def _extra_texts(state: State) -> list[str]:
    return [state["question"]] + retriever.glossary_text((state.get("retrieved") or {}).get("chunks", []))


def _as_of(calls: list[dict]) -> str:
    for c in calls:
        for row in c["rows"]:
            if row.get("as_of_date"):
                return str(row["as_of_date"])
    try:
        from common import dbx

        rows = dbx.sql(f"SELECT value FROM {config.catalog()}.gold.config WHERE key = 'as_of_date'")
        return rows[0]["value"] if rows else "the latest pipeline run"
    except Exception:
        return "the latest pipeline run"


def _no_data_only(state: State) -> bool:
    calls = state.get("tool_calls") or []
    return bool(calls) and all(c.get("error") and "after the last day of data" in c["error"] for c in calls)


def retrieve(state: State) -> State:
    calls = _good_calls(state)
    if _no_data_only(state):   # the period has no data yet: there is nothing to define or explain
        return {"retrieved": {"chunks": [], "queries": [], "mode": "skipped (no data for the period)", "note": None},
                "as_of": _as_of(calls)}
    with tracing.span("retrieve", "RETRIEVER", {"question": state["question"]}) as sp:
        r = retriever.retrieve(state["question"], calls, use_vector_search=state["options"].get("use_vector_search", True))
        sp.set_outputs([{"doc": c.get("title"), "section": c.get("section"), "score": c.get("score"),
                         "text": c.get("text")} for c in r["chunks"]])
        sp.set_attributes({"retrieval_mode": r["mode"], "queries": json.dumps(r["queries"])})
    return {"retrieved": r, "as_of": _as_of(calls)}


def _context(chunks: list[dict]) -> str:
    return "\n\n".join(f"[{c.get('title')} › {c.get('section')}] ({c.get('doc_type')})\n{c.get('text')}" for c in chunks) \
        or "(no reference text found)"


def draft(state: State) -> State:
    attempts = state.get("attempts", 0)
    if attempts == 0:
        results = [{"tool": c["tool"], "args": c["args"], "rows": c["rows"][:ROWS_TO_LLM]} for c in _checkable(state)]
        failed = [{"tool": c["tool"], "args": c["args"], "error": c["error"]} for c in state.get("tool_calls") or [] if c.get("error")]
        if failed:
            results.append({"failed_calls": failed})
        messages = [{"role": "system", "content": prompts.DRAFT_SYSTEM.format(as_of=state["as_of"])},
                    {"role": "user", "content": prompts.DRAFT_USER.format(
                        question=state["question"], tool_results=json.dumps(results, default=str),
                        context=_context(state["retrieved"]["chunks"]))}]
    elif not state["draft"].strip():   # nothing to correct: ask again from the start
        messages = state["draft_messages"]
    else:
        last = state["checks"][-1]
        messages = state["draft_messages"] + [{"role": "assistant", "content": state["draft"]},
                                              {"role": "user", "content": prompts.RETRY_USER.format(
                                                  unmatched="\n".join(f"- {u}" for u in last["unmatched"]))}]
    with tracing.span("draft", "LLM", {"attempt": attempts + 1}) as sp:
        text, model, diag = draft_llm(messages)
        text = tidy(text)
        if text and state["options"].get("inject_wrong_number"):
            text = number_check.inject_wrong_number(text, number_check.allowed_values(_checkable(state), _extra_texts(state)))
        sp.set_outputs(text)
        sp.set_attributes({"draft_calls": json.dumps(diag)})
    return {"draft": text, "draft_messages": messages if attempts == 0 else state["draft_messages"],
            "attempts": attempts + 1, "models": (state.get("models") or []) + ([model] if model else []),
            "draft_diag": (state.get("draft_diag") or []) + [{"attempt": attempts + 1, "calls": diag}]}


def check_numbers(state: State) -> State:
    extra = _extra_texts(state)
    with tracing.span("number_check", "PARSER", {"attempt": state["attempts"]}) as sp:
        r = number_check.check(state["draft"], _checkable(state), extra)
        problems = format_problems(state["draft"])
        if problems:
            r = {**r, "passed": False, "unmatched": problems + r["unmatched"]}
        sp.set_outputs(r)
        sp.set_attributes({"number_check_passed": r["passed"], "unmatched_numbers": json.dumps(r["unmatched"]),
                           "attempt": state["attempts"]})
    return {"checks": (state.get("checks") or []) + [r]}


FOOTER = "_Synthetic data · Northlake Bank (fictional). Data as of {as_of}._"

_LEAKS = [
    r"\s*【[^】]*】",                                  # gpt-oss source brackets (the tools are in "show your work")
    r"\s*\((?=[^()]*\b[a-z]+_[a-z0-9_]+\b)[^()]*\)",   # (raroc_min = 0.0606), (missing_products)
    r"\s*\([^()]*\btotals?\b[^()]*\)",                # (cross-sell whitespace totals)
    r"\s*\([^()]*(?<![\d.$])0\.\d+(?![\d.]*\s*[%KMBkmb])[^()]*\)",   # (raroc 0.0899), (0.25): raw decimals
    r"\s*\([^()]*\bflag\b[^()]*\)",                 # (over capacity flag true)
    r"\s*[–—-]\s*summary rows?\b|\s*\(summary rows?\)",  # "– summary row", "(summary row)"
    r"\s*[–—-]\s*[a-z]+_[a-z0-9_]+\s*(?:=\s*)?(?:true|false)\b",   # "– over_capacity true"
    r"\s*\((?:tool|table|from|via)?\s*`[^`]+`\)",       # "(tool `get_kpi`)"
    r"\s+(?:using|from|via)\s+`[a-z0-9_.]+`",            # "using `gold.fact_pipeline`"
    r"\s+in the totals(?: rows?)?\b",                     # "… in the totals row"
]


def tidy(text: str) -> str:
    """Removes what the writer is told never to show: column names, raw decimals, source brackets."""
    for pat in _LEAKS:
        text = re.sub(pat, "", text)
    return text


def evidence_text(state: State, limit: int = 30000) -> str:
    parts = [f"[tool {c['tool']} {json.dumps(c['args'])}]\n{json.dumps(c['rows'][:ROWS_TO_LLM], default=str)}"
             for c in _checkable(state)]
    parts += [f"[tool {c['tool']} {json.dumps(c['args'])} returned an error] {c['error']}"
              for c in state.get("tool_calls") or [] if c.get("error")]
    parts += [f"[document {c.get('title')} › {c.get('section')}]\n{c.get('text')}"
              for c in (state.get("retrieved") or {}).get("chunks", [])]
    return "\n\n".join(parts)[:limit] or "(no evidence)"


def verify_claims(question: str, answer: str, evidence: str) -> dict:
    """{'unsupported': [{'claim', 'evidence_says'}], 'model'}; {'error'} if the checker can't be reached."""
    from common import dbx

    model = config.model("verifier") or config.model("draft") or config.model("llm")
    try:
        resp = dbx.chat([{"role": "user", "content": prompts.CLAIM_CHECK.format(question=question, evidence=evidence,
                                                                                 answer=answer)}],
                        model=model, temperature=0, max_tokens=6000, timeout=120)
        text = _text(resp.choices[0].message.content)
        m = re.search(r"\{.*\}", text, re.S)
        found = json.loads(m.group(0)).get("unsupported", []) if m else None
        if found is None:
            raise ValueError(f"not JSON: {text[:150]!r}")
        return {"unsupported": [x for x in found if isinstance(x, dict) and x.get("claim")], "model": model}
    except Exception as ex:   # the claim check is a second safeguard: if it is down, the number check still applies
        return {"unsupported": [], "model": model, "error": str(ex)[:200]}


def check_claims(state: State) -> State:
    with tracing.span("claim_check", "LLM", {"attempt": state["attempts"]}) as sp:
        r = verify_claims(state["question"], state["draft"], evidence_text(state))
        sp.set_outputs(r)
        sp.set_attributes({"claim_check_passed": not r["unsupported"], "unsupported_claims": json.dumps(r["unsupported"])})
    checks = list(state["checks"])
    if r["unsupported"]:   # mark this draft as failed, with the reasons, so the redraft sees them
        last = checks[-1]
        reasons = [f"(unsupported claim: \"{x['claim']}\"; the evidence says {x.get('evidence_says', 'otherwise')})"
                   for x in r["unsupported"]]
        checks[-1] = {**last, "passed": False, "unmatched": reasons + last["unmatched"], "claims_failed": True}
    return {"checks": checks, "claims": (state.get("claims") or []) + [{"attempt": state["attempts"], **r}]}


def finish(state: State) -> State:
    """The footer is always ours, with the real as-of date (a model once wrote a window end date there instead)."""
    lines = [l for l in state["draft"].rstrip().splitlines() if not re.match(r"^[\W_]*synthetic data", l.strip(), re.I)]
    answer = "\n".join(lines).rstrip() + "\n\n" + FOOTER.format(as_of=state.get("as_of"))
    return {"answer": answer, "outcome": "answered"}


def _table(rows: list[dict], limit: int = 10) -> str:
    if not rows:
        return "_no rows_"
    cols = [k for k in rows[0] if k != "error" and any(r.get(k) not in (None, "") for r in rows)][:9]

    def fmt(v):
        if isinstance(v, float):
            return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:.4g}"
        return str(v).replace("|", "/") if v is not None else ""
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(r.get(c)) for c in cols) + " |" for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"\n_{len(rows) - limit} more rows not shown_")
    return "\n".join(lines)


def blocked(state: State) -> State:
    parts = [prompts.WITHHELD]
    for c in _good_calls(state):
        args = ", ".join(f"{k}={v}" for k, v in c["args"].items())
        parts.append(f"**{c['tool']}({args})**\n\n{_table(c['rows'])}")
    parts.append(f"_Synthetic data · Northlake Bank (fictional). Data as of {state.get('as_of')}._")
    return {"answer": "\n\n".join(parts), "outcome": "blocked"}


def refuse(state: State) -> State:
    return {"answer": prompts.REFUSAL, "outcome": "refused"}


# ---------------------------------------------------------------- edges

def after_guard(state: State) -> str:
    return "refuse" if state["status"] == "out_of_scope" else "plan"


def after_plan(state: State) -> str:
    if state.get("status") == "out_of_scope":
        return "refuse"
    if state.get("pending"):
        return "tools"
    return "retrieve" if state.get("status") == "ready" else "plan"


def after_tools(state: State) -> str:
    return "retrieve" if state["status"] == "ready" else "plan"


def after_check(state: State) -> str:
    if state["checks"][-1]["passed"]:
        return "claim_check" if CLAIM_CHECK else "finish"
    return "draft" if state["attempts"] <= MAX_RETRIES else "blocked"


def after_claims(state: State) -> str:
    if state["checks"][-1]["passed"]:
        return "finish"
    return "draft" if state["attempts"] <= MAX_RETRIES else "blocked"


def build_graph():
    g = StateGraph(State)
    for name, fn in [("guard", guard), ("plan", plan), ("tools", run_tools), ("retrieve", retrieve), ("draft", draft),
                     ("number_check", check_numbers), ("claim_check", check_claims), ("finish", finish),
                     ("blocked", blocked), ("refuse", refuse)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"plan": "plan", "refuse": "refuse"})
    g.add_conditional_edges("plan", after_plan, {"tools": "tools", "retrieve": "retrieve", "plan": "plan", "refuse": "refuse"})
    g.add_conditional_edges("tools", after_tools, {"plan": "plan", "retrieve": "retrieve"})
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "number_check")
    g.add_conditional_edges("number_check", after_check, {"claim_check": "claim_check", "finish": "finish",
                                                           "draft": "draft", "blocked": "blocked"})
    g.add_conditional_edges("claim_check", after_claims, {"finish": "finish", "draft": "draft", "blocked": "blocked"})
    for end in ("finish", "blocked", "refuse"):
        g.add_edge(end, END)
    return g.compile()


GRAPH = build_graph()


# ---------------------------------------------------------------- entry point

def run(question: str, inject_wrong_number: bool = False, use_vector_search: bool = True) -> dict:
    """Answers one question. Returns the answer and the 'show your work' payload."""
    t0 = time.time()
    options = {"inject_wrong_number": inject_wrong_number, "use_vector_search": use_vector_search}
    with tracing.span("cib_insights_agent", "AGENT", {"question": question}) as root:
        state = GRAPH.invoke({"question": question, "options": options}, {"recursion_limit": 40})
        checks = state.get("checks") or []
        work = {
            "question": question,
            "outcome": state["outcome"],
            "answer": state["answer"],
            "as_of": state.get("as_of"),
            "tool_calls": [{"name": c["tool"], "params": c["args"], "sql": c.get("sql"), "row_count": c.get("row_count"),
                            "rows_preview": c.get("rows", [])[:PREVIEW_ROWS], "error": c.get("error"), "seconds": c.get("seconds")}
                           for c in state.get("tool_calls") or []],
            "retrieved": [{"doc": c.get("title"), "doc_id": c.get("doc_id"), "section": c.get("section"),
                           "score": c.get("score")} for c in (state.get("retrieved") or {}).get("chunks", [])],
            "retrieval_mode": (state.get("retrieved") or {}).get("mode"),
            "retrieval_note": (state.get("retrieved") or {}).get("note"),
            "number_check": {"passed": bool(checks) and checks[-1]["passed"],
                             "unmatched": checks[-1]["unmatched"] if checks else [],
                             "retries": max(0, len(checks) - 1), "checked": checks[-1]["checked"] if checks else 0,
                             "attempts": checks},
            "models": list(dict.fromkeys(state.get("models") or [])),
            "draft_diagnostics": state.get("draft_diag") or [],
            "claim_check": {"enabled": CLAIM_CHECK, "runs": state.get("claims") or [],
                            "passed": bool(state.get("claims")) and not state["claims"][-1]["unsupported"]},
            "refusal_reason": state.get("reason") if state["outcome"] == "refused" else None,
        }
        work["latency_ms"] = int((time.time() - t0) * 1000)
        # Everything the answer may rest on, for the evaluation judges (not shown to users)
        work["evidence"] = {
            "tool_results": [{"tool": c["tool"], "args": c["args"], "rows": c["rows"][:ROWS_TO_LLM]} for c in _checkable(state)],
            "failed_calls": [{"tool": c["tool"], "args": c["args"], "error": c["error"]}
                             for c in state.get("tool_calls") or [] if c.get("error")],
            "retrieved": [{"doc_id": c.get("doc_id"), "title": c.get("title"), "section": c.get("section"), "text": c.get("text")}
                          for c in (state.get("retrieved") or {}).get("chunks", [])],
        }
        root.set_outputs(work["answer"])
        root.set_attributes({"outcome": work["outcome"], "number_check_passed": work["number_check"]["passed"],
                             "unmatched_numbers": json.dumps(work["number_check"]["unmatched"]),
                             "retries": work["number_check"]["retries"], "tools_called": len(work["tool_calls"])})
    work["trace_id"] = tracing.last_trace_id()
    return work
