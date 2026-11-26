"""agent/number_check.py: formats, rounding tolerance, what is ignored, and planted mismatches."""
import pytest

from agent.number_check import allowed_values, check, extract, inject_wrong_number

TOOLS = [
    {"rows": [{"actual": 38036512.05, "plan": 46836512.03, "var_vs_plan": -8799999.98, "pct_vs_plan": -0.1879,
               "as_of_date": "2026-11-23"}]},
    {"rows": [{"dimension_value": "M&A Advisory", "delta": -7500000.0, "share_of_total_delta": 0.8523},
              {"dimension_value": "Lending", "delta": -1279359.87, "share_of_total_delta": 0.1454}]},
    {"rows": [{"team": "Coverage East 2", "clients_per_banker": 14.0, "capacity_target": 10, "win_rate": 0.4375},
              {"team": "Coverage East 2", "clients_per_banker": 14.0, "capacity_target": 10, "win_rate": 0.25}]},
    {"rows": [{"client_name": "Northwind Midstream (fictional)", "raroc": 0.0606},
              {"client_name": "Rosehill Power (fictional)", "raroc": 0.0894}]},
]


def figs(text):
    return [f["text"] for f in extract(text)]


@pytest.mark.parametrize("text, value", [
    ("$8.8M", 8.8e6), ("$8.8 million", 8.8e6), ("$8,799,999.98", 8799999.98), ("$450K", 450e3),
    ("C$1.2B", 1.2e9), ("85%", 0.85), ("−18.8%", 0.188), ("14.0", 14.0), ("150 bps", 0.015),
])
def test_formats(text, value):
    (f,) = extract(f"The figure was {text} in total.")
    assert f["values"][0] == pytest.approx(value)


def test_rounded_display_values_match():
    draft = ("Industrials missed plan by $8.8M (18.8%); actual $38.0M vs plan $46.8M. M&A Advisory −$7.5M (85%), "
             "Lending −$1.3M (15%). Coverage East 2 runs 14 clients per banker vs a target of 10; win rate 44% → 25%. "
             "RAROC 6–9%. Roughly $9M in total.")
    r = check(draft, TOOLS)
    assert r["passed"], r["unmatched"]
    assert r["checked"] >= 12


def test_dates_ids_years_and_names_are_ignored():
    text = ("As of 2026-11-23 (Nov 23, 2026), Q3 2026 and 2026-Q3, deal D-20458 for C-1001 (P02), "
            "expected Aug 20 and now November 19; Coverage East 2 and banker B01; top 5 clients in FY2027.")
    assert check(text, TOOLS) == {"passed": True, "unmatched": [], "checked": 0}


def test_small_counts_are_not_checked_but_units_are():
    assert check("Two mandates, 3 deals and 5 bankers.", TOOLS)["passed"]
    assert not check("3% of revenue", TOOLS)["passed"]


def test_planted_mismatches_are_caught():
    for wrong in ["$9.4M", "$8.6M", "91%", "$46.0M", "17 clients per banker"]:
        r = check(f"Revenue fell {wrong}.", TOOLS)
        assert not r["passed"] and r["unmatched"], wrong


def test_aggregates_and_extra_texts_are_allowed():
    assert check("Average RAROC 7.5%.", TOOLS)["passed"]                     # mean of 6.06% and 8.94%
    assert not check("The hurdle is 12%.", TOOLS)["passed"]
    assert check("The hurdle is 12%.", TOOLS, ["Hurdle rate: the hurdle rate is 12%."])["passed"]
    assert 2.0 in allowed_values(TOOLS)                                       # row counts


def test_inject_wrong_number_always_fails():
    good = "**Headline:** Industrials missed plan by $8.8M."
    bad = inject_wrong_number(good)
    assert bad != good and "$12.1M" in bad
    assert check(good, TOOLS)["passed"] and not check(bad, TOOLS)["passed"]
    assert not check(inject_wrong_number("**Headline:** no money here"), TOOLS)["passed"]


def test_counts_of_rows_sharing_a_value_are_allowed():
    rows = [{"segment": "Mid-Market", "raroc": 0.06 + i / 1000} for i in range(18)] + \
           [{"segment": "Sponsor", "raroc": 0.11}, {"segment": "Large Corporate", "raroc": 0.12}]
    assert check("20 clients are below the hurdle; 18 of them are Mid-Market.", [{"rows": rows}])["passed"]
    assert not check("19 of them are Mid-Market.", [{"rows": rows}])["passed"]


def test_injected_number_never_lands_on_a_real_figure():
    tools = [{"rows": [{"var_vs_plan": -8799999.98, "var_vs_prior": -12087967.89}]}]   # 8.8 x 1.37 = 12.1 is real
    text = "**Headline:** Industrials missed plan by $8.8M."
    bad = inject_wrong_number(text, allowed_values(tools))
    assert "$12.1M" not in bad and not check(bad, tools)["passed"]


def test_unicode_hyphens_and_spaces_in_dates_are_ignored():
    # gpt-oss writes 2026‑11‑24 with non-breaking hyphens (U+2011) and narrow spaces
    text = "Data as of 2026‑11‑24 (Nov 24, 2026); miss −$8.8M; range 6–9%."
    assert [f["text"] for f in extract(text)] == ["$8.8M", "6%", "9%"]
