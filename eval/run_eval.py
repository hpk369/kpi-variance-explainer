# Databricks notebook source
# MAGIC %md
# MAGIC # Evaluate the agent (MLflow GenAI evaluation)
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Runs the agent on the 15 questions in `eval/eval_set.jsonl` through `mlflow.genai.evaluate` and scores each answer:
# MAGIC
# MAGIC | Scorer | Kind | Passes when |
# MAGIC |---|---|---|
# MAGIC | number_match | code | every expected figure from `expected_answers.json` is in the answer (money ±1%, rates ±0.6 pts, counts exact) |
# MAGIC | tool_selection | code | the expected governed tools were called (none for a refusal) |
# MAGIC | expected_docs, mentions | code | the expected document was retrieved; key names appear |
# MAGIC | guardrail | code | real-bank question refused; future period answered with "no data" and no figures |
# MAGIC | groundedness | LLM judge | every claim and figure is supported by the tool results and retrieved documents |
# MAGIC | retrieval_relevance | LLM judge | share of retrieved excerpts that are relevant |
# MAGIC
# MAGIC The judge is the backup model (`gpt-oss-120b`), not the agent's model. Results: experiment `/Users/<you>/cib_insights_eval`
# MAGIC (Traces tab: one trace per question with its assessments) and the scorecard printed at the end.
# MAGIC Each full run is 15 agent runs plus ~26 judge calls: keep to 3–4 runs a day on Free Edition.

# COMMAND ----------

# MAGIC %pip install -q "langgraph>=0.3" "openai>=1.40" "databricks-sql-connector>=3.4" "pyyaml>=6" "mlflow[databricks]>=3.1"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import os
import sys
from pathlib import Path

dbutils.widgets.text("repo_root", "")
dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.text("ids", "")                    # e.g. s1_why_miss,g_future (blank = all 15)
dbutils.widgets.text("limit", "")                  # e.g. 3 for a pilot
dbutils.widgets.dropdown("judges", "yes", ["yes", "no"])
dbutils.widgets.text("max_workers", "1")           # questions in parallel: 1, since agent and judge share a tokens-per-minute quota

REPO = dbutils.widgets.get("repo_root") or os.path.dirname(os.getcwd())
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = dbutils.widgets.get("max_workers") or "1"

from agent.dbx_env import configure  # noqa: E402

info = configure(dbutils.widgets.get("warehouse_id"))

import mlflow  # noqa: E402

mlflow.openai.autolog()

from eval import evaluation  # noqa: E402

try:   # results go next to the code in the workspace; /tmp if the folder is read-only
    (Path(REPO) / "eval" / "results").mkdir(exist_ok=True)
    (Path(REPO) / "eval" / "results" / ".write_test").write_text("ok")
except OSError:
    evaluation.RESULTS = Path("/tmp/kpi_eval_results")

ids = [x.strip() for x in dbutils.widgets.get("ids").split(",") if x.strip()] or None
limit = int(dbutils.widgets.get("limit")) if dbutils.widgets.get("limit") else None
rows = evaluation.load_set(Path(REPO) / "eval" / "eval_set.jsonl", ids=ids, limit=limit)
EXPERIMENT = f"/Users/{info['user']}/cib_insights_eval"
print(f"{len(rows)} questions · experiment {EXPERIMENT} · judges {dbutils.widgets.get('judges')} · results {evaluation.RESULTS}")

# COMMAND ----------

payload = evaluation.run_mlflow(rows, EXPERIMENT, judges=dbutils.widgets.get("judges") == "yes")
print("MLflow run:", payload["mlflow_run_id"])

# COMMAND ----------

# MAGIC %md ## Scorecard

# COMMAND ----------

displayHTML("<pre style='font-size:13px'>" + (evaluation.RESULTS / "scorecard.md").read_text()
            .replace("&", "&amp;").replace("<", "&lt;") + "</pre>")

# COMMAND ----------

# MAGIC %md ## Answers (read the failures here)

# COMMAND ----------

for r in payload["results"]:
    bad = {k: s["rationale"] for k, s in r["scores"].items()
           if s and (s["value"] is False or s.get("error") or (isinstance(s["value"], float) and s["value"] < 1))}
    print("=" * 100 + f"\n[{r['id']}] {r['question']}\noutcome: {r['outcome']} · {round((r['latency_ms'] or 0) / 1000)}s"
          + (f"\nFAILED: {bad}" if bad else "\nall scorers passed") + "\n" + "-" * 100 + f"\n{r['answer']}\n")
