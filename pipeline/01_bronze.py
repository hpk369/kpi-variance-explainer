# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Bronze
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Loads the files in the volume into `bronze.*` tables as they are, adding `_source_file` and `_ingest_ts`.
# MAGIC - Monthly history (Oct 2024 → Sep 2026): one file per table, e.g. `fact_revenue.parquet` → `bronze.fact_revenue`
# MAGIC - Daily feed (Oct 2026 → yesterday): `feed/<table>/*.parquet` → `bronze.feed_<table>`
# MAGIC
# MAGIC Every run reloads everything (the data is small), so a re-run never double-counts.

# COMMAND ----------

import os

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "cib_demo")
dbutils.widgets.text("raw_path", "")
CAT = dbutils.widgets.get("catalog")
RAW = dbutils.widgets.get("raw_path") or f"/Volumes/{CAT}/bronze/raw"

HISTORY = ["dim_client", "dim_banker", "dim_product", "dim_month", "fact_revenue", "fact_plan",
           "fact_capital", "fact_pipeline", "fact_coverage", "gl_control"]

# Fixed schemas for the feed (same as generator/daily_feed.py SCHEMAS), so an empty feed still gives typed tables.
FEED = {
    "revenue_daily": "client_id STRING, product_id STRING, revenue_date DATE, revenue DOUBLE, fees DOUBLE, nii DOUBLE",
    "pipeline_new": "deal_id STRING, deal_name STRING, client_id STRING, product_id STRING, banker_id STRING, stage STRING, "
                    "status STRING, probability DOUBLE, expected_fee DOUBLE, actual_fee DOUBLE, open_date DATE, pitch_date DATE, "
                    "mandate_date DATE, execution_date DATE, expected_close_date DATE, actual_close_date DATE",
    "pipeline_events": "deal_id STRING, event_date DATE, event_type STRING, new_stage STRING, actual_fee DOUBLE",
    "coverage_daily": "banker_id STRING, client_id STRING, activity_date DATE, meetings BIGINT, calls BIGINT",
    "gl_control_daily": "product_id STRING, control_date DATE, control_total DOUBLE, source_system STRING",
    "fact_capital": "client_id STRING, month_start DATE, allocated_capital DOUBLE, expected_loss DOUBLE, cost_allocation DOUBLE",
}


def save(df, table: str) -> int:
    (df.withColumn("_source_file", F.col("_metadata.file_path"))
       .withColumn("_ingest_ts", F.current_timestamp())
       .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CAT}.bronze.{table}"))
    return spark.table(f"{CAT}.bronze.{table}").count()

# COMMAND ----------

loaded = []
for name in HISTORY:
    path = f"{RAW}/{name}.parquet"
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} is missing. Upload generator/out/ to the volume (BUILD.md step 6).")
    loaded.append((f"bronze.{name}", save(spark.read.parquet(path), name)))

for name, ddl in FEED.items():
    folder = f"{RAW}/feed/{name}"
    files = [f for f in os.listdir(folder) if f.endswith(".parquet")] if os.path.isdir(folder) else []
    if files:
        df = spark.read.schema(ddl).parquet(f"{folder}/*.parquet")
        loaded.append((f"bronze.feed_{name}", save(df, f"feed_{name}")))
    else:  # no feed yet: create the empty table so silver still runs
        (spark.createDataFrame([], ddl + ", _source_file STRING, _ingest_ts TIMESTAMP")
              .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CAT}.bronze.feed_{name}"))
        loaded.append((f"bronze.feed_{name}", 0))

display(spark.createDataFrame(loaded, "table STRING, row_count BIGINT"))
