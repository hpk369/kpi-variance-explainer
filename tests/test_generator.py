"""Fast tests for the synthetic data generator and the daily feed (no Databricks needed)."""
from datetime import date

import pytest

from common.config import constants
from generator.daily_feed import Feed
from generator.generate import build, write


@pytest.fixture(scope="module")
def result():
    return build()


def test_story1_industrials_miss(result):
    s1 = result["expected"]["story1_industrials_q3"]
    st = constants()["stories"]["industrials_q3"]
    fees = sum(m["fee"] for m in st["mandates"])
    assert s1["variance"] == pytest.approx(-(fees + st["other_products_shortfall"]), abs=1000)
    assert 0.80 <= s1["ma_share_of_variance"] <= 0.90
    assert len(s1["slipped_deals"]) == 2


def test_story2_whitespace(result):
    s2 = result["expected"]["story2_whitespace"]
    assert s2["count"] == constants()["stories"]["whitespace"]["lending_only_mid_market"]
    assert s2["clients_below_hurdle_all_segments"] == s2["count"]
    assert all(c["products_held"] == ["Lending"] for c in s2["clients"])


def test_story3_capacity(result):
    teams = {t["team"]: t for t in result["expected"]["story3_capacity"]["teams"]}
    e2 = teams["Coverage East 2"]
    assert e2["over_capacity"] and e2["clients_per_banker"] > e2["capacity_target"]
    wr = list(e2["win_rate_by_quarter"].values())
    assert wr[0] > wr[-1] and wr[-1] <= 0.30


def test_story4_one_break(result):
    breaks = result["expected"]["story4_recon"]["breaks"]
    assert len(breaks) == 1 and breaks[0]["product_id"] == "P06" and breaks[0]["month"] == "2026-06"


def test_build_is_deterministic(result):
    again = build()
    assert again["expected"]["headline"] == result["expected"]["headline"]


def test_feed_is_deterministic_and_closes_mandates(result, tmp_path):
    write(result, tmp_path)
    feed = Feed(tmp_path)
    a = feed.day_tables(date(2026, 11, 12))
    b = Feed(tmp_path).day_tables(date(2026, 11, 12))
    assert a["revenue_daily"].revenue.round(2).tolist() == b["revenue_daily"].revenue.round(2).tolist()
    won = a["pipeline_events"][a["pipeline_events"].event_type == "won"].actual_fee.tolist()
    assert 4_500_000 in won
    assert a["gl_control_daily"].control_total.sum() == pytest.approx(a["revenue_daily"].revenue.sum(), abs=1)
