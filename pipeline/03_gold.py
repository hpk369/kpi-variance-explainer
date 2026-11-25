# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Gold
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Star schema for the agent's governed tools and the Power BI model, plus:
# MAGIC - `gold.recon_control`: GL control totals vs booked revenue by product and month ($1k tolerance)
# MAGIC - `gold.dq_summary`: this run's data-quality results
# MAGIC - `gold.config`: hurdle rate, tax rate and key dates, from `config/constants.yaml`
# MAGIC - `gold.v_pbi_meta`: `as_of_date` (last day of data) and when the pipeline last ran
# MAGIC
# MAGIC Every table and column gets a Unity Catalog comment, so reviewers and the agent can read what it means.

# COMMAND ----------

import os
from datetime import datetime, timezone

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "cib_demo")
CAT = dbutils.widgets.get("catalog")
B, S, G = f"{CAT}.bronze", f"{CAT}.silver", f"{CAT}.gold"
RUN_TS = datetime.now(timezone.utc).replace(tzinfo=None)
APPLY_COMMENTS = not os.environ.get("NORTHLAKE_LOCAL_TEST")  # comments need Unity Catalog

cfg = {r.key: r.value for r in spark.table(f"{B}.run_config").collect()}
as_of = spark.table(f"{S}.revenue_daily").agg(F.max("revenue_date")).first()[0]
as_of = as_of or datetime.fromisoformat(cfg["history_end"]).date()
print(f"as_of_date = {as_of}")


def save(df, name: str) -> None:
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{G}.{name}")

# COMMAND ----------

# MAGIC %md ## Dimensions and facts

# COMMAND ----------

save(spark.table(f"{S}.dim_client"), "dim_client")
save(spark.table(f"{S}.dim_banker"), "dim_banker")
save(spark.table(f"{S}.dim_product"), "dim_product")
save(spark.table(f"{S}.dim_month")
     .withColumn("is_complete", F.col("month_end") <= F.lit(as_of))
     .withColumn("is_current", (F.col("month_start") <= F.lit(as_of)) & (F.col("month_end") >= F.lit(as_of))), "dim_month")

save(spark.table(f"{S}.revenue"), "fact_revenue")
save(spark.table(f"{S}.revenue_daily"), "fact_revenue_daily")
save(spark.table(f"{S}.plan"), "fact_plan")
save(spark.table(f"{S}.capital"), "fact_capital")
save(spark.table(f"{S}.coverage"), "fact_coverage")
save(spark.table(f"{S}.pipeline")
     .withColumn("weighted_fee", F.when(F.col("status") == "Open", F.col("expected_fee") * F.col("probability")).otherwise(F.lit(0.0)))
     .withColumn("close_quarter", F.when(F.col("actual_close_date").isNotNull(),
                                         F.concat(F.year("actual_close_date"), F.lit("-Q"), F.quarter("actual_close_date"))))
     .withColumn("expected_close_quarter", F.concat(F.year("expected_close_date"), F.lit("-Q"), F.quarter("expected_close_date"))),
     "fact_pipeline")

# COMMAND ----------

# MAGIC %md ## Reconciliation, data quality, config, run metadata

# COMMAND ----------

tolerance = float(cfg.get("recon_tolerance", 1000))
save(spark.sql(f"""
SELECT g.product_id, p.product_name, g.month_start,
       ROUND(g.control_total, 2)                                   AS control_total,
       ROUND(COALESCE(f.fact_total, 0), 2)                         AS fact_total,
       ROUND(g.control_total - COALESCE(f.fact_total, 0), 2)       AS delta,
       ABS(g.control_total - COALESCE(f.fact_total, 0)) > {tolerance} AS is_break,
       CAST({tolerance} AS DOUBLE)                                 AS tolerance
FROM {S}.gl_control g
LEFT JOIN (SELECT product_id, month_start, SUM(revenue) AS fact_total FROM {G}.fact_revenue GROUP BY product_id, month_start) f
  ON f.product_id = g.product_id AND f.month_start = g.month_start
LEFT JOIN {G}.dim_product p ON p.product_id = g.product_id
"""), "recon_control")

