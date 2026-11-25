# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Checks
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Compares gold with `expected_answers.json` (written by the generator) and checks freshness, keys and data quality.
# MAGIC - **FAIL** stops the job (the task turns red): wrong story numbers, an unexpected reconciliation break, an unexpected
# MAGIC   data-quality failure, orphan keys, or data more than 3 days old.
# MAGIC - **WARN** is printed but the job stays green: the planted Transaction Banking break (it's meant to be there; the SOP covers it)
# MAGIC   and data that is 1–3 days behind.
# MAGIC
# MAGIC Results are appended to `gold.pipeline_checks`.

# COMMAND ----------

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "cib_demo")
dbutils.widgets.text("raw_path", "")
CAT = dbutils.widgets.get("catalog")
RAW = dbutils.widgets.get("raw_path") or f"/Volumes/{CAT}/bronze/raw"
G = f"{CAT}.gold"
RUN_TS = datetime.now(timezone.utc).replace(tzinfo=None)

with open(f"{RAW}/expected_answers.json", encoding="utf-8") as f:
    E = json.load(f)
cfg = {r.key: r.value for r in spark.table(f"{G}.config").collect()}
results: list[tuple[str, str, str]] = []   # (check, status, detail)


def check(name: str, ok: bool, detail: str, warn_only: bool = False) -> None:
    results.append((name, "PASS" if ok else ("WARN" if warn_only else "FAIL"), detail))


def one(sql: str):
    return spark.sql(sql).first()

# COMMAND ----------

# MAGIC %md ## Tables and keys

# COMMAND ----------

for t in ["dim_client", "dim_banker", "dim_product", "dim_month", "fact_revenue", "fact_plan", "fact_capital",
          "fact_coverage", "fact_pipeline", "recon_control", "dq_summary"]:
    n = spark.table(f"{G}.{t}").count()
    check(f"rows: gold.{t}", n > 0, f"{n:,} rows")

for fact, col, dim in [("fact_revenue", "client_id", "dim_client"), ("fact_revenue", "product_id", "dim_product"),
                       ("fact_plan", "client_id", "dim_client"), ("fact_capital", "client_id", "dim_client"),
                       ("fact_coverage", "banker_id", "dim_banker"), ("fact_pipeline", "client_id", "dim_client"),
                       ("fact_pipeline", "banker_id", "dim_banker")]:
    orphans = one(f"SELECT COUNT(*) n FROM {G}.{fact} f LEFT ANTI JOIN {G}.{dim} d USING ({col})").n
    check(f"keys: {fact}.{col} → {dim}", orphans == 0, f"{orphans} orphan rows")

dups = one(f"SELECT COUNT(*) - COUNT(DISTINCT deal_id) n FROM {G}.fact_pipeline").n
check("keys: deal_id unique in fact_pipeline", dups == 0, f"{dups} duplicates")

# COMMAND ----------

# MAGIC %md ## Freshness

# COMMAND ----------

as_of = datetime.fromisoformat(cfg["as_of_date"]).date()
yesterday = (datetime.now(ZoneInfo("America/Toronto")) - timedelta(days=1)).date()
lag = (yesterday - as_of).days
check("freshness: as_of_date is yesterday (Toronto)", lag <= 0, f"as_of_date {as_of}, yesterday {yesterday}")
if lag > 0:
    check("freshness: no more than 3 days behind", lag <= 3, f"{lag} day(s) behind", warn_only=lag <= 3)

# COMMAND ----------

# MAGIC %md ## The four planted stories

# COMMAND ----------

