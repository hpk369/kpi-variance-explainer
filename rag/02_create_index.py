# Databricks notebook source
# MAGIC %md
# MAGIC # RAG 02 · Create or sync the Vector Search index `gold.doc_chunks_idx`
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC A **Delta Sync** index on `gold.doc_chunks`, on the single Free Edition endpoint `cib_vs_endpoint`, triggered sync.
# MAGIC - **managed** (preferred): Databricks embeds the `text` column with the hosted `gte-large-en` serving endpoint.
# MAGIC - **self_managed** (automatic fallback, used only if no embedding *serving endpoint* exists): this notebook embeds the chunks
# MAGIC   itself into `gold.doc_chunks_emb` (1024 dimensions) and the index reads that table. The agent's retriever handles both.
# MAGIC
# MAGIC First run creates the index and waits until it is online (5–10 min). Later runs just sync it.

# COMMAND ----------

# MAGIC %pip install -q databricks-vectorsearch

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient
from databricks.vector_search.client import VectorSearchClient

dbutils.widgets.text("catalog", "cib_demo")
dbutils.widgets.text("endpoint", "cib_vs_endpoint")
dbutils.widgets.text("index", "")                 # default: <catalog>.gold.doc_chunks_idx
dbutils.widgets.text("embedding_model", "gte-large-en")
dbutils.widgets.text("embedding_endpoint", "")    # optional: exact serving endpoint name, e.g. databricks-gte-large-en
dbutils.widgets.dropdown("mode", "auto", ["auto", "managed", "self_managed"])
dbutils.widgets.dropdown("recreate", "no", ["no", "yes"])

CAT = dbutils.widgets.get("catalog")
ENDPOINT = dbutils.widgets.get("endpoint")
INDEX = dbutils.widgets.get("index") or f"{CAT}.gold.doc_chunks_idx"
SOURCE = f"{CAT}.gold.doc_chunks"
SOURCE_EMB = f"{CAT}.gold.doc_chunks_emb"
BARE = dbutils.widgets.get("embedding_model").replace("system.ai.", "").replace("databricks-", "")
MODE = dbutils.widgets.get("mode")
RECREATE = dbutils.widgets.get("recreate") == "yes"
SYNC_COLUMNS = ["chunk_id", "doc_id", "doc_type", "title", "section", "section_path", "as_of", "text"]
DIM = 1024

w = WorkspaceClient()
vsc = VectorSearchClient(disable_notice=True)
n_rows = spark.table(SOURCE).count()
assert n_rows > 0, f"{SOURCE} is empty. Run rag/01_chunk_and_load first."

state = vsc.get_endpoint(ENDPOINT).get("endpoint_status", {}).get("state")
print(f"Endpoint {ENDPOINT}: {state} · source {SOURCE}: {n_rows} chunks · index {INDEX}")
if state != "ONLINE":
    raise RuntimeError(f"Vector Search endpoint {ENDPOINT} is {state}. Wait until it is ONLINE (Compute → Vector Search), then re-run.")

# COMMAND ----------

# MAGIC %md ## Pick the embedding route

# COMMAND ----------

serving = {e.name: e for e in w.serving_endpoints.list()}
wanted = dbutils.widgets.get("embedding_endpoint")
names = [wanted] if wanted else [f"databricks-{BARE}", BARE]
EMB_ENDPOINT = next((n for n in names if n in serving), None)
print("Embedding-like serving endpoints:", sorted(n for n in serving if any(k in n.lower() for k in ("gte", "bge", "embed"))) or "none")

if MODE == "auto":
    MODE = "managed" if EMB_ENDPOINT else "self_managed"
if MODE == "managed" and not EMB_ENDPOINT:
    raise RuntimeError(f"No serving endpoint named {' or '.join(names)}. Set embedding_endpoint, or use mode = self_managed.")
print(f"Mode: {MODE}" + (f" · embedding endpoint {EMB_ENDPOINT}" if MODE == "managed" else f" · embedding model {BARE}, computed here"))

# COMMAND ----------

# MAGIC %md ## Self-managed only: embed the chunks into `gold.doc_chunks_emb`

# COMMAND ----------

