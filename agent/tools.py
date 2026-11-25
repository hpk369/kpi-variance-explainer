"""The agent's only way to get numbers: the six governed Unity Catalog functions in <catalog>.gold.

    from agent import tools
    tools.openai_tools()                      # tool schemas for the LLM (OpenAI function-calling format)
    r = tools.call("get_kpi", {"metric": "revenue", "period": "2026-Q3", "segment": "Industrials"})
    r["rows"], r["sql"], r["error"]           # rows, the exact SQL that ran ("show your work"), error text

Arguments go to the warehouse as bound parameters (never pasted into the SQL), so the model cannot inject SQL.
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import datetime as dt
import decimal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import config  # noqa: E402

MAX_ROWS = 200
PERIOD = "YYYY-MM (e.g. 2026-09), YYYY-Qn (e.g. 2026-Q3), MTD or QTD (to the last day of data)"
SEGMENT = ("ALL, a client segment (Large Corporate, Mid-Market, Financial Institutions, Sponsor), "
           "an industry (e.g. Industrials, Energy, Technology) or a coverage team (e.g. Coverage East 2)")

# name -> (description, [(param, sql_type, json_schema, required, default)])
TOOLS: dict[str, tuple[str, list[tuple[str, str, dict, bool, Any]]]] = {
    "get_kpi": (
        "One KPI for one period and one slice, with plan, prior period and the variances. Metrics: revenue (CAD), "
        "raroc (decimal, trailing 12 complete months), win_rate (decimal), weighted_pipeline (CAD), products_per_client. "
        "Use for 'how did X do', 'how is this month tracking', 'what is our RAROC'.",
        [("metric", "STRING", {"type": "string", "enum": ["revenue", "raroc", "win_rate", "weighted_pipeline", "products_per_client"]}, True, None),
         ("period", "STRING", {"type": "string", "description": PERIOD}, True, None),
         ("segment", "STRING", {"type": "string", "description": SEGMENT}, False, "ALL")]),
    "variance_drivers": (
        "Explains WHY revenue differs from plan, the prior period or the prior year: splits the gap by product, client, "
        "banker, team, segment, industry or region and ranks the drivers with their share of the total gap.",
        [("metric", "STRING", {"type": "string", "enum": ["revenue"]}, True, None),
         ("period", "STRING", {"type": "string", "description": PERIOD}, True, None),
         ("compare_to", "STRING", {"type": "string", "enum": ["plan", "prior_period", "prior_year"]}, False, "plan"),
         ("dimension", "STRING", {"type": "string", "enum": ["product", "client", "banker", "team", "segment", "industry", "region"]}, False, "product"),
         ("segment", "STRING", {"type": "string", "description": "Filter. " + SEGMENT}, False, "ALL"),
         ("top_n", "INT", {"type": "integer", "minimum": 1, "maximum": 50}, False, 10)]),
    "pipeline_conversion": (
        "What happened to the deals EXPECTED to close in a period: won_on_time, lost, slipped (closed late or still open) "
        "and not_yet_due. Use detail='slipped' to list the slipped deals (client, product, fee, dates, stage now).",
        [("period", "STRING", {"type": "string", "description": PERIOD}, True, None),
         ("segment", "STRING", {"type": "string", "description": SEGMENT}, False, "ALL"),
         ("detail", "STRING", {"type": "string", "enum": ["summary", "slipped", "deals"]}, False, "summary")]),
    "cross_sell_whitespace": (
        "Clients whose trailing 12-month RAROC is below a threshold (default: the 15% hurdle), with the products they hold "
        "and the products they don't: the cross-sell opportunity. Lowest RAROC first.",
        [("segment", "STRING", {"type": "string", "description": SEGMENT}, False, "Mid-Market"),
         ("max_raroc", "DOUBLE", {"type": ["number", "null"], "description": "Decimal, e.g. 0.15. Null = the hurdle rate"}, False, None),
         ("period", "STRING", {"type": "string", "description": "LATEST (last complete month), YYYY-MM or YYYY-Qn: end of the 12-month window"}, False, "LATEST")]),
    "coverage_capacity": (
        "Coverage capacity and productivity by team: clients per banker vs the team target, revenue per banker and meetings "
        "per client (trailing 12 months), and the win rate for each of the last complete quarters.",
        [("team", "STRING", {"type": "string", "description": "ALL, Coverage East 1, Coverage East 2, Coverage West, Coverage US or Coverage FI & Sponsors"}, False, "ALL"),
         ("quarters", "INT", {"type": "integer", "minimum": 1, "maximum": 8}, False, 4)]),
    "recon_breaks": (
        "Checks that booked revenue reconciles to the general ledger by product and month. Returns each break over the "
        "$1,000 tolerance (delta = GL - booked; positive means revenue missing from the books) or a single no_breaks row.",
        [("period", "STRING", {"type": "string", "description": "ALL or " + PERIOD}, False, "ALL")]),
}


def openai_tools() -> list[dict]:
    """Tool schemas in the OpenAI / Databricks function-calling format."""
    out = []
    for name, (desc, params) in TOOLS.items():
        props = {p: dict(schema) for p, _, schema, _, _ in params}
        for p, _, _, _, default in params:
            if default is not None:
                props[p]["description"] = (props[p].get("description", "") + f" Default: {default}.").strip()
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": [p for p, _, _, req, _ in params if req], "additionalProperties": False}}})
    return out


def _literal(value: Any, sql_type: str) -> str:
    if value is None:
        return f"CAST(NULL AS {sql_type})"
    if sql_type in ("INT", "DOUBLE"):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build(name: str, args: dict | None) -> tuple[str, dict, str, list[str]]:
    """Returns (sql with :markers, bound params, display SQL with literals, notes). Raises ValueError on bad input."""
    if name not in TOOLS:
        raise ValueError(f"Unknown tool {name!r}. Tools: {', '.join(TOOLS)}")
    args = dict(args or {})
    _, params = TOOLS[name]
    names = [p for p, *_ in params]
    notes = [f"ignored unknown argument {k!r}" for k in args if k not in names]
    values, markers, shown = {}, [], []
    for p, sql_type, _, required, default in params:
        v = args.get(p, default)
        if v is None and required:
            raise ValueError(f"{name}: argument {p!r} is required")
        if v is not None:
            try:
                v = int(v) if sql_type == "INT" else float(v) if sql_type == "DOUBLE" else str(v).strip()
            except (TypeError, ValueError):
                raise ValueError(f"{name}: argument {p!r} must be {sql_type.lower()}, got {v!r}") from None
            if sql_type == "STRING" and len(v) > 100:
                raise ValueError(f"{name}: argument {p!r} is too long")
        values[f"p_{p}"] = v
        markers.append(f"CAST(:p_{p} AS {sql_type})")
        shown.append(_literal(v, sql_type))
    fn = f"{config.catalog()}.gold.{name}"
    return (f"SELECT * FROM {fn}({', '.join(markers)})", values,
            f"SELECT * FROM {fn}({', '.join(shown)})", notes)


def _clean(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    return v


def call(name: str, args: dict | None = None) -> dict:
    """Runs one tool. Never raises: problems come back in 'error' so the agent can fix its arguments."""
    from common import dbx

    result = {"tool": name, "args": args or {}, "sql": None, "columns": [], "rows": [], "row_count": 0,
              "truncated": False, "error": None, "notes": [], "seconds": None}
    try:
        sql, params, shown, notes = build(name, args)
        result.update(sql=shown, notes=notes)
    except ValueError as ex:
        result["error"] = str(ex)
        return result
    t0 = time.time()
    try:
        rows = dbx.sql(sql, params)
    except Exception as ex:  # warehouse or permission problem
        result["error"] = f"query failed: {str(ex)[:300]}"
        result["seconds"] = round(time.time() - t0, 2)
        return result
    result["seconds"] = round(time.time() - t0, 2)
    rows = [{k: _clean(v) for k, v in r.items()} for r in rows]
    errors = sorted({r["error"] for r in rows if r.get("error")})
    if errors:  # the function validated the input and said what was wrong
        result["error"] = "; ".join(errors)
        rows = [r for r in rows if not r.get("error")]
    result["columns"] = list(rows[0].keys()) if rows else []
    result["row_count"] = len(rows)
    result["truncated"] = len(rows) > MAX_ROWS
    result["rows"] = rows[:MAX_ROWS]
    return result


if __name__ == "__main__":  # python agent/tools.py get_kpi metric=revenue period=2026-Q3 segment=Industrials
    import json

    if len(sys.argv) < 2:
        sys.exit("usage: python agent/tools.py <tool> key=value ...   tools: " + ", ".join(TOOLS))
    kv = dict(a.split("=", 1) for a in sys.argv[2:])
    print(json.dumps(call(sys.argv[1], kv), indent=2, default=str))
