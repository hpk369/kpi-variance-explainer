"""agent/tools.py without Databricks: schemas, argument checks and SQL building."""
import pytest

from agent import tools


def test_six_tools_with_schemas():
    specs = tools.openai_tools()
    assert [s["function"]["name"] for s in specs] == ["get_kpi", "variance_drivers", "pipeline_conversion",
                                                       "cross_sell_whitespace", "coverage_capacity", "recon_breaks"]
    kpi = specs[0]["function"]["parameters"]
    assert kpi["required"] == ["metric", "period"] and "revenue" in kpi["properties"]["metric"]["enum"]


def test_build_binds_parameters_and_shows_literal_sql():
    sql, params, shown, notes = tools.build("get_kpi", {"metric": "revenue", "period": "2026-Q3", "segment": "Industrials"})
    assert sql == ("SELECT * FROM cib_demo.gold.get_kpi(CAST(:p_metric AS STRING), CAST(:p_period AS STRING), "
                   "CAST(:p_segment AS STRING))")
    assert params == {"p_metric": "revenue", "p_period": "2026-Q3", "p_segment": "Industrials"}
    assert shown == "SELECT * FROM cib_demo.gold.get_kpi('revenue', '2026-Q3', 'Industrials')"
    assert notes == []


def test_defaults_types_and_injection_safety():
    sql, params, shown, _ = tools.build("variance_drivers", {"metric": "revenue", "period": "2026-Q3", "top_n": "5",
                                                             "segment": "x'); DROP TABLE t; --"})
    assert params["p_top_n"] == 5 and params["p_compare_to"] == "plan" and params["p_dimension"] == "product"
    assert "DROP" not in sql                       # the value is bound, never pasted into the SQL
    assert "x\\'); DROP TABLE t; --'" in shown     # display copy is escaped
    _, p2, shown2, _ = tools.build("cross_sell_whitespace", {})
    assert p2["p_max_raroc"] is None and "CAST(NULL AS DOUBLE)" in shown2


def test_bad_input_is_reported_not_raised():
    with pytest.raises(ValueError):
        tools.build("drop_tables", {})
    with pytest.raises(ValueError):
        tools.build("get_kpi", {"metric": "revenue"})          # period missing
    with pytest.raises(ValueError):
        tools.build("coverage_capacity", {"quarters": "many"})
    r = tools.call("get_kpi", {"metric": "revenue"})           # call() never raises
    assert r["error"] and r["rows"] == []
    _, _, _, notes = tools.build("recon_breaks", {"period": "ALL", "colour": "red"})
    assert notes == ["ignored unknown argument 'colour'"]
