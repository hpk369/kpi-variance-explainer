"""Recorded answers for when the live backend is down, and the rules for deciding that it is.

cache.json is written by space/build_cache.py from answers that passed the evaluation checks. The Space switches to
it automatically on an auth error (401/403), a quota or "compute unavailable" error, a timeout, or when the
FORCE_CACHE secret is 1. Only the 4 suggested questions have recorded answers; anything else gets a polite
"live mode is off". Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from functools import lru_cache
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "cache.json"

# Error text that means "the backend can't answer right now" (as opposed to a bad question)
_DOWN = re.compile(
    r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid (?:access )?token|token (?:is )?(?:expired|invalid|revoked)|"
    r"permission[_ ]denied|credential|\b429\b|request_limit_exceeded|rate limit|quota|resource_exhausted|"
    r"compute (?:is )?unavailable|temporarily unavailable|service unavailable|\b50[234]\b|warehouse .*(?:stopped|not running|starting)|"
    r"connection (?:error|refused|reset|aborted)|failed to establish|max retries exceeded|name or service not known|"
    r"timed? ?out|read timeout|missing setting databricks|failed on every route",
    re.I)


def force_cache() -> bool:
    return os.environ.get("FORCE_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


def backend_down(error: str | None) -> bool:
    return bool(error) and bool(_DOWN.search(error))


def work_backend_error(work: dict) -> str | None:
    """A finished agent run whose tool calls couldn't reach the warehouse: the answer can't be trusted as live."""
    for c in work.get("tool_calls") or []:
        err = c.get("error") or ""
        if err.startswith("query failed") and backend_down(err):
            return err
    return None


def normalise(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (question or "").lower()).strip()


@lru_cache(maxsize=1)
def _load(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load(path: Path | None = None) -> dict | None:
    path = path or CACHE
    if not path.exists():
        return None
    try:
        return _load(str(path), path.stat().st_mtime)
    except (OSError, ValueError):
        return None


def lookup(question: str, path: Path | None = None) -> dict | None:
    cache = load(path)
    if not cache:
        return None
    key = normalise(question)
    return next((e for e in cache.get("questions", []) if normalise(e["question"]) == key), None)


def recorded_on(entry: dict) -> str:
    try:
        d = dt.datetime.fromisoformat(entry.get("recorded_at", ""))
        return d.strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return "an earlier run"
