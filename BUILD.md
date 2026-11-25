# BUILD · run order

Northlake Bank (fictional). Synthetic data only. Every number lives in `config/constants.yaml` (hurdle 15%).

| Step | Command | What it does |
|---|---|---|
| 1 | `cp .env.example .env` and fill it in | Databricks host, token, warehouse ID, model names |
| 2 | `python space/smoke_test.py` | SQL warehouse, both LLMs (tool call), embeddings, Vector Search endpoint |
| 3 | `python generator/generate.py` | 10 tables + `expected_answers.json` in `generator/out/` |
| 4 | `python generator/checks.py` | Confirms the 4 planted stories, keys and planted DQ defects |
| 5 | `python scripts/run_sql.py setup/01_create_catalog.sql` | Catalog `cib_demo`, schemas bronze/silver/gold, volume `bronze.raw` |
| 6 | `databricks fs cp -r generator/out/ dbfs:/Volumes/cib_demo/bronze/raw/ --overwrite -p northlake` | Upload the generated files |
| 7 | `python -m pytest -q` | Unit tests (no Databricks needed) |
| 8 | `bash scripts/push_workspace.sh` | Uploads `pipeline/`, `generator/`, `common/`, `config/` to `/Workspace/Users/<you>/northlake-cib` (never `.env`) |
| 9 | `databricks jobs create --json @build/job.json -p northlake` (step 8 writes it from the `pipeline/job.json` template) | Creates `northlake_cib_pipeline`: 5 serverless tasks, daily 05:30 Toronto |
| 10 | `databricks jobs run-now <job_id> -p northlake` | First run backfills the feed from Oct 1 and builds bronze → silver → gold |
| 11 | `python scripts/run_sql.py pipeline/tests.sql` | 9 tests, every line PASS |
| 12 | `python scripts/run_sql.py tools/create_functions.sql` | The 6 governed tools in `cib_demo.gold` (built from `tools/build_functions.py`) |
| 13 | `python scripts/run_sql.py tools/test_functions.sql` | 9 tests, every line PASS |
| 14 | `python agent/tools.py get_kpi metric=revenue period=2026-Q3 segment=Industrials` | Calls a tool the way the agent and the Space do |
| 15 | `python rag/retrieval_tests.py --offline` | Keyword check on the local `knowledge/` files (no Databricks): 6/6 |
| 16 | `databricks fs cp -r knowledge/ dbfs:/Volumes/cib_demo/bronze/raw/knowledge/ --overwrite -p northlake` then `bash scripts/push_workspace.sh` | Upload the 11 documents and the `rag/` code |
| 17 | Workspace → `northlake-cib/rag/01_chunk_and_load` → Run all | `gold.doc_chunks` (88 chunks, Change Data Feed on) |
| 18 | Workspace → `northlake-cib/rag/02_create_index` → Run all | Delta Sync index `gold.doc_chunks_idx` on `cib_vs_endpoint`; waits until online |
| 19 | `python rag/retrieval_tests.py` | 6 questions against the index, expected document in the top 3 |
| 20 | `pip install -r requirements.txt` then `python -m pytest -q` | Adds LangGraph and MLflow; agent tests run with a scripted LLM (no Databricks) |
| 21 | `python -m agent.cli "Why did Industrials revenue miss plan in Q3?" --show-work` | One answer plus tool calls, SQL, retrieved definitions and the number check |
| 22 | `python -m agent.cli --demo` | The six Gate 6 questions |
| 23 | `python -m agent.cli --inject-wrong-number "Why did Industrials revenue miss plan in Q3?"` | Blocked-answer demo: 2 retries, then "Commentary withheld" |
| 24 | `bash scripts/push_workspace.sh`, then run `notebooks/run_agent` and `notebooks/log_and_register` | MLflow traces; UC model `cib_demo.gold.cib_insights_agent` |

