# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Silver
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Types and de-duplicates bronze, runs the data-quality rules, and joins the monthly history with the daily feed.
# MAGIC Rows that fail a rule go to `silver.dq_quarantine` with the rule name; the counts go to `silver.dq_results`.
# MAGIC
# MAGIC | Rule | Tables | Action |
# MAGIC |---|---|---|
# MAGIC | keys not null | all | quarantine |
# MAGIC | foreign key exists (client, banker, product, deal) | facts | quarantine |
# MAGIC | no duplicate natural key | all | keep the latest, quarantine the rest |
# MAGIC | fee / revenue ≥ 0 | revenue, pipeline | quarantine |
# MAGIC | probability between 0 and 1 | pipeline | quarantine |
# MAGIC | close date ≥ open date | pipeline | quarantine |
# MAGIC
# MAGIC The generator plants three defects that must show up here: ~1% null `banker_id` in coverage, 3 duplicate deals, 1 negative fee.

# COMMAND ----------

from datetime import datetime, timezone

from pyspark.sql import Window
from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "cib_demo")
CAT = dbutils.widgets.get("catalog")
B, S = f"{CAT}.bronze", f"{CAT}.silver"
RUN_TS = datetime.now(timezone.utc).replace(tzinfo=None)
META = ["_source_file", "_ingest_ts"]

dq_results: list[tuple] = []   # (table_name, rule, rows_checked, rows_failed)
quarantine = []                # DataFrames: table_name, rule, record, _source_file


def bronze(name: str):
    return spark.table(f"{B}.{name}")


def add_fk(df, col: str, dim: str, key: str):
    """Adds boolean column _fk_<col>: True when df.<col> exists in silver.<dim>.<key>."""
    keys = spark.table(f"{S}.{dim}").select(F.col(key).alias(f"_k_{col}")).distinct().withColumn(f"_fk_{col}", F.lit(True))
    return df.join(F.broadcast(keys), df[col] == keys[f"_k_{col}"], "left").drop(f"_k_{col}")


def dedupe(df, table: str, keys: list[str], rule: str):
    """Keeps the latest row per natural key (by _ingest_ts, then _source_file); quarantines the others."""
    w = Window.partitionBy(*keys).orderBy(F.col("_ingest_ts").desc(), F.col("_source_file").desc())
    ranked = df.withColumn("_rn", F.row_number().over(w)).cache()
    dropped = ranked.filter("_rn > 1").drop("_rn")
    n_total, n_dropped = ranked.count(), dropped.count()
    dq_results.append((table, rule, n_total, n_dropped))
    if n_dropped:
        data_cols = [c for c in dropped.columns if c not in META]
        quarantine.append(dropped.select(F.lit(table).alias("table_name"), F.lit(rule).alias("rule"),
                                         F.to_json(F.struct(*data_cols)).alias("record"), "_source_file"))
    return ranked.filter("_rn = 1").drop("_rn")


def apply_rules(df, table: str, rules: list[tuple[str, str]]):
    """rules: (rule_name, SQL condition that is TRUE for a good row). NULL counts as a failure.
    Returns the good rows without helper columns. Failed rows go to quarantine, counts to dq_results."""
    flags = [F.when(F.coalesce(F.expr(cond), F.lit(False)), F.lit(None)).otherwise(F.lit(name)) for name, cond in rules]
    checked = df.withColumn("_failed", F.filter(F.array(*flags), lambda x: x.isNotNull())).cache()
    counts = checked.agg(F.count("*").alias("n"),
                         *[F.sum(F.array_contains("_failed", name).cast("int")).alias(name) for name, _ in rules]).first()
    for name, _ in rules:
        dq_results.append((table, name, counts["n"], int(counts[name] or 0)))
    data_cols = [c for c in df.columns if not c.startswith("_")]
    bad = checked.filter(F.size("_failed") > 0)
    if bad.limit(1).count():
        quarantine.append(bad.select(F.lit(table).alias("table_name"), F.array_join("_failed", ", ").alias("rule"),
                                     F.to_json(F.struct(*data_cols)).alias("record"),
                                     F.col("_source_file") if "_source_file" in df.columns else F.lit(None).cast("string")))
    keep = [c for c in df.columns if not c.startswith("_fk_")]
    return checked.filter(F.size("_failed") == 0).select(*keep)


def write(df, name: str) -> None:
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{S}.{name}")

# COMMAND ----------

# MAGIC %md ## Dimensions

# COMMAND ----------

