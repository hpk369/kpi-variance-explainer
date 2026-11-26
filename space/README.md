---
title: KPI Variance Explainer
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: gradio
sdk_version: 6.3.0
app_file: app.py
pinned: false
short_description: Performance insights agent on synthetic bank data
---

# KPI Variance Explainer (Northlake Bank, fictional; synthetic data)

An agent that explains why KPIs moved. It gets every number from six governed Databricks Unity Catalog SQL functions, explains terms from the bank's glossary through Vector Search, and checks every figure in its answer against the tool results before showing it. Open **Show your work** under an answer to see the tool calls, the SQL, the definitions it used and the number check.

Northlake Bank is fictional. Every client, deal and figure in this demo is synthetic.

- 5 questions per visit, and a cap per hour across all visitors (the backend is a free-tier workspace)
- If the backend is unavailable, the 4 suggested questions show recorded answers that passed the evaluation

Code, build guide and evaluation scorecard: https://github.com/hpk369/kpi-variance-explainer