| 25 | `python eval/build_eval_set.py` | `eval/eval_set.jsonl`: 15 questions with expected figures from `expected_answers.json` |
| 26 | `python -m eval.evaluation --limit 3` | Pilot on the Mac: 3 questions, all scorers, scorecard in `eval/results/scorecard.md` |
| 27 | `bash scripts/push_workspace.sh`, then `eval/run_eval` → Run all | Full evaluation in MLflow (experiment `cib_insights_eval`) |
| 28 | `python space/build_cache.py` | `space/cache.json`: the 4 suggested questions, saved only if they pass the eval checks |
| 29 | `bash scripts/deploy_space.sh <hf-username>/northlake-cib-insights` | Builds `space/build/` (app, agent, config, documents, `cache.json`) and uploads it to the Space |
| 30 | Space secrets: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` (optional: `FORCE_CACHE=1`, `LIVE_TIMEOUT_S`, `SESSION_LIMIT`, `HOURLY_CAP`) | The Space reads the same names as `.env` |
| 31 | `open docs/index.html` | The demo page; GitHub Pages serves `docs/` at insights.inkandinfra.com |

## The Space (`space/`)
- `app.py` draws the page (Gradio); `core.py` does the rest and is unit-tested in `tests/test_space.py` without Gradio.
- 5 questions per visit, `HOURLY_CAP` (default 30) across all visitors, 300-character limit, URLs and code stripped. The page has no public API, so the limits can't be skipped.
- **Fallback:** on an auth error (401/403), a quota or "compute unavailable" error, no answer within `LIVE_TIMEOUT_S` (default 45 s), or `FORCE_CACHE=1`, a suggested question gets its recorded answer from `cache.json` with a note "Live backend unavailable, showing a recorded answer from …"; any other question gets "live mode is off".

## Evaluation (`eval/`)
| Scorer | Kind | Passes when |
|---|---|---|
| number_match | code | every expected figure is in the answer (money ±1%, rates ±0.6 pts, counts exact) |
| tool_selection | code | the expected governed tools were called (none for a refusal) |
| expected_docs · mentions | code | the expected document was retrieved · key names appear |
| guardrail | code | real-bank question refused · future period answered "no data", no figures |
| groundedness | LLM judge (gpt-oss-120b) | every claim and figure is supported by the tool results and retrieved text |
| retrieval_relevance | LLM judge | share of retrieved excerpts that are relevant |

Story 2 questions fix the RAROC window to the 12 months to Sep 2026 and story 3 win rates are for the four quarters to 2026-Q3, so the expected figures don't drift as the daily feed adds data (the win-rate row needs updating after Dec 31, 2026).

## The agent (`agent/`)
`guard → plan ⇄ tools → retrieve → draft → number_check → answer` (LangGraph, `agent/graph.py`).
- **guard** refuses questions about real banks or real data without calling anything.
- **plan** lets the LLM call the six governed tools (up to 4 rounds); tool errors go back to the model to fix.
- **retrieve** looks up the glossary term behind each tool used, plus the question itself (Vector Search; local keyword search if the index can't be reached).
- **draft** writes Headline · Drivers · Executive paragraph (≤ 90 words) · Definitions used.
- **number_check** (`agent/number_check.py`) matches every figure to the tool results (display rounding or 0.5%). On a mismatch the draft is redone with the list of bad figures; after 2 retries the answer is blocked and only the tool tables are shown.
- **claim_check** reads the answer against the tool results with a second model (`models.verifier`, gpt-oss-120b) and sends back any claim the evidence doesn't support: a figure on the wrong team, "below" for "equal to", "all clients" for a filtered list. Same 2 retries, then blocked. If the checker can't be reached the number check still applies. `CLAIM_CHECK=0` turns it off.
- `agent/cli.py` for the terminal, `agent/responses_agent.py` for MLflow (answer + "show your work" in `custom_outputs`).

## Knowledge and RAG (`knowledge/`, `rag/`)
- `knowledge/glossary.md` is the single source of truth for definitions (hurdle 15%, tax 25%, stage probabilities 10/25/60/85%). The SQL tools, DAX and agent follow it; `tests/test_chunking.py` checks it against `config/constants.yaml`.
- 6 product one-pagers, management commentary for Jul, Aug and Sep 2026 (figures match the generated data), and the DQ and reconciliation-break SOP.
- `rag/chunking.py` makes one chunk per heading (one glossary term per chunk, ≤ 1,600 characters). Edit a document, re-upload, re-run notebooks 01 and 02: only changed chunks are re-embedded.
- `02_create_index` uses managed embeddings through the `databricks-gte-large-en` serving endpoint. If that endpoint doesn't exist, it embeds the chunks itself into `gold.doc_chunks_emb` and builds the index from that. `common/dbx.vs_query()` works with either.

## The pipeline (`pipeline/`)
| Task | Notebook | Does |
|---|---|---|
| daily_feed | `00_daily_feed.py` | Writes missing feed days up to yesterday into the volume; writes `bronze.run_config` |
| bronze | `01_bronze.py` | Volume files → `bronze.*` with `_source_file`, `_ingest_ts` (full reload, so re-runs never double-count) |
| silver | `02_silver.py` | Types, de-duplicates, DQ rules → `silver.dq_quarantine` / `dq_results`; history + feed joined; deal state from events |
| gold | `03_gold.py` | Star schema, `recon_control`, `dq_summary`, `config`, `pipeline_runs`, `v_pbi_meta`, UC comments |
| checks | `04_checks.py` | Gold vs `expected_answers.json`; FAIL on unexpected breaks or DQ failures, WARN on the planted break |

Optional: `python generator/daily_feed.py --until 2026-11-22` writes the daily feed locally to test it. In Databricks the job writes the feed itself, so don't upload a local `generator/out/feed/`.

## The four planted stories
1. **Industrials misses plan in Q3 2026** by $8.8M: two M&A mandates ($4.5M, $3.0M) slip from August to November (85% of the miss); the rest of Industrials is $1.3M short.
2. **18 lending-only Mid-Market clients** sit below the 15% RAROC hurdle (6–9%): the cross-sell whitespace.
3. **Coverage East 2** carries 14 clients per banker against a target of 10, and its win rate falls 44% → 25% over four quarters.
4. **Reconciliation break**: the GL control total for Transaction Banking, June 2026, is $1.2M above booked revenue.

From October 2026 a daily feed adds one synthetic business day at a time, so the dashboard and the agent's month-to-date answers stay current. The two slipped mandates close on Nov 12 and Nov 19, 2026.
