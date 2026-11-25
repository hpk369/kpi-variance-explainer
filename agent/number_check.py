"""Number check: every figure in a draft answer must match a value in the tool results.

    from agent.number_check import check
    result = check(draft_text, tool_results, extra_texts=[question, glossary_text])
    result["passed"], result["unmatched"], result["checked"]

How a figure matches:
- "$8.8M", "$8.8 million", "$8,800,000" → 8,800,000; "$450K" → 450,000; "$1.2B" → 1,200,000,000
- "85%" → 0.85 (tool rates are decimals); "6–9%" → 6% and 9%
- A match is within the display rounding of the figure (8.8M covers 8.75M–8.85M) or 0.5% of the tool value
- Signs are ignored ("a miss of $8.8M" matches -8,799,999.98)
- Allowed values: every number in every tool row, each column's sum, mean, min and max, row counts,
  counts of rows sharing a value (18 rows with segment = Mid-Market),
  plus numbers in the question and in the glossary definitions passed as extra_texts
Not checked: dates, years, quarters (2026-Q3, Q3), months, IDs (C-1001, D-20458, P06), names that contain a digit
(Coverage East 2), list numbering and plain whole numbers from 0 to 12 without a unit (counts like "two mandates").
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

SMALL_INT = 12
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
         "b": 1e9, "bn": 1e9, "billion": 1e9}
MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

_MASKS = [
    r"\b\d{4}-\d{2}-\d{2}\b",                              # 2026-08-20
    r"\b\d{4}-(?:Q[1-4]|\d{2})\b",                         # 2026-Q3, 2026-09
    rf"\b{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b",   # Aug 20, November 12, 2026
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}\b(?:\s+\d{{4}})?",       # 12 November 2026
    rf"\b{MONTHS}\.?\s+\d{{4}}\b",                          # June 2026
    r"\b(?:FY|F)?Q[1-4](?:\s*(?:FY)?\s*'?\d{2,4})?\b",     # Q3, Q3 2026, FQ4
    r"\bFY\s?'?\d{2,4}\b", r"\bH[12]\b",
    r"\b[A-Z]{1,3}-\d+\b", r"\bP0\d\b",                    # C-1001, D-20458, P06
    r"(?<![\d$.,])\b(?:19|20)\d{2}\b(?![\d.,]*\s*(?:%|[kKmMbB]\b|bn|mn|million|billion|thousand))",  # bare years
    r"^\s*\d+[.)]\s",                                       # numbered list markers
    r"\b(?:top|last|first|past|next)\s+\d+\b",              # top 5, last 4 quarters
]

_NUM = re.compile(
    r"(?P<cur>C?\$|CAD\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>%|pp\b|bps\b|[kK]\b|thousand\b|mm\b|MM\b|mn\b|[mM]\b|million\b|bn\b|[bB]\b|billion\b|x\b))?"
)
# Models often write non-breaking hyphens (2026‑11‑24), minus signs and narrow spaces: make them plain first
_NORMALISE = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2043": "-", "\u2212": "-",
                            "\ufe63": "-", "\uff0d": "-", "\u00a0": " ", "\u2007": " ", "\u2009": " ",
                            "\u202f": " ", "\u200a": " "})
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s?(?:[–—-]|to)\s?(\d+(?:\.\d+)?)\s?(%|[kKmMbB]\b|million|billion)")


def _numbers_in(obj: Any) -> Iterable[float]:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            yield float(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _numbers_in(v)


def _names_with_digits(tool_results: list[dict]) -> set[str]:
    names = set()
    for r in tool_results:
        for row in r.get("rows", []):
            for v in row.values():
                if isinstance(v, str) and re.search(r"\d", v) and re.search(r"[A-Za-z]", v) and len(v) <= 80:
                    names.add(v)
    return names


def allowed_values(tool_results: list[dict], extra_texts: Iterable[str] = ()) -> list[float]:
    """Every number a draft may use: tool row values, per-column aggregates, row counts, numbers in extra texts."""
    vals: list[float] = []
    for r in tool_results:
        rows = r.get("rows", [])
        vals.extend(_numbers_in(rows))
        vals.append(float(len(rows)))
        cols: dict[str, list[float]] = {}
        for row in rows:
            for k, v in row.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None and math.isfinite(v):
                    cols.setdefault(k, []).append(float(v))
        for xs in cols.values():
            if len(xs) > 1:
                vals += [sum(xs), sum(xs) / len(xs), min(xs), max(xs)]
        groups: dict[tuple, int] = {}       # how many rows share a value, e.g. 18 rows with segment = Mid-Market
        for row in rows:
            for k, v in row.items():
                if isinstance(v, (str, bool)):
                    groups[(k, v)] = groups.get((k, v), 0) + 1
        vals += [float(n) for n in groups.values()]
    for t in extra_texts:
        for f in extract(t or ""):
            vals.extend(f["values"])
    return [abs(v) for v in vals]


def extract(text: str, names: Iterable[str] = (), keep_small: bool = False) -> list[dict]:
    """Figures in a text: [{'text': '$8.8M', 'values': [8800000.0], 'tol': 50000.0}, ...]."""
    t = text.translate(_NORMALISE)
    for n in sorted(names, key=len, reverse=True):
        t = t.replace(n, " ")
    t = _RANGE.sub(lambda m: f"{m.group(1)}{m.group(3)} to {m.group(2)}{m.group(3)}", t)
    for pat in _MASKS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE | re.MULTILINE)
    out = []
    for m in _NUM.finditer(t):
        raw, unit, cur = m.group("num"), (m.group("unit") or "").strip(), m.group("cur")
        prev = t[max(0, m.start() - 1):m.start()]
        if prev.isalpha() or prev in ("_", "/"):         # part of a word or code: e.g. B01, gte-large-en
            continue
        num = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        half_unit = 0.5 * 10 ** -decimals
        u = unit.lower()
        if u in ("%", "pp"):
            values, tol = [num / 100], half_unit / 100
        elif u == "bps":
            values, tol = [num / 10000], half_unit / 10000
        elif u in SCALE:
            values, tol = [num * SCALE[u]], half_unit * SCALE[u]
        elif u == "x":
            values, tol = [num], half_unit
        else:
            if not cur and decimals == 0 and num <= SMALL_INT and not keep_small:
                continue                                    # small counts are not checked
            values, tol = [num], half_unit
        out.append({"text": m.group(0).strip(), "values": values, "tol": tol})
    return out


def _matches(fig: dict, allowed: list[float]) -> bool:
    for v in fig["values"]:
        v = abs(v)
        for a in allowed:
            if abs(v - a) <= max(fig["tol"] + 1e-9, 0.005 * a):
                return True
    return False


def check(draft: str, tool_results: list[dict], extra_texts: Iterable[str] = ()) -> dict:
    """{'passed', 'unmatched': [figure texts], 'checked': n, 'figures': [...]}"""
    allowed = allowed_values(tool_results, extra_texts)
    figures = extract(draft, _names_with_digits(tool_results))
    unmatched = [f["text"] for f in figures if not _matches(f, allowed)]
    return {"passed": not unmatched, "unmatched": list(dict.fromkeys(unmatched)), "checked": len(figures)}


def inject_wrong_number(text: str, allowed: list[float] | None = None) -> str:
    """For the blocked-answer demo: makes the first money figure wrong, choosing a multiplier whose result is NOT
    in the allowed values (a random multiple can land on a real figure, e.g. a prior-period variance)."""
    m = re.search(r"\$(\d+(?:\.\d+)?)\s?([KMB])\b", text)
    if m:
        unit = {"K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]
        for factor in (1.37, 1.53, 1.71, 1.89, 2.37, 3.11, 4.73, 7.9):
            val = round(float(m.group(1)) * factor, 1)
            fig = {"values": [val * unit], "tol": 0.05 * unit}
            if allowed is None or not _matches(fig, allowed):
                return text[:m.start()] + f"${val:.1f}{m.group(2)}" + text[m.end():]
    wrong = "Revenue reached $987.6M."
    return text.replace("**Headline:**", f"**Headline:** {wrong}", 1) if "**Headline:**" in text else text + f"\n\n{wrong}"