def embed(texts: list[str]) -> list[list[float]]:
    """Unity AI Gateway first (system.ai.<model>), then a serving endpoint. Same order as common/dbx.py."""
    errors = []
    try:
        r = w.api_client.do("POST", "/ai-gateway/mlflow/v1/embeddings", body={"model": f"system.ai.{BARE}", "input": texts})
        return [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    except Exception as ex:
        errors.append(f"gateway: {str(ex)[:200]}")
    for name in (EMB_ENDPOINT, f"databricks-{BARE}"):
        if not name:
            continue
        try:
            r = w.serving_endpoints.query(name=name, input=texts)
            return [d.embedding for d in r.data]
        except Exception as ex:
            errors.append(f"{name}: {str(ex)[:200]}")
    raise RuntimeError("Could not embed with any route. " + " | ".join(errors))


if MODE == "self_managed":
    rows = spark.table(SOURCE).select(*SYNC_COLUMNS, "content_hash").collect()
    vectors = []
    for i in range(0, len(rows), 16):
        vectors += embed([r.text for r in rows[i:i + 16]])
    assert all(len(v) == DIM for v in vectors), f"Expected {DIM}-dimension vectors, got {len(vectors[0])}"
    emb = spark.createDataFrame([(*[r[c] for c in SYNC_COLUMNS], r.content_hash, [float(x) for x in v]) for r, v in zip(rows, vectors)],
                                ", ".join(f"{c} STRING" for c in SYNC_COLUMNS) + ", content_hash STRING, embedding ARRAY<FLOAT>")
    spark.sql(f"""CREATE TABLE IF NOT EXISTS {SOURCE_EMB} ({", ".join(f"{c} STRING" for c in SYNC_COLUMNS)},
                  content_hash STRING, embedding ARRAY<FLOAT>)
                  COMMENT 'Northlake Bank (fictional), synthetic. doc_chunks with {BARE} embeddings (self-managed Vector Search index).'
                  TBLPROPERTIES (delta.enableChangeDataFeed = true)""")
    emb.createOrReplaceTempView("new_emb")
    spark.sql(f"""
    MERGE INTO {SOURCE_EMB} t USING new_emb s ON t.chunk_id = s.chunk_id
    WHEN MATCHED AND t.content_hash <> s.content_hash THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    WHEN NOT MATCHED BY SOURCE THEN DELETE""")
    print(f"{SOURCE_EMB}: {spark.table(SOURCE_EMB).count()} rows with {DIM}-dimension embeddings")

# COMMAND ----------

# MAGIC %md ## Create the index, or sync it if it exists

# COMMAND ----------

existing = [i["name"] for i in vsc.list_indexes(ENDPOINT).get("vector_indexes", [])]
source_table = SOURCE if MODE == "managed" else SOURCE_EMB

if INDEX in existing:
    spec = vsc.get_index(endpoint_name=ENDPOINT, index_name=INDEX).describe().get("delta_sync_index_spec", {})
    current_mode = "managed" if spec.get("embedding_source_columns") else "self_managed"
    if RECREATE or current_mode != MODE or spec.get("source_table") != source_table:
        print(f"Deleting {INDEX} (mode {current_mode}, source {spec.get('source_table')}) to recreate it as {MODE} on {source_table}")
        vsc.delete_index(endpoint_name=ENDPOINT, index_name=INDEX)
        while INDEX in [i["name"] for i in vsc.list_indexes(ENDPOINT).get("vector_indexes", [])]:
            time.sleep(10)
        existing = []

if INDEX in existing:
    index = vsc.get_index(endpoint_name=ENDPOINT, index_name=INDEX)
    try:
        index.sync()
        print(f"Sync started for {INDEX}")
    except Exception as ex:   # e.g. a sync is already running
        print(f"Sync not started ({str(ex)[:160]}); waiting for the current state instead")
elif MODE == "managed":
    index = vsc.create_delta_sync_index(
        endpoint_name=ENDPOINT, index_name=INDEX, source_table_name=SOURCE, pipeline_type="TRIGGERED",
        primary_key="chunk_id", embedding_source_column="text", embedding_model_endpoint_name=EMB_ENDPOINT,
        columns_to_sync=SYNC_COLUMNS)
    print(f"Created {INDEX} (managed embeddings via {EMB_ENDPOINT})")
else:
    index = vsc.create_delta_sync_index(
        endpoint_name=ENDPOINT, index_name=INDEX, source_table_name=SOURCE_EMB, pipeline_type="TRIGGERED",
        primary_key="chunk_id", embedding_dimension=DIM, embedding_vector_column="embedding",
        columns_to_sync=SYNC_COLUMNS)
    print(f"Created {INDEX} (self-managed embeddings from {SOURCE_EMB})")

# COMMAND ----------

# MAGIC %md ## Wait until the index is online with every chunk (5–10 min the first time)

# COMMAND ----------

deadline, last = time.time() + 30 * 60, None
while True:
    status = index.describe().get("status", {})
    detailed, count, ready = status.get("detailed_state", "?"), status.get("indexed_row_count", 0), status.get("ready", False)
    if (detailed, count) != last:
        print(f"{time.strftime('%H:%M:%S')}  {detailed}  ready={ready}  indexed {count}/{n_rows}")
        last = (detailed, count)
    if ready and count == n_rows and ("NO_PENDING" in detailed or detailed == "ONLINE"):
        break
    if "FAILED" in detailed:
        raise RuntimeError(f"Index {INDEX} failed: {status.get('message')}")
    if time.time() > deadline:
        raise TimeoutError(f"Index not ready after 30 minutes: {status}. Check Compute → Vector Search → {ENDPOINT}.")
    time.sleep(20)
print(f"OK: {INDEX} is online with {count} chunks ({MODE}).")

# COMMAND ----------

# MAGIC %md ## Quick check: one query

# COMMAND ----------

q = "what is RAROC"
kwargs = {"query_text": q} if MODE == "managed" else {"query_vector": embed([q])[0]}
res = index.similarity_search(columns=["chunk_id", "doc_id", "section"], num_results=3, **kwargs)
cols = [c["name"] for c in res["manifest"]["columns"]]
for row in res["result"].get("data_array", []):
    print(dict(zip(cols, row)))
print("\nNext, on the Mac: python rag/retrieval_tests.py   (expect 6/6)")
