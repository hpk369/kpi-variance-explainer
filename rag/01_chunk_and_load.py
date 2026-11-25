# Databricks notebook source
# MAGIC %md
# MAGIC # RAG 01 · Chunk the knowledge documents and load `gold.doc_chunks`
# MAGIC **Northlake Bank (fictional). Synthetic data only.**
# MAGIC
# MAGIC Reads the Markdown files in `/Volumes/<catalog>/bronze/raw/knowledge/` (uploaded from `knowledge/`), splits them into one chunk
# MAGIC per heading section (one glossary term = one chunk, ~400 tokens at most) and merges them into `gold.doc_chunks`.
# MAGIC The table has Change Data Feed on, so the Delta Sync index (`rag/02_create_index`) only re-embeds chunks that changed.
# MAGIC Re-running is safe: unchanged chunks are left alone and chunks of deleted sections are removed.

# COMMAND ----------

import os
import sys

dbutils.widgets.text("catalog", "cib_demo")
dbutils.widgets.text("docs_path", "")    # default: /Volumes/<catalog>/bronze/raw/knowledge, then <repo>/knowledge
dbutils.widgets.text("repo_root", "")    # default: the folder above this notebook

CAT = dbutils.widgets.get("catalog")
REPO = dbutils.widgets.get("repo_root") or os.path.dirname(os.getcwd())
candidates = [p for p in [dbutils.widgets.get("docs_path"), f"/Volumes/{CAT}/bronze/raw/knowledge", os.path.join(REPO, "knowledge")] if p]
DOCS = next((p for p in candidates if os.path.isdir(p) and any(f.endswith(".md") for f in os.listdir(p))), None)
if DOCS is None:
    raise RuntimeError("No .md files found in " + " or ".join(candidates) +
                       ". Upload them: databricks fs cp -r knowledge/ dbfs:/Volumes/cib_demo/bronze/raw/knowledge/ --overwrite -p northlake")
if not os.path.isfile(os.path.join(REPO, "rag", "chunking.py")):
    raise RuntimeError(f"rag/chunking.py not found under {REPO}. Run scripts/push_workspace.sh, or set repo_root.")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from rag.chunking import COLUMNS, load_folder  # noqa: E402

TABLE = f"{CAT}.gold.doc_chunks"
print(f"Documents: {DOCS}\nTable: {TABLE}")

# COMMAND ----------

chunks = load_folder(DOCS)   # raises if a file lacks front matter or the "fictional, synthetic" line
schema = ("chunk_id STRING, doc_id STRING, doc_type STRING, title STRING, section STRING, section_path STRING, as_of STRING, "
          "owner STRING, chunk_index INT, text STRING, n_chars INT, source_file STRING, content_hash STRING")
df = spark.createDataFrame([tuple(c[k] for k in COLUMNS) for c in chunks], schema)
print(f"{df.select('doc_id').distinct().count()} documents → {df.count()} chunks")

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  chunk_id     STRING NOT NULL COMMENT 'Primary key: <doc_id>::<section slug>',
  doc_id       STRING COMMENT 'Document key = file name without .md',
  doc_type     STRING COMMENT 'glossary, product, commentary or sop',
  title        STRING COMMENT 'Document title',
  section      STRING COMMENT 'Heading of this chunk (a glossary term, a product section, ...)',
  section_path STRING COMMENT 'Parent heading > heading',
  as_of        STRING COMMENT 'Date the document is valid from (YYYY-MM-DD)',
  owner        STRING COMMENT 'Team that owns the document',
  chunk_index  INT    COMMENT 'Position of the chunk in the corpus',
  text         STRING COMMENT 'Title + section + body: the text that is embedded and returned to the agent',
  n_chars      INT    COMMENT 'Length of text',
  source_file  STRING COMMENT 'Markdown file the chunk came from',
  content_hash STRING COMMENT 'Hash of text; a chunk is re-embedded only when it changes',
  loaded_at    TIMESTAMP COMMENT 'When the chunk was last inserted or changed'
)
COMMENT 'Northlake Bank (fictional), synthetic. Chunks of the knowledge documents (glossary, product one-pagers, management commentary, DQ-break SOP) for the Vector Search index doc_chunks_idx.'
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")
# In case the table was created earlier without CDF:
spark.sql(f"ALTER TABLE {TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

df.createOrReplaceTempView("new_chunks")
result = spark.sql(f"""
MERGE INTO {TABLE} t
USING new_chunks s ON t.chunk_id = s.chunk_id
WHEN MATCHED AND t.content_hash <> s.content_hash THEN UPDATE SET
  doc_id = s.doc_id, doc_type = s.doc_type, title = s.title, section = s.section, section_path = s.section_path,
  as_of = s.as_of, owner = s.owner, chunk_index = s.chunk_index, text = s.text, n_chars = s.n_chars,
  source_file = s.source_file, content_hash = s.content_hash, loaded_at = current_timestamp()
WHEN MATCHED AND t.chunk_index <> s.chunk_index THEN UPDATE SET chunk_index = s.chunk_index
WHEN NOT MATCHED THEN INSERT (chunk_id, doc_id, doc_type, title, section, section_path, as_of, owner, chunk_index, text,
                              n_chars, source_file, content_hash, loaded_at)
  VALUES (s.chunk_id, s.doc_id, s.doc_type, s.title, s.section, s.section_path, s.as_of, s.owner, s.chunk_index, s.text,
          s.n_chars, s.source_file, s.content_hash, current_timestamp())
WHEN NOT MATCHED BY SOURCE THEN DELETE
""")
display(result)

# COMMAND ----------

summary = spark.sql(f"""
SELECT doc_id, doc_type, COUNT(*) AS chunks, MAX(n_chars) AS longest_chunk_chars
FROM {TABLE} GROUP BY doc_id, doc_type ORDER BY doc_type, doc_id
""")
display(summary)
n = spark.table(TABLE).count()
assert n == len(chunks), f"{TABLE} has {n} rows, expected {len(chunks)}"
print(f"OK: {TABLE} has {n} chunks from {summary.count()} documents. Next: rag/02_create_index")
