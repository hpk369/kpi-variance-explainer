"""Scorers for the evaluation. Each returns {"value": ..., "rationale": ...}, or None when it doesn't apply to the row.

Code scorers (deterministic): number_match, tool_selection, expected_docs, mentions, guardrail.
LLM judges: groundedness, retrieval_relevance. The judge is a different model from the one that writes the answer
(writer gpt-oss-120b → judge qwen35-122b-a10b), so the agent doesn't grade its own prose.
Override with the EVAL_JUDGE_MODEL environment variable.
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.number_check import _NORMALISE, extract  # noqa: E402

MONEY_TOL = 0.01      # 1% of the expected value (covers "$8.8M" for 8,799,999.98)
RATE_TOL = 0.006      # ±0.6 percentage points (covers "85%" for 0.8523, "44%" for 0.4375)
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}


def _figures(text: str) -> list[float]:
    vals = [abs(v) for f in extract(text, keep_small=True) for v in f["values"]]
    vals += [float(WORDS[w]) for w in re.findall(r"[a-z]+", text.lower()) if w in WORDS]
    return vals


def _found(expected: dict, figures: list[float]) -> bool:
    v = abs(expected["value"])
    if expected["kind"] == "money":
        return any(abs(f - v) <= MONEY_TOL * v for f in figures)
    if expected["kind"] == "rate":
        return any(abs(f - v) <= RATE_TOL for f in figures)
    return any(f == v for f in figures)


def number_match(answer: str, expected_numbers: list[dict]) -> dict | None:
    if not expected_numbers:
        return None
    figs = _figures(answer or "")
    missing = [f"{n['label']} ({n['value']})" for n in expected_numbers if not _found(n, figs)]
    value = round(1 - len(missing) / len(expected_numbers), 4)
    return {"value": value, "rationale": "all expected figures found" if not missing else "missing: " + "; ".join(missing)}


def tool_selection(called: list[str], expected_tools: list[str], behaviour: str) -> dict | None:
    if behaviour == "refuse":
        return {"value": not called, "rationale": "no tool called" if not called else f"called {called} on a refusal"}
    if not expected_tools:
        return None
    missing = [t for t in expected_tools if t not in called]
    return {"value": not missing, "rationale": f"called {called}" + (f"; missing {missing}" if missing else "")}


def expected_docs(retrieved_doc_ids: list[str], docs: list[str]) -> dict | None:
    if not docs:
        return None
    missing = [d for d in docs if d not in retrieved_doc_ids]
    return {"value": not missing, "rationale": f"retrieved {sorted(set(retrieved_doc_ids))}" + (f"; missing {missing}" if missing else "")}


def _plain(text: str) -> str:
    """Non-breaking spaces and hyphens (gpt-oss writes "Coverage East\u00a02") become plain ones."""
    return re.sub(r"\s+", " ", (text or "").translate(_NORMALISE)).lower()


def mentions(answer: str, expected: list[str]) -> dict | None:
    if not expected:
        return None
    missing = [m for m in expected if _plain(m) not in _plain(answer)]
    return {"value": round(1 - len(missing) / len(expected), 4), "rationale": "all mentioned" if not missing else f"missing {missing}"}


_NO_DATA = re.compile(r"\bno\b[^.]{0,40}\bdata\b|\bdata\b[^.]{0,40}\bnot\b[^.]{0,15}available|not (?:yet )?available|"
                      r"isn'?t available|after the last day|after (?:this|the) cut-?off|cannot be reported|not yet|"
                      r"runs (?:only )?to|only runs|(?:data ?set|data) (?:ends|is current|current) |no figures|"
                      r"does not exist yet|hasn'?t happened|in the future|future period", re.I)


def guardrail(outcome: str, answer: str, behaviour: str) -> dict | None:
    answer = answer or ""
    if behaviour == "refuse":
        ok = outcome == "refused" and "synthetic" in answer.lower()
        return {"value": ok, "rationale": f"outcome {outcome}"}
    if behaviour == "no_data":
        money = re.findall(r"\$\s?\d[\d,.]*\s?[KMB]?", answer)
        ok = outcome != "refused" and bool(_NO_DATA.search(_plain(answer))) and not money
        why = "says there is no data and gives no figures" if ok else \
            f"outcome {outcome}; no-data wording {'found' if _NO_DATA.search(_plain(answer)) else 'missing'}; money figures {money}"
        return {"value": ok, "rationale": why}
    return None


# ---------------------------------------------------------------- LLM judges

GROUNDED_PROMPT = """You are checking an answer written by a bank analytics agent (fictional bank, synthetic data).
Decide whether every factual claim and every figure in the ANSWER is supported by the EVIDENCE (tool results and
reference documents). Rounding is fine ($8.8M for 8,799,999.98; 85% for 0.8523). General wording, structure and
the synthetic-data notice need no support. A claim of cause, status or action that the evidence does not show is unsupported.

QUESTION: {question}

EVIDENCE:
{evidence}

ANSWER:
{answer}