banker = dedupe(bronze("dim_banker"), "dim_banker", ["banker_id"], "unique_banker_id")
banker = apply_rules(banker, "dim_banker", [("not_null_banker_id", "banker_id IS NOT NULL")])
write(banker.select("banker_id", "banker_name", "team", "role", F.col("capacity_target").cast("int")), "dim_banker")

product = dedupe(bronze("dim_product"), "dim_product", ["product_id"], "unique_product_id")
product = apply_rules(product, "dim_product", [("not_null_product_id", "product_id IS NOT NULL")])
write(product.select("product_id", "product_name", "revenue_type", "capital_intensity", "description"), "dim_product")

client = dedupe(bronze("dim_client"), "dim_client", ["client_id"], "unique_client_id")
client = add_fk(client, "coverage_banker_id", "dim_banker", "banker_id")
client = apply_rules(client, "dim_client", [
    ("not_null_client_id", "client_id IS NOT NULL"),
    ("fk_coverage_banker_id", "coverage_banker_id IS NULL OR _fk_coverage_banker_id"),
])
write(client.select("client_id", "client_name", "segment", "industry", F.col("internal_rating").cast("int"), "team", "region",
                    "coverage_banker_id", F.to_date("relationship_start").alias("relationship_start")), "dim_client")

month = dedupe(bronze("dim_month"), "dim_month", ["month_start"], "unique_month_start")
month = apply_rules(month, "dim_month", [("not_null_month_start", "month_start IS NOT NULL")])
write(month.select(F.to_date("month_start").alias("month_start"), F.to_date("month_end").alias("month_end"),
                   F.col("year").cast("int"), F.col("month").cast("int"), "month_label", "quarter",
                   F.col("fiscal_year").cast("int"), "fiscal_quarter", "is_history"), "dim_month")

# COMMAND ----------

# MAGIC %md ## Revenue (monthly history + daily feed)

# COMMAND ----------

def fact_rules(df, table: str, keys: list[str], extra: list[tuple[str, str]] = ()):
    rules = [(f"not_null_{k}", f"{k} IS NOT NULL") for k in keys]
    for col, dim, key in [("client_id", "dim_client", "client_id"), ("product_id", "dim_product", "product_id"),
                          ("banker_id", "dim_banker", "banker_id")]:
        if col in df.columns:
            df = add_fk(df, col, dim, key)
            rules.append((f"fk_{col}", f"{col} IS NULL OR _fk_{col}"))
    return apply_rules(df, table, rules + list(extra))


rev_daily = bronze("feed_revenue_daily")
rev_daily = dedupe(rev_daily, "revenue_daily", ["client_id", "product_id", "revenue_date"], "unique_client_product_day")
rev_daily = fact_rules(rev_daily, "revenue_daily", ["client_id", "product_id", "revenue_date"],
                       [("revenue_non_negative", "revenue >= 0")])
rev_daily = rev_daily.select("client_id", "product_id", "revenue_date", "revenue", "fees", "nii")
write(rev_daily, "revenue_daily")

rev_hist = bronze("fact_revenue").withColumn("month_start", F.to_date("month_start"))
rev_hist = dedupe(rev_hist, "revenue", ["client_id", "product_id", "month_start"], "unique_client_product_month")
rev_hist = fact_rules(rev_hist, "revenue", ["client_id", "product_id", "month_start"], [("revenue_non_negative", "revenue >= 0")])

rev_feed = (spark.table(f"{S}.revenue_daily")
            .groupBy("client_id", "product_id", F.trunc("revenue_date", "month").alias("month_start"))
            .agg(*[F.round(F.sum(c), 2).alias(c) for c in ["revenue", "fees", "nii"]],
                 F.max("revenue_date").alias("last_revenue_date"))
            .withColumn("source", F.lit("feed")))
revenue = (rev_hist.select("client_id", "product_id", "month_start", "revenue", "fees", "nii",
                           F.last_day("month_start").alias("last_revenue_date"), F.lit("history").alias("source"))
           .unionByName(rev_feed))
write(revenue, "revenue")

# COMMAND ----------

# MAGIC %md ## Plan, capital, coverage, GL control

# COMMAND ----------

plan = bronze("fact_plan").withColumn("month_start", F.to_date("month_start"))
plan = dedupe(plan, "plan", ["client_id", "product_id", "month_start"], "unique_client_product_month")
plan = fact_rules(plan, "plan", ["client_id", "product_id", "month_start"], [("plan_non_negative", "plan_revenue >= 0")])
write(plan.select("client_id", "product_id", "month_start", "plan_revenue"), "plan")

