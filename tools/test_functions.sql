-- Northlake Bank (fictional) · tests for the six governed tools. Every row should say PASS.
-- Expected values come from generator/out/expected_answers.json (the planted stories).
-- Run: python scripts/run_sql.py tools/test_functions.sql

-- 1. Story 1: Industrials missed its Q3 2026 plan by $8.8M
SELECT 'get_kpi: Industrials Q3 2026 revenue vs plan' AS test,
       CASE WHEN error IS NULL AND ABS(var_vs_plan - (-8799999.98)) < 1 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat('var_vs_plan ', CAST(var_vs_plan AS STRING), ' (expected -8799999.98) ', COALESCE(error, '')) AS detail
FROM ${catalog}.gold.get_kpi('revenue', '2026-Q3', 'Industrials');

-- 2. Story 1: M&A Advisory is about 85% of the miss
SELECT 'variance_drivers: M&A Advisory drives the Industrials miss' AS test,
       CASE WHEN MAX(CASE WHEN rank = 1 THEN dimension_value END) = 'M&A Advisory'
                 AND MAX(CASE WHEN rank = 1 THEN share_of_total_delta END) BETWEEN 0.85 AND 0.86 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat('rank 1: ', MAX(CASE WHEN rank = 1 THEN dimension_value END), ' share ',
              CAST(MAX(CASE WHEN rank = 1 THEN share_of_total_delta END) AS STRING), ' (expected M&A Advisory 0.8523)') AS detail
FROM ${catalog}.gold.variance_drivers('revenue', '2026-Q3', 'plan', 'product', 'Industrials');

-- 3. Story 1: exactly the two planted mandates slipped out of Q3 2026
SELECT 'pipeline_conversion: the two Industrials mandates slipped' AS test,
       CASE WHEN concat_ws(',', array_sort(collect_list(deal_id))) = 'D-20458,D-20466' THEN 'PASS' ELSE 'FAIL' END AS result,
       concat('slipped: ', concat_ws(', ', array_sort(collect_list(concat(deal_id, ' ', client_name, ' ', stage_now))))) AS detail
FROM ${catalog}.gold.pipeline_conversion('2026-Q3', 'Industrials', 'slipped');

-- 4. Story 2: 18 lending-only Mid-Market clients below the 15% hurdle (window to Sep 2026)
SELECT 'cross_sell_whitespace: 18 lending-only Mid-Market clients' AS test,
       CASE WHEN COUNT(*) = 18 AND MIN(products_held) = 'Lending' AND MAX(products_held) = 'Lending'
                 AND MAX(raroc) < 0.10 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(COUNT(*), ' clients, RAROC ', CAST(MIN(raroc) AS STRING), ' to ', CAST(MAX(raroc) AS STRING), ' (expected 18, 6%-9%)') AS detail
FROM ${catalog}.gold.cross_sell_whitespace('Mid-Market', 0.15, '2026-09');

-- 5. Story 3: Coverage East 2 is over capacity and its win rate falls 44% -> 25%
SELECT 'coverage_capacity: Coverage East 2 over capacity, win rate falling' AS test,
       CASE WHEN MAX(clients_per_banker) = 14 AND MAX(capacity_target) = 10 AND BOOL_AND(over_capacity)
                 AND ABS(MAX(CASE WHEN quarter = '2025-Q4' THEN win_rate END) - 0.4375) < 0.001
                 AND ABS(MAX(CASE WHEN quarter = '2026-Q1' THEN win_rate END) - 0.40) < 0.001
                 AND ABS(MAX(CASE WHEN quarter = '2026-Q2' THEN win_rate END) - 0.30) < 0.001
                 AND ABS(MAX(CASE WHEN quarter = '2026-Q3' THEN win_rate END) - 0.25) < 0.001 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(CAST(MAX(clients_per_banker) AS STRING), ' clients/banker vs ', CAST(MAX(capacity_target) AS STRING), '; win rate ',
              concat_ws(', ', collect_list(CASE WHEN quarter IN ('2025-Q4', '2026-Q1', '2026-Q2', '2026-Q3')
                                                THEN concat(quarter, ' ', CAST(win_rate AS STRING)) END))) AS detail
FROM ${catalog}.gold.coverage_capacity('Coverage East 2', 8);

-- 6. Story 4: one break, Transaction Banking, Jun 2026, +$1.2M
SELECT 'recon_breaks: Transaction Banking Jun 2026 +1.2M' AS test,
       CASE WHEN COUNT(*) = 1 AND MAX(status) = 'break' AND MAX(product_name) = 'Transaction Banking'
                 AND MAX(month) = '2026-06' AND ABS(MAX(delta) - 1200000) < 1 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat_ws('; ', collect_list(concat(status, ' ', COALESCE(product_name, ''), ' ', COALESCE(month, ''), ' ', COALESCE(CAST(delta AS STRING), '')))) AS detail
FROM ${catalog}.gold.recon_breaks('2026-Q2');

-- 7. Bad input returns an error row, not a failure
SELECT 'bad input: error rows, no failures' AS test,
       CASE WHEN COUNT(*) = 4 AND COUNT(error) = 4 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(COUNT(error), ' of 4 calls returned an error message') AS detail
FROM (
  SELECT error FROM ${catalog}.gold.get_kpi('banana', '2026-Q3', 'ALL')
  UNION ALL SELECT error FROM ${catalog}.gold.get_kpi('revenue', '2026-Q9', 'ALL')
  UNION ALL SELECT error FROM ${catalog}.gold.variance_drivers('revenue', '2026-Q3', 'plan', 'product', 'Atlantis')
  UNION ALL SELECT error FROM ${catalog}.gold.recon_breaks('yesterday')
);

-- 8. Month to date works (for "how is this month tracking?")
SELECT 'get_kpi: revenue MTD' AS test,
       CASE WHEN error IS NULL AND actual > 0 AND plan > 0 AND period_end = as_of_date THEN 'PASS' ELSE 'FAIL' END AS result,
       concat(CAST(period_start AS STRING), ' to ', CAST(period_end AS STRING), ': actual ', CAST(actual AS STRING),
              ', plan ', CAST(plan AS STRING), ' ', COALESCE(error, '')) AS detail
FROM ${catalog}.gold.get_kpi('revenue', 'MTD', 'ALL');

-- 9. All six functions exist with a description
SELECT 'catalog: 6 functions with comments' AS test,
       CASE WHEN COUNT(*) = 6 AND COUNT(comment) = 6 THEN 'PASS' ELSE 'FAIL' END AS result,
       concat_ws(', ', collect_list(routine_name)) AS detail
FROM ${catalog}.information_schema.routines
WHERE routine_schema = 'gold'
  AND routine_name IN ('get_kpi', 'variance_drivers', 'pipeline_conversion', 'cross_sell_whitespace', 'coverage_capacity', 'recon_breaks');
