-- One wide, report-ready view for the first Power BI report (Phase 8 "default analysis").
-- Revenue and plan by month, client and product, with readable column names, up to the current month.
-- Phase 9 adds the full v_pbi_* model; this view stays so the published report keeps working.
-- Run: python scripts/run_sql.py pipeline/05_pbi_quick.sql
-- Northlake Bank (fictional). Synthetic data only.

CREATE OR REPLACE VIEW ${catalog}.gold.v_pbi_revenue
COMMENT 'Revenue, Plan and Variance vs plan in CAD, by month, client and product, complete months plus the current month to date. For Power BI.'
AS
WITH rp AS (
  SELECT COALESCE(r.client_id, p.client_id)    AS client_id,
         COALESCE(r.product_id, p.product_id)  AS product_id,
         COALESCE(r.month_start, p.month_start) AS month_start,
         COALESCE(r.revenue, 0)                AS revenue,
         COALESCE(p.plan_revenue, 0)           AS plan_revenue
  FROM ${catalog}.gold.fact_revenue r
  FULL OUTER JOIN ${catalog}.gold.fact_plan p
    ON r.client_id = p.client_id AND r.product_id = p.product_id AND r.month_start = p.month_start
)
SELECT m.month_start                         AS `Month`,
       m.month_label                         AS `Month label`,
       m.quarter                             AS `Quarter`,
       m.fiscal_year                         AS `Fiscal year`,
       m.is_current                          AS `Current month`,
       c.segment                             AS `Segment`,
       c.industry                            AS `Industry`,
       c.region                              AS `Region`,
       c.team                                AS `Coverage team`,
       c.client_name                         AS `Client`,
       pr.product_name                       AS `Product`,
       pr.revenue_type                       AS `Revenue type`,
       rp.revenue                            AS `Revenue`,
       rp.plan_revenue                       AS `Plan`,
       rp.revenue - rp.plan_revenue          AS `Variance vs plan`
FROM rp
JOIN ${catalog}.gold.dim_month   m  ON m.month_start = rp.month_start
JOIN ${catalog}.gold.dim_client  c  ON c.client_id   = rp.client_id
JOIN ${catalog}.gold.dim_product pr ON pr.product_id = rp.product_id
WHERE m.is_complete OR m.is_current;

-- Check: Industrials Q3 2026 should be about -8.8M vs plan
SELECT `Quarter`, ROUND(SUM(`Revenue`) / 1e6, 1) AS revenue_m, ROUND(SUM(`Plan`) / 1e6, 1) AS plan_m,
       ROUND(SUM(`Variance vs plan`) / 1e6, 1) AS variance_m,
       CASE WHEN ROUND(SUM(`Variance vs plan`) / 1e6, 1) = -8.8 THEN 'PASS' ELSE 'FAIL' END AS result
FROM ${catalog}.gold.v_pbi_revenue
WHERE `Industry` = 'Industrials' AND `Quarter` = '2026-Q3'
GROUP BY `Quarter`;