capital = (bronze("fact_capital").withColumn("month_start", F.to_date("month_start"))
           .unionByName(bronze("feed_fact_capital").withColumn("month_start", F.to_date("month_start"))))
capital = dedupe(capital, "capital", ["client_id", "month_start"], "unique_client_month")
capital = fact_rules(capital, "capital", ["client_id", "month_start"], [
    ("capital_positive", "allocated_capital > 0"),
    ("expected_loss_non_negative", "expected_loss >= 0"),
    ("cost_non_negative", "cost_allocation >= 0"),
])
write(capital.select("client_id", "month_start", "allocated_capital", "expected_loss", "cost_allocation"), "capital")

cov_hist = bronze("fact_coverage").withColumn("month_start", F.to_date("month_start"))
cov_feed = (bronze("feed_coverage_daily")
            .groupBy("banker_id", "client_id", F.trunc("activity_date", "month").alias("month_start"))
            .agg(F.sum("meetings").alias("meetings"), F.sum("calls").alias("calls"),
                 F.max("_source_file").alias("_source_file"), F.max("_ingest_ts").alias("_ingest_ts")))
coverage = cov_hist.select("banker_id", "client_id", "month_start", "meetings", "calls", *META).unionByName(cov_feed)
coverage = fact_rules(coverage, "coverage", ["banker_id", "client_id", "month_start"],
                      [("activity_non_negative", "meetings >= 0 AND calls >= 0")])
coverage = dedupe(coverage, "coverage", ["banker_id", "client_id", "month_start"], "unique_banker_client_month")
write(coverage.select("banker_id", "client_id", "month_start", F.col("meetings").cast("int"), F.col("calls").cast("int")), "coverage")

gl_daily = bronze("feed_gl_control_daily")
gl_daily = dedupe(gl_daily, "gl_control_daily", ["product_id", "control_date"], "unique_product_day")
gl_daily = fact_rules(gl_daily, "gl_control_daily", ["product_id", "control_date"])
write(gl_daily.select("product_id", "control_date", "control_total", "source_system"), "gl_control_daily")

gl_hist = bronze("gl_control").withColumn("month_start", F.to_date("month_start"))
gl_hist = dedupe(gl_hist, "gl_control", ["product_id", "month_start"], "unique_product_month")
gl_hist = fact_rules(gl_hist, "gl_control", ["product_id", "month_start"])
gl_feed = (spark.table(f"{S}.gl_control_daily")
           .groupBy("product_id", F.trunc("control_date", "month").alias("month_start"))
           .agg(F.round(F.sum("control_total"), 2).alias("control_total"), F.first("source_system").alias("source_system")))
write(gl_hist.select("product_id", "month_start", "control_total", "source_system").unionByName(gl_feed), "gl_control")

# COMMAND ----------

# MAGIC %md ## Pipeline (snapshot at history end + deals and events from the feed)

# COMMAND ----------

PIPE_COLS = ["deal_id", "deal_name", "client_id", "product_id", "banker_id", "stage", "status", "probability", "expected_fee",
             "actual_fee", "open_date", "pitch_date", "mandate_date", "execution_date", "expected_close_date", "actual_close_date"]
dates = ["open_date", "pitch_date", "mandate_date", "execution_date", "expected_close_date", "actual_close_date"]
pipe = (bronze("fact_pipeline").select(*[F.to_date(c).alias(c) if c in dates else F.col(c) for c in PIPE_COLS], *META)
        .unionByName(bronze("feed_pipeline_new").select(*PIPE_COLS, *META)))
pipe = dedupe(pipe, "pipeline", ["deal_id"], "unique_deal_id")
pipe = fact_rules(pipe, "pipeline", ["deal_id", "client_id", "product_id", "banker_id"], [
    ("fee_non_negative", "expected_fee >= 0 AND (actual_fee IS NULL OR actual_fee >= 0)"),
    ("probability_0_to_1", "probability BETWEEN 0 AND 1"),
    ("close_after_open", "expected_close_date >= open_date AND (actual_close_date IS NULL OR actual_close_date >= open_date)"),
    ("valid_status", "status IN ('Open', 'Won', 'Lost')"),
])
pipe.select(*PIPE_COLS).createOrReplaceTempView("pipe_base")

