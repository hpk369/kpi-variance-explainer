"""Run a .sql file on the Databricks SQL warehouse from .env.

Usage:  python scripts/run_sql.py setup/01_create_catalog.sql
        python scripts/run_sql.py pipeline/tests.sql

- ${catalog} in the file is replaced with the catalog from config/constants.yaml.
- Statements end with a semicolon at the end of a line.
- Rows returned by a statement are printed (test files return PASS/FAIL rows).
- Exit code 1 if any statement fails or any row has a column `result` = 'FAIL'.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import config, dbx  # noqa: E402


def statements(text: str) -> list[str]:
    text = text.replace("${catalog}", config.catalog())
    parts = re.split(r";[ \t]*(?:--[^\n]*)?\n", text + "\n")
    out = []
    for p in parts:
        body = "\n".join(l for l in p.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            out.append(p.strip().rstrip(";"))
    return out


def main(path: str) -> int:
    stmts = statements(Path(path).read_text(encoding="utf-8"))
    print(f"{path}: {len(stmts)} statement(s) on catalog {config.catalog()}\n")
    failed = 0
    for i, s in enumerate(stmts, 1):
        first = next(l for l in s.splitlines() if l.strip() and not l.strip().startswith("--")).strip()
        try:
            rows = dbx.sql(s)
            print(f"[{i}] OK   {first[:90]}")
            for r in rows[:50]:
                print("       " + " | ".join(f"{k}={v}" for k, v in r.items()))
                if str(r.get("result", "")).upper() == "FAIL":
                    failed += 1
            if len(rows) > 50:
                print(f"       … {len(rows) - 50} more rows")
        except Exception as ex:
            failed += 1
            print(f"[{i}] FAIL {first[:90]}\n       {str(ex)[:400]}")
    print(f"\n{'All good' if not failed else f'{failed} problem(s)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
