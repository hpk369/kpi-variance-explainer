# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Daily synthetic feed
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Writes every missing business day from `dates.feed_start` (Oct 1, 2026) up to yesterday (Toronto time) into the volume,
# MAGIC under `feed/<table>/<date>.parquet`. The first run backfills; later runs add one day. Re-running a day gives the same rows.
# MAGIC Also writes `bronze.run_config`, the numbers from `config/constants.yaml` that later tasks need.

# COMMAND ----------

# MAGIC %pip install -q pyyaml

# COMMAND ----------

import os
import sys
from datetime import date

dbutils.widgets.text("catalog", "cib_demo")
dbutils.widgets.text("raw_path", "")    # default: /Volumes/<catalog>/bronze/raw
dbutils.widgets.text("repo_root", "")   # default: the folder above this notebook
dbutils.widgets.text("until", "")       # default: yesterday, Toronto time (YYYY-MM-DD to override)

CAT = dbutils.widgets.get("catalog")
RAW = dbutils.widgets.get("raw_path") or f"/Volumes/{CAT}/bronze/raw"
REPO = dbutils.widgets.get("repo_root") or os.path.dirname(os.getcwd())
UNTIL = dbutils.widgets.get("until")

if not os.path.isdir(os.path.join(REPO, "generator")):
    raise RuntimeError(f"generator/ not found under {REPO}. Run scripts/push_workspace.sh, or set the repo_root parameter.")
if not os.path.isdir(os.path.join(RAW, "_internal")):
    raise RuntimeError(f"{RAW}/_internal not found. Upload generator/out/ to the volume first (BUILD.md step 6).")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from common.config import constants  # noqa: E402
from generator.daily_feed import Feed, yesterday_toronto  # noqa: E402

C = constants()
until = date.fromisoformat(UNTIL) if UNTIL else yesterday_toronto()
print(f"Catalog {CAT} · volume {RAW} · feed from {C['dates']['feed_start']} to {until}")

# COMMAND ----------

written = Feed(RAW, C).run(until=until)
print(f"{len(written)} new day(s) written")

# COMMAND ----------

# Numbers later tasks and the governed tools need, so nothing downstream has to parse YAML.
rb = C["stories"]["recon_break"]
config_rows = [
    ("constants_version", str(C["version"]), "Version of config/constants.yaml"),
    ("bank", C["bank"], "Fictional bank name"),
    ("currency", C["currency"], "Reporting currency"),
    ("hurdle_rate", str(C["raroc"]["hurdle_rate"]), "RAROC hurdle rate (decimal)"),
    ("tax_rate", str(C["raroc"]["tax_rate"]), "Tax rate used in RAROC (decimal)"),
    ("history_start", C["dates"]["history_start"], "First month of monthly history"),
    ("history_end", C["dates"]["history_end"], "Last day of the monthly history (planted stories)"),
    ("feed_start", C["dates"]["feed_start"], "First day of the daily feed"),
    ("plan_end", C["dates"]["plan_end"], "Plan figures exist up to this date"),
    ("recon_tolerance", "1000", "Reconciliation tolerance in CAD; a larger gap is a break"),
    ("known_break_product_id", rb["product_id"], "Planted reconciliation break (story 4): product"),
    ("known_break_month", rb["month"], "Planted reconciliation break (story 4): month"),
    ("feed_until", str(until), "Last feed day requested in this run"),
]
(spark.createDataFrame(config_rows, "key STRING, value STRING, description STRING")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CAT}.bronze.run_config"))
dbutils.jobs.taskValues.set(key="feed_until", value=str(until))
display(spark.table(f"{CAT}.bronze.run_config"))
