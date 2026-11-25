# Databricks notebook source
# MAGIC %md
# MAGIC # Run the agent in Databricks (MLflow traces)
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Runs the six Gate 6 questions plus the blocked-number test. Every run is traced to the MLflow experiment
# MAGIC `/Users/<you>/northlake-cib-agent`: open **Experiments → northlake-cib-agent → Traces** and click a trace to see the spans
# MAGIC `plan`, `tool:<name>`, `retrieve`, `draft` and `number_check` (and each LLM call inside them).

# COMMAND ----------

# MAGIC %pip install -q "langgraph>=0.3" "openai>=1.40" "databricks-sql-connector>=3.4" "pyyaml>=6" "mlflow>=3.1"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import os
import sys

dbutils.widgets.text("repo_root", "")      # default: the folder above this notebook
dbutils.widgets.text("warehouse_id", "")   # default: first running SQL warehouse
REPO = dbutils.widgets.get("repo_root") or os.path.dirname(os.getcwd())
if not os.path.isfile(os.path.join(REPO, "agent", "graph.py")):
    raise RuntimeError(f"agent/graph.py not found under {REPO}. Run scripts/push_workspace.sh, or set repo_root.")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agent.dbx_env import configure  # noqa: E402

info = configure(dbutils.widgets.get("warehouse_id"))

import mlflow  # noqa: E402

EXPERIMENT = f"/Users/{info['user']}/northlake-cib-agent"
mlflow.set_experiment(EXPERIMENT)
mlflow.openai.autolog()   # a child span for every LLM call

from agent import tracing  # noqa: E402
from agent.cli import DEMO, show_work  # noqa: E402
from agent.graph import run  # noqa: E402

print(f"Warehouse {info['warehouse_id']} · experiment {EXPERIMENT} · tracing {tracing.enabled()}")

# COMMAND ----------

results = []
for q in DEMO:
    w = run(q)
    results.append(w)
    print("=" * 90 + f"\nQ: {q}\n" + "=" * 90)
    print(w["answer"])
    print(show_work(w), "\n")

# COMMAND ----------

# MAGIC %md ## Blocked-number test
# MAGIC A wrong figure is written into every draft. Expected: 2 retries, then "Commentary withheld" with the tool tables.

# COMMAND ----------

w = run(DEMO[0], inject_wrong_number=True)
results.append(w)
print(w["answer"])
print(show_work(w))

# COMMAND ----------

summary = [(r["question"][:60], r["outcome"], r["number_check"]["passed"], r["number_check"]["retries"],
            ", ".join(c["name"] for c in r["tool_calls"]), round(r["latency_ms"] / 1000, 1), r.get("trace_id"))
           for r in results]
display(spark.createDataFrame(summary, "question STRING, outcome STRING, number_check_passed BOOLEAN, retries INT, "
                                       "tools STRING, seconds DOUBLE, trace_id STRING"))