events = bronze("feed_pipeline_events")
events = dedupe(events, "pipeline_events", ["deal_id", "event_date", "event_type", "new_stage"], "unique_event")
known = pipe.select(F.col("deal_id").alias("_k_deal_id")).withColumn("_fk_deal_id", F.lit(True))
events = events.join(known, events.deal_id == known._k_deal_id, "left").drop("_k_deal_id")
events = apply_rules(events, "pipeline_events", [
    ("not_null_deal_id", "deal_id IS NOT NULL"),
    ("not_null_event_date", "event_date IS NOT NULL"),
    ("fk_deal_id", "deal_id IS NULL OR _fk_deal_id"),
    ("valid_event_type", "event_type IN ('stage', 'won', 'lost')"),
    ("fee_non_negative", "actual_fee IS NULL OR actual_fee >= 0"),
])
events = events.select("deal_id", "event_date", "event_type", "new_stage", "actual_fee")
write(events, "pipeline_events")

# Current state = snapshot + events. Stage follows the latest stage date reached; won/lost closes the deal.
current = spark.sql(f"""
WITH ev AS (
  SELECT deal_id,
         MIN(CASE WHEN event_type = 'stage' AND new_stage = 'Pitch'     THEN event_date END) AS ev_pitch,
         MIN(CASE WHEN event_type = 'stage' AND new_stage = 'Mandate'   THEN event_date END) AS ev_mandate,
         MIN(CASE WHEN event_type = 'stage' AND new_stage = 'Execution' THEN event_date END) AS ev_execution,
         MAX(CASE WHEN event_type IN ('won', 'lost') THEN event_date END)                   AS ev_close,
         MAX(CASE WHEN event_type = 'won' THEN 'Won' WHEN event_type = 'lost' THEN 'Lost' END) AS ev_status,
         MAX(CASE WHEN event_type = 'won' THEN actual_fee END)                               AS ev_fee,
         MAX(event_date)                                                                     AS last_event_date
  FROM {S}.pipeline_events GROUP BY deal_id
), merged AS (
  SELECT p.deal_id, p.deal_name, p.client_id, p.product_id, p.banker_id, p.expected_fee,
         p.open_date,
         COALESCE(p.pitch_date, e.ev_pitch)         AS pitch_date,
         COALESCE(p.mandate_date, e.ev_mandate)     AS mandate_date,
         COALESCE(p.execution_date, e.ev_execution) AS execution_date,
         p.expected_close_date,
         COALESCE(p.actual_close_date, e.ev_close)  AS actual_close_date,
         CASE WHEN p.status <> 'Open' THEN p.status ELSE COALESCE(e.ev_status, 'Open') END AS status,
         COALESCE(p.actual_fee, e.ev_fee)           AS actual_fee,
         e.last_event_date
  FROM pipe_base p LEFT JOIN ev e USING (deal_id)
)
SELECT deal_id, deal_name, client_id, product_id, banker_id,
       CASE WHEN status = 'Won' THEN 'Closed Won' WHEN status = 'Lost' THEN 'Closed Lost'
            WHEN execution_date IS NOT NULL THEN 'Execution' WHEN mandate_date IS NOT NULL THEN 'Mandate'
            WHEN pitch_date IS NOT NULL THEN 'Pitch' ELSE 'Origination' END AS stage,
       status,
       CAST(CASE WHEN status = 'Won' THEN 1.0 WHEN status = 'Lost' THEN 0.0
                 WHEN execution_date IS NOT NULL THEN 0.85 WHEN mandate_date IS NOT NULL THEN 0.60
                 WHEN pitch_date IS NOT NULL THEN 0.25 ELSE 0.10 END AS DOUBLE) AS probability,
       expected_fee, CASE WHEN status = 'Won' THEN actual_fee END AS actual_fee,
       open_date, pitch_date, mandate_date, execution_date, expected_close_date, actual_close_date, last_event_date
FROM merged
""")
write(current, "pipeline")

# COMMAND ----------

# MAGIC %md ## Data-quality results

# COMMAND ----------

results = (spark.createDataFrame(dq_results, "table_name STRING, rule STRING, rows_checked BIGINT, rows_failed BIGINT")
           .withColumn("run_ts", F.lit(RUN_TS).cast("timestamp")))
write(results, "dq_results")
results.write.mode("append").saveAsTable(f"{S}.dq_history")

q_schema = "table_name STRING, rule STRING, record STRING, _source_file STRING"
q = spark.createDataFrame([], q_schema)
for part in quarantine:
    q = q.unionByName(part.select("table_name", "rule", "record", F.col("_source_file").cast("string")))
write(q.withColumn("run_ts", F.lit(RUN_TS).cast("timestamp")), "dq_quarantine")

display(spark.table(f"{S}.dq_results").filter("rows_failed > 0").orderBy(F.desc("rows_failed")))
