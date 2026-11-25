-- Northlake Bank (fictional) · pipeline tests. Every row should say PASS.
-- Run: python scripts/run_sql.py pipeline/tests.sql

-- 1. Every layer has data
SELECT 'rows per layer' AS test,
       CASE WHEN MIN(n) > 0 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat_ws(', ', collect_list(concat(t, '=', n))) AS detail
FROM (
  SELECT 'bronze.fact_revenue' t, COUNT(*) n FROM ${catalog}.bronze.fact_revenue
  UNION ALL SELECT 'bronze.feed_revenue_daily', COUNT(*) FROM ${catalog}.bronze.feed_revenue_daily
  UNION ALL SELECT 'silver.revenue', COUNT(*) FROM ${catalog}.silver.revenue
  UNION ALL SELECT 'silver.pipeline', COUNT(*) FROM ${catalog}.silver.pipeline
  UNION ALL SELECT 'gold.fact_revenue', COUNT(*) FROM ${catalog}.gold.fact_revenue
  UNION ALL SELECT 'gold.fact_pipeline', COUNT(*) FROM ${catalog}.gold.fact_pipeline
  UNION ALL SELECT 'gold.fact_capital', COUNT(*) FROM ${catalog}.gold.fact_capital
  UNION ALL SELECT 'gold.fact_coverage', COUNT(*) FROM ${catalog}.gold.fact_coverage
);

-- 2. The three planted data-quality defects are caught
SELECT 'dq: planted defects caught' AS test,
       CASE WHEN SUM(CASE WHEN table_name = 'coverage' AND rule = 'not_null_banker_id' AND rows_failed = 46 THEN 1
                          WHEN table_name = 'pipeline' AND rule = 'unique_deal_id'     AND rows_failed = 3  THEN 1
                          WHEN table_name = 'pipeline' AND rule = 'fee_non_negative'   AND rows_failed = 1  THEN 1 ELSE 0 END) = 3
            THEN 'PASS' ELSE 'FAIL' END AS result,
       concat_ws(', ', collect_list(CASE WHEN rows_failed > 0 THEN concat(table_name, '.', rule, '=', rows_failed) END)) AS detail
FROM ${catalog}.gold.dq_summary;

-- 3. No other rule failed
SELECT 'dq: no unexpected failures' AS test,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result,
       COALESCE(concat_ws(', ', collect_list(concat(table_name, '.', rule, '=', rows_failed))), '') AS detail
FROM ${catalog}.gold.dq_summary
WHERE rows_failed > 0
  AND NOT (table_name = 'coverage' AND rule = 'not_null_banker_id')
  AND NOT (table_name = 'pipeline' AND rule IN ('unique_deal_id', 'fee_non_negative'));

-- 4. Exactly one reconciliation break: Transaction Banking, Jun 2026, about +$1.2M
SELECT 'recon: exactly the planted break' AS test,
       CASE WHEN COUNT(*) = 1 AND MAX(product_id) = 'P06' AND MAX(month_start) = DATE'2026-06-01'
                 AND ABS(MAX(delta) - 1200000) < 1 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat_ws(', ', collect_list(concat(product_name, ' ', CAST(month_start AS STRING), ' ', CAST(delta AS STRING)))) AS detail
FROM ${catalog}.gold.recon_control
WHERE is_break;

-- 5. Story 1: Industrials missed its Q3 2026 plan by about $8.8M
SELECT 'story 1: Industrials Q3 2026 vs plan' AS test,
       CASE WHEN a.v - p.v BETWEEN -8810000 AND -8790000 THEN 'PASS' ELSE 'FAIL' END AS result,
       CAST(ROUND(a.v - p.v, 2) AS STRING) AS detail
FROM (SELECT SUM(r.revenue) v FROM ${catalog}.gold.fact_revenue r
        JOIN ${catalog}.gold.dim_client c USING (client_id) JOIN ${catalog}.gold.dim_month m USING (month_start)
      WHERE c.industry = 'Industrials' AND m.quarter = '2026-Q3') a,
     (SELECT SUM(p.plan_revenue) v FROM ${catalog}.gold.fact_plan p
        JOIN ${catalog}.gold.dim_client c USING (client_id) JOIN ${catalog}.gold.dim_month m USING (month_start)
      WHERE c.industry = 'Industrials' AND m.quarter = '2026-Q3') p;

-- 6. One row per deal
SELECT 'keys: deal_id unique' AS test,
       CASE WHEN COUNT(*) = COUNT(DISTINCT deal_id) THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(COUNT(*), ' rows, ', COUNT(DISTINCT deal_id), ' deals') AS detail
FROM ${catalog}.gold.fact_pipeline;

-- 7. Data runs to yesterday (Toronto time)
SELECT 'freshness: as_of_date = yesterday' AS test,
       CASE WHEN as_of_date >= date_sub(CAST(from_utc_timestamp(current_timestamp(), 'America/Toronto') AS DATE), 1)
            THEN 'PASS' ELSE 'FAIL' END AS result,
       concat('as_of_date ', CAST(as_of_date AS STRING), ', last run ', CAST(refreshed_at_utc AS STRING), ' UTC') AS detail
FROM ${catalog}.gold.v_pbi_meta;

-- 8. The job's own checks (04_checks) passed on the latest run
SELECT 'job checks: latest run has no FAIL' AS test,
       CASE WHEN SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) = 0 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(SUM(CASE WHEN status = 'PASS' THEN 1 ELSE 0 END), ' pass, ',
              SUM(CASE WHEN status = 'WARN' THEN 1 ELSE 0 END), ' warn, ',
              SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END), ' fail') AS detail
FROM ${catalog}.gold.pipeline_checks
WHERE run_ts = (SELECT MAX(run_ts) FROM ${catalog}.gold.pipeline_checks);

-- 9. Every gold table has a Unity Catalog comment
SELECT 'comments: every gold table described' AS test,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result,
       COALESCE(concat_ws(', ', collect_list(table_name)), '') AS detail
FROM ${catalog}.information_schema.tables
WHERE table_schema = 'gold' AND (comment IS NULL OR comment = '');