# Story 1: Industrials misses plan in Q3 2026
s1 = E["story1_industrials_q3"]
r = one(f"""
WITH a AS (SELECT SUM(r.revenue) v FROM {G}.fact_revenue r JOIN {G}.dim_client c USING (client_id) JOIN {G}.dim_month m USING (month_start)
           WHERE c.industry = 'Industrials' AND m.quarter = '{s1["quarter"]}'),
     p AS (SELECT SUM(p.plan_revenue) v FROM {G}.fact_plan p JOIN {G}.dim_client c USING (client_id) JOIN {G}.dim_month m USING (month_start)
           WHERE c.industry = 'Industrials' AND m.quarter = '{s1["quarter"]}')
SELECT a.v - p.v AS variance FROM a, p""")
check("story 1: Industrials Q3 2026 revenue vs plan", abs(r.variance - s1["variance"]) < 1,
      f"{r.variance:,.2f} vs expected {s1['variance']:,.2f}")

ma = one(f"""SELECT SUM(r.revenue) actual FROM {G}.fact_revenue r JOIN {G}.dim_client c USING (client_id) JOIN {G}.dim_month m USING (month_start)
             WHERE c.industry = 'Industrials' AND m.quarter = '{s1["quarter"]}' AND r.product_id = 'P02'""")
check("story 1: no Industrials M&A revenue in Q3 2026", (ma.actual or 0) == 0, f"M&A actual {ma.actual or 0:,.0f}")

slipped = [d["deal_id"] for d in s1["slipped_deals"]]
rows = spark.table(f"{G}.fact_pipeline").filter(F.col("deal_id").isin(slipped)).collect()
ok = len(rows) == len(slipped) and all(str(x.expected_close_quarter) == s1["quarter"] and
                                       (x.status == "Open" or str(x.close_quarter) > s1["quarter"]) for x in rows)
check("story 1: the two mandates slipped out of Q3 2026", ok,
      "; ".join(f"{x.deal_id} {x.status} {x.actual_close_date}" for x in rows))

# Story 2: lending-only Mid-Market clients below the hurdle (trailing 12 months to Sep 2026)
s2 = E["story2_whitespace"]
hurdle, tax = float(cfg["hurdle_rate"]), float(cfg["tax_rate"])
ttm_end = s2["as_of_month"] + "-01"
r = one(f"""
WITH win AS (SELECT month_start FROM {G}.dim_month WHERE month_start BETWEEN add_months(DATE'{ttm_end}', -11) AND DATE'{ttm_end}'),
     rev AS (SELECT client_id, SUM(revenue) revenue FROM {G}.fact_revenue JOIN win USING (month_start) GROUP BY client_id),
     cap AS (SELECT client_id, AVG(allocated_capital) cap, SUM(expected_loss) el, SUM(cost_allocation) cost
             FROM {G}.fact_capital JOIN win USING (month_start) GROUP BY client_id),
     k AS (SELECT c.client_id, c.segment, (COALESCE(rev.revenue, 0) - cap.cost - cap.el) * (1 - {tax}) / cap.cap AS raroc
           FROM cap JOIN {G}.dim_client c USING (client_id) LEFT JOIN rev USING (client_id))
SELECT SUM(CASE WHEN segment = 'Mid-Market' AND raroc < {hurdle} THEN 1 ELSE 0 END) AS mm_below,
       SUM(CASE WHEN raroc < {hurdle} THEN 1 ELSE 0 END) AS all_below,
       AVG(CASE WHEN segment = 'Mid-Market' AND raroc < {hurdle} THEN raroc END) AS avg_raroc
FROM k""")
check("story 2: Mid-Market clients below the hurdle", r.mm_below == s2["count"] and r.all_below == s2["count"],
      f"{r.mm_below} Mid-Market ({r.all_below} in all segments), average RAROC {r.avg_raroc:.2%}; expected {s2['count']}")

# Story 3: Coverage East 2 capacity and win rate
s3 = next(t for t in E["story3_capacity"]["teams"] if t["team"] == E["story3_capacity"]["team"])
cpb = one(f"""SELECT COUNT(DISTINCT c.client_id) / COUNT(DISTINCT b.banker_id) AS cpb
              FROM {G}.dim_banker b LEFT JOIN {G}.dim_client c ON c.team = b.team WHERE b.team = '{s3["team"]}'""").cpb