save(spark.table(f"{S}.dq_results")
     .withColumn("failure_rate", F.round(F.col("rows_failed") / F.greatest(F.col("rows_checked"), F.lit(1)), 4))
     .select("table_name", "rule", "rows_checked", "rows_failed", "failure_rate", "run_ts"), "dq_summary")

save(spark.table(f"{B}.run_config").filter("key <> 'feed_until'")
     .unionByName(spark.createDataFrame([("as_of_date", str(as_of), "Last day with data (the daily feed runs to yesterday)")],
                                        "key STRING, value STRING, description STRING")), "config")

runs = spark.createDataFrame(
    [(RUN_TS, as_of, spark.table(f"{G}.fact_revenue_daily").select("revenue_date").distinct().count(),
      spark.table(f"{G}.recon_control").filter("is_break").count(),
      int(spark.table(f"{G}.dq_summary").agg(F.sum("rows_failed")).first()[0] or 0))],
    "run_ts TIMESTAMP, as_of_date DATE, feed_days BIGINT, recon_breaks BIGINT, dq_rows_failed BIGINT")
runs.write.mode("append").saveAsTable(f"{G}.pipeline_runs")

spark.sql(f"""
CREATE OR REPLACE VIEW {G}.v_pbi_meta
COMMENT 'Last day of data (as_of_date) and when the pipeline last ran. Used by the Power BI banner.'
AS
SELECT r.as_of_date,
       r.run_ts                                                        AS refreshed_at_utc,
       CAST((SELECT value FROM {G}.config WHERE key = 'history_end') AS DATE) AS history_end,
       (SELECT value FROM {G}.config WHERE key = 'bank')               AS bank,
       (SELECT value FROM {G}.config WHERE key = 'currency')           AS currency,
       'Northlake Bank is fictional. Every client, deal and figure in this demo is synthetic.' AS data_note
FROM {G}.pipeline_runs r
ORDER BY r.run_ts DESC
LIMIT 1
""")

# COMMAND ----------

# MAGIC %md ## Unity Catalog comments

# COMMAND ----------

