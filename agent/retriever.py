"""Retrieves definitions and reference text for an answer from the Vector Search index.

Queries: the question itself, plus one glossary lookup per metric the tool calls used (e.g. RAROC, Slipped deal).
Falls back to a keyword search over the local knowledge/ files if Vector Search can't be reached, and says so.
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMNS = ["chunk_id", "doc_id", "doc_type", "title", "section", "text"]

# Glossary terms behind each tool: (always, {term: words in the question that make it relevant})
TOOL_TERMS = {
    "get_kpi": {"revenue": (["Revenue vs Plan"], {}), "raroc": (["RAROC", "Hurdle rate"], {}),
                "win_rate": (["Win rate"], {}), "weighted_pipeline": (["Weighted pipeline"], {}),
                "products_per_client": (["Products per client"], {})},
    "variance_drivers": (["Revenue vs Plan", "Variance driver"], {}),
    "pipeline_conversion": {"summary": (["Slipped deal"], {"Win rate": r"win"}),
                            "slipped": (["Slipped deal"], {}), "deals": (["Slipped deal"], {})},
    "cross_sell_whitespace": (["RAROC", "Hurdle rate"], {"Cross-sell whitespace": r"cross|whitespace|products? .*(?:miss|hold)"}),
    "coverage_capacity": ([], {"Coverage capacity": r"capacit|per banker|bankers", "Win rate": r"\bwin",
                               "Revenue per banker": r"revenue|productiv"}),   # none matched → Coverage capacity
    "recon_breaks": (["Reconciliation break"], {}),
}
EXTRA_QUERIES = {"recon_breaks": ["triage steps for a reconciliation break", "reconciliation break severity owner escalation"]}
# Management commentary is not retrieved for answers: it is dated, its figures go stale, and it once led the agent
# to repeat an old status ("under investigation") as fact. The glossary, product one-pagers and SOP are timeless.
ANSWER_DOC_TYPES = ["glossary", "product", "sop"]
DEFINITION = re.compile(r"\bwhat (?:is|are)\b|defin|counts? as|\bmean(?:s|ing)?\b|\btreat", re.I)


def terms_for(tool_calls: list[dict], question: str = "") -> list[str]:
    out: list[str] = []
    for c in tool_calls:
        spec = TOOL_TERMS.get(c["tool"])
        if isinstance(spec, dict):   # depends on an argument: metric for get_kpi, detail for pipeline_conversion
            key = c.get("args", {}).get("metric") or c.get("args", {}).get("detail") or "summary"
            spec = spec.get(str(key).lower())
        if not spec:
            continue
        always, optional = spec
        terms = always + [t for t, pat in optional.items() if re.search(pat, question, re.I)]
        if not terms and optional:   # nothing in the question points at one term: use the tool's main one
            terms = [next(iter(optional))]
        out += [t for t in terms if t not in out]
    if any(str(c.get("args", {}).get("period", "")).upper() in ("MTD", "QTD") for c in tool_calls):
        out.append("Month to date (MTD), quarter to date (QTD) and as-of date")
    return out


@lru_cache(maxsize=1)
def _local_index():
    from rag.chunking import load_folder
    from rag.retrieval_tests import Bm25

    chunks = load_folder(ROOT / "knowledge")
    return Bm25(chunks), {c["chunk_id"]: c for c in chunks}


def _search(query: str, k: int, filters: dict | None, mode: list[str]) -> list[dict]:
    if mode[0] == "vector_search":
        try:
            from common import dbx

            return dbx.vs_query(query, k=k, columns=COLUMNS, filters=filters)
        except Exception as ex:  # index offline, token problem, ...
            mode[:] = ["local_keyword", f"Vector Search failed ({str(ex)[:150]}); used local keyword search"]
    engine, by_id = _local_index()
    hits = engine.search(query, k * 4 if filters else k)
    out = []
    for h in hits:
        c = by_id[h["chunk_id"]]
        if filters and any(c.get(f) not in (v if isinstance(v, list) else [v]) for f, v in filters.items()):
            continue
        out.append({**{col: c.get(col) for col in COLUMNS}, "score": h["score"]})
    return out[:k]


def retrieve(question: str, tool_calls: list[dict], k_question: int = 1, use_vector_search: bool = True) -> dict:
    """{'chunks': [...best first, deduplicated], 'queries': [...], 'mode': 'vector_search' | 'local_keyword', 'note'}"""
    mode = ["vector_search" if use_vector_search else "local_keyword"]
    queries, seen, chunks = [], set(), []

    def add(query: str, k: int, filters: dict | None = None):
        queries.append({"query": query, "filters": filters})
        for h in _search(query, k, filters, mode):
            if h["chunk_id"] not in seen:
                seen.add(h["chunk_id"])
                chunks.append({**h, "query": query})

    for term in terms_for(tool_calls, question):
        add(term, 1, {"doc_type": "glossary"})
    for c in tool_calls:
        for q in EXTRA_QUERIES.get(c["tool"], []):
            add(q, 2, {"doc_type": "sop"})
    if not tool_calls:
        if DEFINITION.search(question):   # "what is RAROC", "how is X defined": the glossary entry is the answer
            add(question, k_question, {"doc_type": "glossary"})
        else:                             # "what drives M&A revenue", "what do we do when...": any reference section
            add(question, k_question, {"doc_type": ANSWER_DOC_TYPES})
    # With tool results, only the definitions of the metrics those tools returned (and the SOP for breaks) are
    # retrieved: a free search on the question added product and SOP sections the answer didn't need.
    return {"chunks": chunks[:10], "queries": queries, "mode": mode[0], "note": mode[1] if len(mode) > 1 else None}


def glossary_text(chunks: list[dict]) -> list[str]:
    """Definition texts whose numbers the answer may quote (hurdle, tax rate, stage probabilities, tolerance)."""
    return [c["text"] for c in chunks if c.get("doc_type") in ("glossary", "sop")]