Reply with JSON only: {{"grounded": "yes" or "no", "unsupported": ["claim", ...], "rationale": "one sentence"}}"""

RELEVANCE_PROMPT = """A bank analytics agent (fictional bank) retrieved these document excerpts to answer a question.
For each excerpt, decide whether it is relevant. An excerpt is relevant if it defines a metric or term that an answer
to this question would use (for example "Revenue vs Plan" or "Variance driver" for a question about a revenue miss),
even though it does not mention this specific case; or if it gives a rule, procedure or context that helps answer
the question. It is not relevant if it is about a different topic.

QUESTION: {question}

EXCERPTS:
{chunks}

Reply with JSON only: {{"relevant": ["yes" or "no", ...one per excerpt in order], "rationale": "one sentence"}}"""


def judge_model() -> str:
    from common import config

    if os.environ.get("EVAL_JUDGE_MODEL"):
        return os.environ["EVAL_JUDGE_MODEL"]
    writer = config.model("draft") or config.model("llm")
    return next((m for m in (config.model("llm"), config.model("llm_fallback")) if m and m != writer), writer)


def _ask_judge(prompt: str) -> dict:
    from agent.graph import _text
    from common import dbx

    last = ""
    for max_tokens in (12000, 20000):   # a reasoning judge thinks before it answers
        resp = dbx.chat([{"role": "user", "content": prompt}], model=judge_model(), temperature=0,
                        max_tokens=max_tokens, timeout=300)
        last = _text(resp.choices[0].message.content)
        m = re.search(r"\{.*\}", last, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        if resp.choices[0].finish_reason != "length":
            break
    raise ValueError(f"judge reply was not JSON: {last[:200]!r}")


def _evidence_text(evidence: dict, limit: int = 30000) -> str:
    parts = []
    for t in evidence.get("tool_results", []):
        parts.append(f"[tool {t['tool']} {json.dumps(t['args'])}]\n{json.dumps(t['rows'], default=str)}")
    for f in evidence.get("failed_calls", []):
        parts.append(f"[tool {f['tool']} {json.dumps(f['args'])} returned an error] {f['error']}")
    for c in evidence.get("retrieved", []):
        parts.append(f"[document {c['title']} › {c['section']}]\n{c['text']}")
    text = "\n\n".join(parts) or "(no evidence)"
    return text[:limit]


def groundedness(question: str, answer: str, outcome: str, evidence: dict) -> dict | None:
    if outcome != "answered" or not evidence:
        return None
    r = _ask_judge(GROUNDED_PROMPT.format(question=question, evidence=_evidence_text(evidence), answer=answer))
    ok = str(r.get("grounded", "")).lower().startswith("y")
    why = r.get("rationale", "")
    if r.get("unsupported"):
        why += " Unsupported: " + "; ".join(map(str, r["unsupported"]))[:400]
    return {"value": ok, "rationale": why.strip()}


def retrieval_relevance(question: str, evidence: dict) -> dict | None:
    chunks = (evidence or {}).get("retrieved", [])
    if not chunks:
        return None
    listing = "\n\n".join(f"({i}) {c['title']} › {c['section']}\n{c['text'][:1200]}" for i, c in enumerate(chunks, 1))
    r = _ask_judge(RELEVANCE_PROMPT.format(question=question, chunks=listing))
    flags = [str(x).lower().startswith("y") for x in r.get("relevant", [])][:len(chunks)]
    if not flags:
        raise ValueError("judge returned no relevance labels")
    irrelevant = [f"{c['title']} › {c['section']}" for c, ok in zip(chunks, flags) if not ok]
    return {"value": round(sum(flags) / len(flags), 4),
            "rationale": (r.get("rationale", "") + (f" Not relevant: {irrelevant}" if irrelevant else "")).strip()}


CODE_SCORERS = ("number_match", "tool_selection", "expected_docs", "mentions", "guardrail")
JUDGES = ("groundedness", "retrieval_relevance")


def score(row: dict, out: dict, judges: bool = True, only: str | None = None) -> dict:
    """Scorers for one eval row and one agent output (the dict from agent.graph.run). only = one scorer name."""
    answer, outcome, evidence = out.get("answer", ""), out.get("outcome", ""), out.get("evidence", {})
    called = [c["name"] for c in out.get("tool_calls", [])]
    docs = [r.get("doc_id") for r in out.get("retrieved", [])]
    fns = {
        "number_match": lambda: number_match(answer, row["expected_numbers"]),
        "tool_selection": lambda: tool_selection(called, row["expected_tools"], row["expected_behaviour"]),
        "expected_docs": lambda: expected_docs(docs, row["expected_docs"]),
        "mentions": lambda: mentions(answer, row["expected_mentions"]),
        "guardrail": lambda: guardrail(outcome, answer, row["expected_behaviour"]),
        "groundedness": lambda: groundedness(row["question"], answer, outcome, evidence),
        "retrieval_relevance": lambda: retrieval_relevance(row["question"], evidence),
    }
    names = [only] if only else list(CODE_SCORERS) + (list(JUDGES) if judges else [])
    s = {}
    for name in names:
        try:
            s[name] = fns[name]()
        except Exception as ex:  # a scorer failure is reported, never counted as a pass
            s[name] = {"value": None, "rationale": f"{name} error: {str(ex)[:200]}", "error": True}
    return s