TABLES = {
    "dim_client": ("One row per corporate client of Northlake Bank (fictional).", {
        "client_id": "Client key, e.g. C-1001", "client_name": "Client name (fictional)",
        "segment": "Large Corporate, Mid-Market, Financial Institutions or Sponsor", "industry": "Industry of the client",
        "internal_rating": "Internal risk rating, 1 (best) to 8", "team": "Coverage team that owns the relationship",
        "region": "Client region", "coverage_banker_id": "Lead coverage banker (dim_banker.banker_id)",
        "relationship_start": "Date the relationship started"}),
    "dim_banker": ("One row per coverage banker.", {
        "banker_id": "Banker key, e.g. B01", "banker_name": "Banker name (fictional)", "team": "Coverage team",
        "role": "Banker title", "capacity_target": "Target number of clients per banker for the team"}),
    "dim_product": ("The six CIB products.", {
        "product_id": "Product key P01–P06", "product_name": "Product name",
        "revenue_type": "Recurring (accrues monthly) or Event (fee at deal close)",
        "capital_intensity": "High, Medium or Low use of capital", "description": "What the product is"}),
    "dim_month": ("Calendar months from Oct 2024 to Dec 2027 (plan horizon). Fiscal year starts in November.", {
        "month_start": "First day of the month", "month_end": "Last day of the month", "month_label": "e.g. Sep 2026",
        "quarter": "Calendar quarter, e.g. 2026-Q3", "fiscal_year": "Fiscal year (Nov–Oct), named by the year it ends",
        "fiscal_quarter": "Fiscal quarter, e.g. FY2026-Q4", "is_history": "True up to Sep 2026 (the monthly history)",
        "is_complete": "True when the whole month is in the data", "is_current": "True for the month that contains as_of_date"}),
    "fact_revenue": ("Booked revenue by client, product and month (CAD). Monthly history to Sep 2026, then the daily feed summed by month; the current month is month-to-date.", {
        "client_id": "dim_client.client_id", "product_id": "dim_product.product_id", "month_start": "dim_month.month_start",
        "revenue": "Total revenue = fees + nii (CAD)", "fees": "Fee revenue (CAD)", "nii": "Net interest income (CAD)",
        "last_revenue_date": "Last day included (month end for history, latest feed day for the current month)",
        "source": "history (monthly file) or feed (daily feed)"}),
    "fact_revenue_daily": ("Daily booked revenue from the synthetic feed (Oct 2026 onward), for month-to-date answers.", {
        "revenue_date": "Business date", "revenue": "Revenue that day (CAD)", "fees": "Fees that day (CAD)", "nii": "NII that day (CAD)"}),
    "fact_plan": ("Revenue plan (budget) by client, product and month, to Dec 2027 (CAD).", {
        "plan_revenue": "Planned revenue for the month (CAD)"}),
    "fact_capital": ("RAROC inputs by client and month (CAD). RAROC = (revenue − cost_allocation − expected_loss) × (1 − tax) ÷ average allocated capital, trailing 12 months.", {
        "allocated_capital": "Economic capital allocated to the client (CAD)", "expected_loss": "Expected credit loss for the month (CAD)",
        "cost_allocation": "Operating cost allocated to the client for the month (CAD)"}),
    "fact_coverage": ("Coverage activity by banker, client and month.", {
        "banker_id": "dim_banker.banker_id", "meetings": "Client meetings in the month", "calls": "Client calls in the month"}),
    "fact_pipeline": ("Current state of every deal: the snapshot at Sep 30, 2026 plus stage moves, wins and losses from the daily feed.", {
        "deal_id": "Deal key", "deal_name": "Product · client", "banker_id": "Banker who owns the deal",
        "stage": "Origination, Pitch, Mandate, Execution, Closed Won or Closed Lost", "status": "Open, Won or Lost",
        "probability": "Win probability by stage (Origination 10%, Pitch 25%, Mandate 60%, Execution 85%)",
        "expected_fee": "Fee expected if won (CAD)", "actual_fee": "Fee booked when won (CAD)",
        "weighted_fee": "expected_fee × probability for open deals, else 0", "open_date": "Date the deal was opened",
        "expected_close_date": "Planned close date", "actual_close_date": "Date the deal was won or lost",
        "close_quarter": "Quarter of actual_close_date", "expected_close_quarter": "Quarter of expected_close_date",
        "last_event_date": "Last feed event for the deal"}),
    "recon_control": ("Reconciliation of GL control totals to booked revenue by product and month. is_break is true when the gap is over the tolerance; the planted break is Transaction Banking, Jun 2026.", {
        "control_total": "GL control total (CAD)", "fact_total": "Sum of fact_revenue (CAD)",
        "delta": "control_total − fact_total (CAD); positive means revenue is missing from the books",
        "is_break": "True when |delta| > tolerance", "tolerance": "Tolerance in CAD"}),
    "dq_summary": ("Data-quality results of the latest pipeline run: one row per table and rule.", {
        "table_name": "Silver table the rule ran on", "rule": "Rule name", "rows_checked": "Rows checked",
        "rows_failed": "Rows that failed (sent to silver.dq_quarantine)", "failure_rate": "rows_failed ÷ rows_checked",
        "run_ts": "When the run happened (UTC)"}),
    "config": ("Settings shared by the pipeline, the agent's tools and Power BI (from config/constants.yaml).", {
        "key": "Setting name", "value": "Setting value (text)", "description": "What it means"}),
    "pipeline_runs": ("One row per pipeline run.", {
        "run_ts": "Run time (UTC)", "as_of_date": "Last day with data", "feed_days": "Days in the daily feed",
        "recon_breaks": "Reconciliation breaks found", "dq_rows_failed": "Rows quarantined by data-quality rules"}),
}


def q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


if APPLY_COMMENTS:
    for table, (comment, cols) in TABLES.items():
        spark.sql(f"COMMENT ON TABLE {G}.{table} IS '{q(comment)}'")
        existing = set(spark.table(f"{G}.{table}").columns)
        for col, text in cols.items():
            if col in existing:
                spark.sql(f"ALTER TABLE {G}.{table} ALTER COLUMN {col} COMMENT '{q(text)}'")

display(spark.table(f"{G}.v_pbi_meta"))
