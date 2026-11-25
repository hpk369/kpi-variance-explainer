"""Inside a Databricks notebook: sets the environment variables the agent reads from .env on the Mac
(DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID, DATABRICKS_USER), using the notebook's own login.

    from agent.dbx_env import configure
    info = configure(warehouse_id="")   # "" = pick a SQL warehouse automatically
"""
from __future__ import annotations

import os


def configure(warehouse_id: str = "") -> dict:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    os.environ["DATABRICKS_HOST"] = w.config.host
    auth = w.config.authenticate().get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise RuntimeError("Could not get a token from the notebook login.")
    os.environ["DATABRICKS_TOKEN"] = auth.split(" ", 1)[1]
    os.environ["DATABRICKS_USER"] = w.current_user.me().user_name
    if not warehouse_id:
        warehouses = list(w.warehouses.list())
        if not warehouses:
            raise RuntimeError("No SQL warehouse found. Set the warehouse_id widget.")
        running = [x for x in warehouses if str(x.state).endswith("RUNNING")]
        warehouse_id = (running or warehouses)[0].id
    os.environ["DATABRICKS_WAREHOUSE_ID"] = warehouse_id
    return {"host": w.config.host, "user": os.environ["DATABRICKS_USER"], "warehouse_id": warehouse_id}