check("story 3: clients per banker", abs(cpb - s3["clients_per_banker"]) < 0.01, f"{cpb:.2f} vs target {s3['capacity_target']}")
wr = {x.close_quarter: x.win_rate for x in spark.sql(f"""
    SELECT p.close_quarter, AVG(CASE WHEN p.status = 'Won' THEN 1.0 ELSE 0.0 END) AS win_rate
    FROM {G}.fact_pipeline p JOIN {G}.dim_banker b USING (banker_id)
    WHERE b.team = '{s3["team"]}' AND p.status IN ('Won', 'Lost') GROUP BY p.close_quarter""").collect()}
ok = all(v is None or abs(float(wr.get(qq, -1)) - v) < 0.001 for qq, v in s3["win_rate_by_quarter"].items())
check("story 3: win rate by quarter", ok, ", ".join(f"{qq} {float(wr.get(qq, 0)):.0%}" for qq in s3["win_rate_by_quarter"]))

# COMMAND ----------

# MAGIC %md ## Reconciliation and data quality

# COMMAND ----------

expected_breaks = {(b["product_id"], b["month"]): b["delta"] for b in E["story4_recon"]["breaks"]}
found = {(x.product_id, str(x.month_start)[:7]): x.delta for x in spark.table(f"{G}.recon_control").filter("is_break").collect()}
unexpected = {k: v for k, v in found.items() if k not in expected_breaks}
check("recon: no unexpected breaks", not unexpected, ", ".join(f"{k[0]} {k[1]} {v:+,.2f}" for k, v in unexpected.items()) or "none")
for k, want in expected_breaks.items():
    got = found.get(k)
    check(f"recon: planted break {k[0]} {k[1]} still flagged", got is not None and abs(got - want) < 1,
          f"delta {got if got is None else f'{got:+,.2f}'} vs {want:+,.2f}")
    if got is not None:
        check(f"recon: open break {k[0]} {k[1]} (planted, see the DQ-break SOP)", False, f"delta {got:+,.2f}", warn_only=True)

dq_expected = {("coverage", "not_null_banker_id"): E["data_quality"]["null_banker_id_rows_in_coverage"],
               ("pipeline", "unique_deal_id"): E["data_quality"]["duplicate_deal_rows"],
               ("pipeline", "fee_non_negative"): len(E["data_quality"]["negative_fee_deal_ids"])}
dq = {(x.table_name, x.rule): x.rows_failed for x in spark.table(f"{G}.dq_summary").collect()}
for k, want in dq_expected.items():
    check(f"dq: planted defect {k[0]}.{k[1]} caught", dq.get(k) == want, f"{dq.get(k)} rows vs {want} planted")
other = {k: v for k, v in dq.items() if v and k not in dq_expected}
check("dq: no unexpected failures", not other, ", ".join(f"{k[0]}.{k[1]}={v}" for k, v in other.items()) or "none")

# COMMAND ----------

out = spark.createDataFrame(results, "check STRING, status STRING, detail STRING").withColumn("run_ts", F.lit(RUN_TS).cast("timestamp"))
out.write.mode("append").saveAsTable(f"{G}.pipeline_checks")
if not os.environ.get("NORTHLAKE_LOCAL_TEST"):
    spark.sql(f"COMMENT ON TABLE {G}.pipeline_checks IS 'Results of 04_checks for every pipeline run: check, PASS/WARN/FAIL, detail, run_ts (UTC).'")
display(out)

failed = [r for r in results if r[1] == "FAIL"]
warned = [r for r in results if r[1] == "WARN"]
print(f"{len(results) - len(failed) - len(warned)} PASS · {len(warned)} WARN · {len(failed)} FAIL")
for r in warned:
    print(f"WARN  {r[0]}: {r[2]}")
if failed:
    raise AssertionError("Checks failed:\n" + "\n".join(f"FAIL  {r[0]}: {r[2]}" for r in failed))
