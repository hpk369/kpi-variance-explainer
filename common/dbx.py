"""Thin Databricks client: SQL warehouse, model calls (Unity Gateway first, Serving second), Vector Search status."""
from __future__ import annotations

import time
from functools import lru_cache

import requests

from common import config

# Unity Gateway names models system.ai.<name>; the older Serving API names them databricks-<name>.
ROUTES = {
    "gateway": ("/ai-gateway/mlflow/v1", "system.ai.{}"),
    "serving": ("/serving-endpoints", "databricks-{}"),
}
_working_route: dict[str, str] = {}


def _bare(name: str) -> str:
    for prefix in ("system.ai.", "databricks-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# ---------- SQL warehouse ----------

def _sql_connection():
    from databricks import sql  # databricks-sql-connector

    return sql.connect(
        server_hostname=config.host().replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{config.env('DATABRICKS_WAREHOUSE_ID')}",
        access_token=config.env("DATABRICKS_TOKEN"),
        _socket_timeout=60,  # fail in minutes, not the 15-minute default
        _retry_stop_after_attempts_count=3,
        _retry_stop_after_attempts_duration=120,
    )


def sql(query: str, params: dict | None = None) -> list[dict]:
    """Run one statement and return rows as dicts (empty list for DDL)."""
    with _sql_connection() as conn, conn.cursor() as cur:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        if cur.description is None:
            return []
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------- Models ----------

@lru_cache(maxsize=4)
def _client(route: str):
    from openai import OpenAI

    path, _ = ROUTES[route]
    return OpenAI(api_key=config.env("DATABRICKS_TOKEN"), base_url=config.host() + path, timeout=60, max_retries=1)


def _call(model: str, fn):
    """Try the route that worked last time, then the others. Returns (result, route, full_model_name)."""
    bare = _bare(model)
    order = [_working_route[bare]] if bare in _working_route else []
    order += [r for r in ROUTES if r not in order]
    errors = []
    for route in order:
        full = ROUTES[route][1].format(bare)
        for wait in RATE_LIMIT_WAITS + (None,):
            try:
                result = fn(_client(route), full)
                _working_route[bare] = route
                return result, route, full
            except Exception as ex:
                if wait is not None and _rate_limited(ex):   # quota per minute: wait, then same route again
                    time.sleep(wait)
                    continue
                errors.append(f"{route}: {str(ex)[:120]}")
                break
    raise RuntimeError(f"Model {bare} failed on every route. " + " | ".join(errors))


RATE_LIMIT_WAITS = (20, 40, 60)   # seconds; Free Edition limits output tokens per minute per workspace


def _rate_limited(ex: Exception) -> bool:
    text = str(ex)
    return "429" in text or "REQUEST_LIMIT_EXCEEDED" in text or "rate limit" in text.lower()


def chat(messages: list[dict], model: str | None = None, **kwargs):
    """OpenAI-style chat completion. kwargs: tools, max_tokens, temperature..."""
    model = model or config.model("llm")
    response, _, _ = _call(model, lambda c, m: c.chat.completions.create(model=m, messages=messages, **kwargs))
    return response


def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    model = model or config.model("embedding")
    response, _, _ = _call(model, lambda c, m: c.embeddings.create(model=m, input=texts))
    return [d.embedding for d in response.data]


def model_route(model: str) -> str | None:
    return _working_route.get(_bare(model))


# ---------- Vector Search ----------

def _api(method: str, path: str, **kwargs) -> dict:
    r = requests.request(
        method,
        config.host() + path,
        headers={"Authorization": f"Bearer {config.env('DATABRICKS_TOKEN')}"},
        timeout=30,
        **kwargs,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def vs_endpoint_state(name: str | None = None) -> str:
    name = name or config.env("VS_ENDPOINT", config.constants()["vector_search"]["endpoint"], required=False)
    info = _api("GET", f"/api/2.0/vector-search/endpoints/{name}")
    return info.get("endpoint_status", {}).get("state", "UNKNOWN")


DOC_COLUMNS = ["chunk_id", "doc_id", "doc_type", "title", "section", "text"]


def vs_index_name() -> str:
    return config.env("VS_INDEX", config.constants()["vector_search"]["index"], required=False)


@lru_cache(maxsize=4)
def vs_index_info(name: str) -> dict:
    return _api("GET", f"/api/2.0/vector-search/indexes/{name}")


def vs_query(query: str, k: int = 5, columns: list[str] | None = None, filters: dict | None = None,
             index: str | None = None) -> list[dict]:
    """Top-k chunks for a question, best first, each with a `score`. Works for a managed-embedding index
    (sends the text) and a self-managed one (embeds the question here first). filters e.g. {"doc_type": "glossary"}."""
    import json

    index = index or vs_index_name()
    spec = vs_index_info(index).get("delta_sync_index_spec", {})
    body: dict = {"columns": columns or DOC_COLUMNS, "num_results": k}
    if spec.get("embedding_source_columns"):
        body["query_text"] = query
    else:
        body["query_vector"] = embed([query])[0]
    if filters:
        body["filters_json"] = json.dumps(filters)
    res = _api("POST", f"/api/2.0/vector-search/indexes/{index}/query", json=body)
    cols = [c["name"] for c in res.get("manifest", {}).get("columns", [])]
    return [dict(zip(cols, row)) for row in (res.get("result", {}) or {}).get("data_array", []) or []]


def timed(fn, *args, **kwargs):
    t0 = time.time()
    out = fn(*args, **kwargs)
    return out, time.time() - t0
