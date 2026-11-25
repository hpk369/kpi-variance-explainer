"""Connection smoke test: SQL warehouse, LLM (+ tool call), embeddings, Vector Search endpoint.

Run locally:  python space/smoke_test.py
In the Space:  the "Run smoke test" button calls run_all().
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "common").exists():
    sys.path.insert(0, str(ROOT))
else:  # inside the Space build folder, common/ sits next to this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config, dbx  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "get_kpi", "description": "Return a KPI value for a period and segment",
    "parameters": {"type": "object",
                   "properties": {"metric": {"type": "string"}, "period": {"type": "string"}, "segment": {"type": "string"}},
                   "required": ["metric", "period", "segment"]}}}]


def check_sql():
    rows, secs = dbx.timed(dbx.sql, "SELECT 1 AS ok, current_user() AS user")
    return f"SELECT 1 → {rows[0]['ok']} as {rows[0]['user']} ({secs:.1f}s)"


def check_llm(kind: str):
    name = config.model(kind)
    msg = [{"role": "user", "content": "What was Industrials revenue in 2026-Q3? Use the tool."}]
    r, secs = dbx.timed(dbx.chat, msg, model=name, tools=TOOLS, max_tokens=1000)
    tc = r.choices[0].message.tool_calls
    if not tc:
        raise RuntimeError(f"{name} answered without calling the tool")
    return f"{name} via {dbx.model_route(name)} → {tc[0].function.name}{tc[0].function.arguments} ({secs:.1f}s)"


def check_embedding():
    name = config.model("embedding")
    v, secs = dbx.timed(dbx.embed, ["weighted pipeline"], model=name)
    return f"{name} via {dbx.model_route(name)} → {len(v[0])} dimensions ({secs:.1f}s)"


def check_vector_search():
    state = dbx.vs_endpoint_state()
    if state != "ONLINE":
        raise RuntimeError(f"endpoint state is {state} (wait until ONLINE)")
    return f"endpoint {config.env('VS_ENDPOINT', 'cib_vs_endpoint', required=False)} is ONLINE"


CHECKS = [
    ("SQL warehouse", check_sql),
    ("Agent LLM", lambda: check_llm("llm")),
    ("Backup LLM", lambda: check_llm("llm_fallback")),
    ("Embeddings", check_embedding),
    ("Vector Search", check_vector_search),
]


def run_all() -> list[tuple[bool, str, str]]:
    results = []
    for label, fn in CHECKS:
        print(f"[smoke] {label}: starting", flush=True)
        try:
            results.append((True, label, fn()))
        except Exception as ex:
            results.append((False, label, str(ex)[:300]))
        print(f"[smoke] {label}: {'ok' if results[-1][0] else 'FAILED'} {results[-1][2][:200]}", flush=True)
    return results


if __name__ == "__main__":
    res = run_all()
    for ok, label, detail in res:
        print(f"{'✓' if ok else '✗'} {label:14} {detail}")
    sys.exit(0 if all(ok for ok, _, _ in res) else 1)
