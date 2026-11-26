# Databricks notebook source
# MAGIC %md
# MAGIC # Log the agent to MLflow and register it in Unity Catalog
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Logs `agent/responses_agent.py` as an MLflow **ResponsesAgent** ("models from code", with `agent/`, `common/`, `config/`,
# MAGIC `rag/` and `knowledge/` packed in), runs one test prediction, and registers it as `cib_demo.gold.cib_insights_agent`.
# MAGIC Check: **Catalog → cib_demo → gold → Models → cib_insights_agent**.

# COMMAND ----------

# MAGIC %pip install -q "langgraph>=0.3" "openai>=1.40" "databricks-sql-connector>=3.4" "pyyaml>=6" "mlflow>=3.1"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import os
import sys
from importlib.metadata import version

dbutils.widgets.text("repo_root", "")
dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.text("model_name", "cib_demo.gold.cib_insights_agent")
REPO = dbutils.widgets.get("repo_root") or os.path.dirname(os.getcwd())
MODEL_NAME = dbutils.widgets.get("model_name")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agent.dbx_env import configure  # noqa: E402

info = configure(dbutils.widgets.get("warehouse_id"))

import mlflow  # noqa: E402
from mlflow.models.resources import (DatabricksFunction, DatabricksServingEndpoint, DatabricksSQLWarehouse,  # noqa: E402
                                     DatabricksVectorSearchIndex)

from agent import tools  # noqa: E402
from common import config  # noqa: E402

mlflow.set_registry_uri("databricks-uc")
mlflow.set_experiment(f"/Users/{info['user']}/northlake-cib-agent")

# COMMAND ----------

# Resources the agent uses (for automatic credentials when served, and for lineage in Unity Catalog)
from databricks.sdk import WorkspaceClient  # noqa: E402

endpoints = {e.name for e in WorkspaceClient().serving_endpoints.list()}
resources = [DatabricksSQLWarehouse(warehouse_id=info["warehouse_id"]),
             DatabricksVectorSearchIndex(index_name=config.constants()["vector_search"]["index"])]
resources += [DatabricksFunction(function_name=f"{config.catalog()}.gold.{name}") for name in tools.TOOLS]
for kind in ("llm", "llm_fallback", "embedding"):
    name = f"databricks-{config.model(kind)}"
    if name in endpoints:
        resources.append(DatabricksServingEndpoint(endpoint_name=name))
print("\n".join(str(r.to_dict()) for r in resources))

packages = ["mlflow", "langgraph", "openai", "databricks-sql-connector", "databricks-sdk", "pyyaml", "requests"]
pip_requirements = [f"{p}=={version(p)}" for p in packages]
print(pip_requirements)

# COMMAND ----------

input_example = {"input": [{"role": "user", "content": "Why did Industrials revenue miss plan in Q3?"}]}

# Copy only the code the model needs into a clean folder (notebooks in the workspace are not plain files).
import glob  # noqa: E402
import shutil  # noqa: E402

CODE = "/tmp/kpi_agent_code"
shutil.rmtree(CODE, ignore_errors=True)
for pattern in ("agent/*.py", "common/*.py", "config/*.yaml", "rag/__init__.py", "rag/chunking.py",
                "rag/retrieval_tests.py", "knowledge/*.md"):
    for src in glob.glob(os.path.join(REPO, pattern)):
        with open(src, encoding="utf-8") as f:
            text = f.read()
        if text.startswith("# Databricks notebook source"):
            continue
        dst = os.path.join(CODE, os.path.relpath(src, REPO))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)
print(sorted(os.path.relpath(p, CODE) for p in glob.glob(f"{CODE}/**/*", recursive=True) if os.path.isfile(p)))

with mlflow.start_run(run_name="cib_insights_agent"):
    mlflow.log_params({"llm": config.model("llm"), "llm_fallback": config.model("llm_fallback"),
                       "embedding": config.model("embedding"), "constants_version": config.constants()["version"],
                       "hurdle_rate": config.constants()["raroc"]["hurdle_rate"]})
    logged = mlflow.pyfunc.log_model(
        name="agent",
        python_model=os.path.join(CODE, "agent", "responses_agent.py"),
        code_paths=[os.path.join(CODE, d) for d in ("agent", "common", "config", "rag", "knowledge")],
        pip_requirements=pip_requirements,
        resources=resources,
        input_example=input_example,
    )
print(logged.model_uri)

# COMMAND ----------

# MAGIC %md ## Test the logged model

# COMMAND ----------

loaded = mlflow.pyfunc.load_model(logged.model_uri)
response = loaded.predict(input_example)
print(response["output"][0]["content"][0]["text"])
print("\noutcome:", response["custom_outputs"]["outcome"], "· number check passed:", response["custom_outputs"]["number_check"]["passed"])

# COMMAND ----------

registered = mlflow.register_model(logged.model_uri, MODEL_NAME)
print(f"Registered {MODEL_NAME} version {registered.version}")
