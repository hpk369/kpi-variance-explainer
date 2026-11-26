-- Northlake Bank (fictional) · catalog, schemas and the raw-file volume.
-- Run: python scripts/run_sql.py setup/01_create_catalog.sql
-- If CREATE CATALOG fails on Free Edition, set databricks.catalog: workspace in config/constants.yaml
-- and re-run (the CREATE CATALOG line then does nothing).

CREATE CATALOG IF NOT EXISTS ${catalog};
COMMENT ON CATALOG ${catalog} IS 'Northlake Bank (fictional) analytics demo. Synthetic data only.';

CREATE SCHEMA IF NOT EXISTS ${catalog}.bronze COMMENT 'Raw synthetic files as loaded, plus ingest metadata';
CREATE SCHEMA IF NOT EXISTS ${catalog}.silver COMMENT 'Typed, de-duplicated, data-quality checked';
CREATE SCHEMA IF NOT EXISTS ${catalog}.gold   COMMENT 'Star schema, governed views and tools used by the agent and Power BI';

CREATE VOLUME IF NOT EXISTS ${catalog}.bronze.raw COMMENT 'Generator output (parquet) and the daily feed';

SELECT catalog_name, schema_name FROM ${catalog}.information_schema.schemata WHERE schema_name IN ('bronze','silver','gold') ORDER BY schema_name;
