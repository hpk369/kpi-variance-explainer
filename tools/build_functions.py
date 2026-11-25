"""Builds tools/create_functions.sql from shared SQL pieces (period parsing, segment filter, config).

Run: python tools/build_functions.py   (then python scripts/run_sql.py tools/create_functions.sql)
"""
from pathlib import Path

G = "${catalog}.gold"


def cfg_cte() -> str:
    return f"""cfg AS (
  SELECT MAX(CASE WHEN key = 'as_of_date'      THEN try_cast(value AS DATE)   END) AS as_of,
         MAX(CASE WHEN key = 'history_start'   THEN try_cast(value AS DATE)   END) AS history_start,
         MAX(CASE WHEN key = 'hurdle_rate'     THEN try_cast(value AS DOUBLE) END) AS hurdle,
         MAX(CASE WHEN key = 'tax_rate'        THEN try_cast(value AS DOUBLE) END) AS tax,
         MAX(CASE WHEN key = 'recon_tolerance' THEN try_cast(value AS DOUBLE) END) AS tolerance
  FROM {G}.config
)"""


def period_ctes(fn: str, param: str = "period", allow_all: bool = False, allow_latest: bool = False) -> str:
    """CTEs p0 (parsed) and b (bounds): start_date, end_date (capped at as_of), nominal_end, is_partial,
    months_len, prior_start, prior_end, ttm_end_month (last complete month <= end_date), prior_ttm_end_month, err."""
    p = f"{fn}.{param}"
    extra_start = ""
    extra_end = ""
    extra_len = ""
    allowed = "YYYY-MM (e.g. 2026-09), YYYY-Qn (e.g. 2026-Q3), MTD or QTD"
    if allow_all:
        extra_start += "\n         WHEN p = 'ALL'    THEN history_start"
        extra_end += "\n         WHEN p = 'ALL'    THEN as_of"
        extra_len += "\n         WHEN p = 'ALL'    THEN 0"
        allowed += " or ALL"
    if allow_latest:
        extra_start += "\n         WHEN p = 'LATEST' THEN CASE WHEN as_of = last_day(as_of) THEN trunc(as_of, 'MM') ELSE add_months(trunc(as_of, 'MM'), -1) END"
        extra_end += "\n         WHEN p = 'LATEST' THEN CASE WHEN as_of = last_day(as_of) THEN as_of ELSE last_day(add_months(as_of, -1)) END"
        extra_len += "\n         WHEN p = 'LATEST' THEN 1"
        allowed += " or LATEST (last complete month)"
    return f"""p0 AS (
  SELECT cfg.*, upper(trim(COALESCE({p}, '-'))) AS p,
    CASE WHEN upper(trim(COALESCE({p}, '-'))) = 'MTD' THEN trunc(as_of, 'MM')
         WHEN upper(trim(COALESCE({p}, '-'))) = 'QTD' THEN trunc(as_of, 'QUARTER')
         WHEN upper(trim(COALESCE({p}, '-'))) RLIKE '^[0-9]{{4}}-Q[1-4]$'
           THEN try_cast(concat(substr(trim({p}), 1, 4), '-',
                lpad(CAST((try_cast(substr(trim({p}), 7, 1) AS INT) - 1) * 3 + 1 AS STRING), 2, '0'), '-01') AS DATE)
         WHEN upper(trim(COALESCE({p}, '-'))) RLIKE '^[0-9]{{4}}-(0[1-9]|1[0-2])$' THEN try_cast(concat(trim({p}), '-01') AS DATE)
    END AS q_start
  FROM cfg
),
p1 AS (
  SELECT *,
    CASE WHEN p IN ('MTD', 'QTD') OR p RLIKE '^[0-9]{{4}}-' THEN q_start{extra_start} END AS start_date,
    CASE WHEN p = 'MTD' THEN last_day(q_start)
         WHEN p = 'QTD' OR p RLIKE '^[0-9]{{4}}-Q' THEN last_day(add_months(q_start, 2))
         WHEN p RLIKE '^[0-9]{{4}}-[0-9]{{2}}$' THEN last_day(q_start){extra_end} END AS nominal_end,
    CASE WHEN p = 'QTD' OR p RLIKE '^[0-9]{{4}}-Q' THEN 3{extra_len} ELSE 1 END AS months_len
  FROM p0
),
b AS (
  SELECT *,
    least(nominal_end, as_of) AS end_date,
    nominal_end > as_of AS is_partial,
    add_months(start_date, -months_len) AS prior_start,
    CASE WHEN nominal_end > as_of THEN add_months(as_of, -months_len)
         ELSE last_day(add_months(nominal_end, -months_len)) END AS prior_end,
    CASE WHEN least(nominal_end, as_of) = last_day(least(nominal_end, as_of)) THEN trunc(least(nominal_end, as_of), 'MM')
         ELSE add_months(trunc(least(nominal_end, as_of), 'MM'), -1) END AS ttm_end_month,
    CASE WHEN start_date IS NULL OR nominal_end IS NULL
           THEN concat('Unknown period ''', COALESCE({p}, 'NULL'), '''. Use {allowed}.')
         WHEN start_date > as_of THEN concat('Period starts after the last day of data (', CAST(as_of AS STRING), ').')
         WHEN nominal_end < history_start THEN concat('Data starts ', CAST(history_start AS STRING), '.')
    END AS err
  FROM p1
)"""


def seg_ctes(fn: str, param: str = "segment", what: str = "segment, industry or coverage team") -> str:
    s = f"{fn}.{param}"
    return f"""cl AS (
  SELECT * FROM {G}.dim_client
  WHERE lower(trim(COALESCE({s}, 'ALL'))) IN ('all', lower(segment), lower(industry), lower(team))
),
seg AS (
  SELECT CASE WHEN lower(trim(COALESCE({s}, 'ALL'))) = 'all' OR (SELECT COUNT(*) FROM cl) > 0 THEN NULL
              ELSE concat('Unknown {param} ''', {s}, '''. Use ALL, a {what} name, e.g. Mid-Market, Industrials or Coverage East 2.')
         END AS seg_err
)"""


REV_RANGE = f"""rev AS (  -- revenue by day where the daily feed exists, by whole month for the monthly history
  SELECT client_id, product_id, revenue_date AS d, revenue FROM {G}.fact_revenue_daily
  UNION ALL
  SELECT client_id, product_id, month_start AS d, revenue FROM {G}.fact_revenue WHERE source = 'history'
)"""


def plan_sum(start: str, end: str, alias: str = "p") -> str:
    """Plan in [start, end], pro-rated by day for partial months."""
    return (f"SUM({alias}.plan_revenue * (datediff(least({end}, last_day({alias}.month_start)), greatest({start}, {alias}.month_start)) + 1)"
            f" / day(last_day({alias}.month_start)))")


FUNCS = []


def R(expr: str, kind: str) -> str:
    """Round money to cents and rates to 4 decimals (ROUND needs a constant scale)."""
    money = "metric IN ('revenue', 'weighted_pipeline')" if kind == "revenue_like" else "metric = 'revenue'"
    return f"CASE WHEN e IS NOT NULL THEN NULL WHEN {money} THEN ROUND({expr}, 2) ELSE ROUND({expr}, 4) END"


def nulls(n: int) -> str:
    return ", ".join(["NULL"] * n)

# ------------------------------------------------------------------ get_kpi
FUNCS.append(f"""
-- 1. get_kpi ---------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.get_kpi(
  metric  STRING COMMENT 'One of: revenue, raroc, win_rate, weighted_pipeline, products_per_client',
  period  STRING COMMENT 'YYYY-MM (e.g. 2026-09), YYYY-Qn (e.g. 2026-Q3), MTD (month to date) or QTD (quarter to date)',
  segment STRING DEFAULT 'ALL' COMMENT 'ALL, a client segment (Large Corporate, Mid-Market, Financial Institutions, Sponsor), an industry (e.g. Industrials) or a coverage team (e.g. Coverage East 2)'
)
RETURNS TABLE (
  metric STRING, period STRING, segment STRING, period_start DATE, period_end DATE, is_partial BOOLEAN,
  actual DOUBLE, plan DOUBLE, var_vs_plan DOUBLE, pct_vs_plan DOUBLE,
  prior_period DOUBLE, var_vs_prior DOUBLE, pct_vs_prior DOUBLE, prior_period_start DATE, prior_period_end DATE,
  unit STRING, as_of_date DATE, note STRING, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this to get ONE KPI for one period and one slice, with plan and prior period. \
Metrics: revenue (CAD, booked revenue in the period; plan is the budget, pro-rated by day for MTD/QTD), \
raroc (decimal, trailing 12 complete months to the period end = (revenue - cost - expected loss) x (1 - tax) / average allocated capital; plan uses planned revenue), \
win_rate (decimal, won / (won + lost) for deals closed in the period; no plan), \
weighted_pipeline (CAD, open deals at the period end x stage probability; no plan), \
products_per_client (average number of products with revenue, trailing 12 complete months; no plan). \
Prior period = previous month or quarter (same number of days for MTD/QTD). Example: get_kpi(''revenue'', ''2026-Q3'', ''Industrials''). \
If error is not null the inputs were invalid: fix them and call again.'
RETURN
WITH {cfg_cte()},
{period_ctes("get_kpi")},
{seg_ctes("get_kpi")},
m AS (
  SELECT lower(trim(COALESCE(get_kpi.metric, '-'))) AS metric,
         CASE WHEN lower(trim(COALESCE(get_kpi.metric, '-'))) NOT IN ('revenue', 'raroc', 'win_rate', 'weighted_pipeline', 'products_per_client')
              THEN concat('Unknown metric ''', COALESCE(get_kpi.metric, 'NULL'), '''. Use revenue, raroc, win_rate, weighted_pipeline or products_per_client.') END AS metric_err
),
ranges AS (
  SELECT 'cur' AS which, start_date AS s, end_date AS e, ttm_end_month AS tm FROM b
  UNION ALL
  SELECT 'prior', prior_start, prior_end,
         CASE WHEN prior_end = last_day(prior_end) THEN trunc(prior_end, 'MM') ELSE add_months(trunc(prior_end, 'MM'), -1) END FROM b
),
{REV_RANGE},
rev_r AS (
  SELECT r.which, SUM(x.revenue) AS v
  FROM ranges r JOIN rev x ON x.d BETWEEN r.s AND r.e JOIN cl ON cl.client_id = x.client_id GROUP BY r.which
),
plan_r AS (
  SELECT r.which, {plan_sum("r.s", "r.e")} AS v
  FROM ranges r JOIN {G}.fact_plan p ON p.month_start <= r.e AND last_day(p.month_start) >= r.s
  JOIN cl ON cl.client_id = p.client_id GROUP BY r.which
),
cap_w AS (
  SELECT r.which, c.client_id, AVG(c.allocated_capital) AS cap, SUM(c.expected_loss) AS el, SUM(c.cost_allocation) AS cost
  FROM ranges r JOIN {G}.fact_capital c ON c.month_start BETWEEN add_months(r.tm, -11) AND r.tm
  JOIN cl ON cl.client_id = c.client_id GROUP BY r.which, c.client_id
),
rev_w AS (
  SELECT r.which, f.client_id, SUM(f.revenue) AS rev, COUNT(DISTINCT CASE WHEN f.revenue > 0 THEN f.product_id END) AS products
  FROM ranges r JOIN {G}.fact_revenue f ON f.month_start BETWEEN add_months(r.tm, -11) AND r.tm
  JOIN cl ON cl.client_id = f.client_id GROUP BY r.which, f.client_id
),
plan_w AS (
  SELECT r.which, p.client_id, SUM(p.plan_revenue) AS plan
  FROM ranges r JOIN {G}.fact_plan p ON p.month_start BETWEEN add_months(r.tm, -11) AND r.tm
  JOIN cl ON cl.client_id = p.client_id GROUP BY r.which, p.client_id
),
raroc_r AS (
  SELECT k.which,
         SUM(COALESCE(rw.rev, 0) - k.cost - k.el) * (1 - MAX(cfg.tax)) / SUM(k.cap)  AS v,
         SUM(COALESCE(pw.plan, 0) - k.cost - k.el) * (1 - MAX(cfg.tax)) / SUM(k.cap) AS v_plan
  FROM cap_w k CROSS JOIN cfg
  LEFT JOIN rev_w rw ON rw.which = k.which AND rw.client_id = k.client_id
  LEFT JOIN plan_w pw ON pw.which = k.which AND pw.client_id = k.client_id
  GROUP BY k.which
),
ppc_r AS (
  SELECT which, AVG(products) AS v FROM rev_w WHERE products > 0 GROUP BY which
),
win_r AS (
  SELECT r.which, AVG(CASE WHEN d.status = 'Won' THEN 1.0 ELSE 0.0 END) AS v
  FROM ranges r JOIN {G}.fact_pipeline d ON d.actual_close_date BETWEEN r.s AND r.e AND d.status IN ('Won', 'Lost')
  JOIN cl ON cl.client_id = d.client_id GROUP BY r.which
),
wp_r AS (
  SELECT r.which,
         SUM(d.expected_fee * CASE WHEN d.execution_date <= r.e THEN 0.85 WHEN d.mandate_date <= r.e THEN 0.60
                                   WHEN d.pitch_date <= r.e THEN 0.25 ELSE 0.10 END) AS v
  FROM ranges r JOIN {G}.fact_pipeline d ON d.open_date <= r.e AND (d.actual_close_date IS NULL OR d.actual_close_date > r.e)
  JOIN cl ON cl.client_id = d.client_id GROUP BY r.which
),
vals AS (
  SELECT r.which,
    CASE m.metric WHEN 'revenue' THEN COALESCE(rv.v, 0) WHEN 'raroc' THEN rr.v WHEN 'win_rate' THEN wr.v
                  WHEN 'weighted_pipeline' THEN COALESCE(wp.v, 0) WHEN 'products_per_client' THEN pc.v END AS v,
    CASE m.metric WHEN 'revenue' THEN COALESCE(pl.v, 0) WHEN 'raroc' THEN rr.v_plan END AS v_plan
  FROM ranges r CROSS JOIN m
  LEFT JOIN rev_r rv ON rv.which = r.which LEFT JOIN plan_r pl ON pl.which = r.which
  LEFT JOIN raroc_r rr ON rr.which = r.which LEFT JOIN ppc_r pc ON pc.which = r.which
  LEFT JOIN win_r wr ON wr.which = r.which LEFT JOIN wp_r wp ON wp.which = r.which
),
out AS (
  SELECT m.metric, b.*, seg.seg_err, m.metric_err,
         cur.v AS actual, cur.v_plan AS plan, pri.v AS prior_v
  FROM b CROSS JOIN m CROSS JOIN seg
  LEFT JOIN vals cur ON cur.which = 'cur' LEFT JOIN vals pri ON pri.which = 'prior'
)
SELECT
  metric, get_kpi.period, COALESCE(get_kpi.segment, 'ALL'),
  CASE WHEN e IS NULL THEN start_date END, CASE WHEN e IS NULL THEN end_date END, CASE WHEN e IS NULL THEN is_partial END,
  {R("actual", "revenue_like")},
  {R("plan", "revenue")},
  {R("actual - plan", "revenue")},
  CASE WHEN e IS NULL THEN ROUND(try_divide(actual - plan, abs(plan)), 4) END,
  {R("prior_v", "revenue_like")},
  {R("actual - prior_v", "revenue_like")},
  CASE WHEN e IS NULL THEN ROUND(try_divide(actual - prior_v, abs(prior_v)), 4) END,
  CASE WHEN e IS NULL THEN prior_start END, CASE WHEN e IS NULL THEN prior_end END,
  CASE WHEN metric IN ('revenue', 'weighted_pipeline') THEN 'CAD' WHEN metric IN ('raroc', 'win_rate') THEN 'decimal (0.15 = 15%)'
       WHEN metric = 'products_per_client' THEN 'products per client' END,
  as_of,
  CASE WHEN e IS NOT NULL THEN NULL
       WHEN metric = 'revenue' AND is_partial THEN concat('Partial period to ', CAST(end_date AS STRING), '; plan pro-rated by day.')
       WHEN metric = 'revenue' THEN 'Booked revenue vs budget.'
       WHEN metric = 'raroc' THEN concat('Trailing 12 complete months to ', date_format(ttm_end_month, 'yyyy-MM'),
                                         '; RAROC = (revenue - cost - expected loss) x (1 - tax) / average allocated capital. Plan uses planned revenue.')
       WHEN metric = 'win_rate' THEN 'Won / (won + lost) for deals closed in the period. No plan.'
       WHEN metric = 'weighted_pipeline' THEN 'Open deals at the period end x stage probability. No plan.'
       WHEN metric = 'products_per_client' THEN concat('Trailing 12 complete months to ', date_format(ttm_end_month, 'yyyy-MM'), '. No plan.')
  END,
  e
FROM (SELECT *, COALESCE(metric_err, err, seg_err) AS e FROM out)
""")

# ------------------------------------------------------------------ variance_drivers
FUNCS.append(f"""
-- 2. variance_drivers ------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.variance_drivers(
  metric     STRING COMMENT 'Only revenue is supported',
  period     STRING COMMENT 'YYYY-MM, YYYY-Qn, MTD or QTD',
  compare_to STRING DEFAULT 'plan' COMMENT 'plan, prior_period (previous month or quarter) or prior_year (same period a year earlier)',
  dimension  STRING DEFAULT 'product' COMMENT 'product, client, banker, team, segment, industry or region',
  segment    STRING DEFAULT 'ALL' COMMENT 'Filter: ALL, a segment, an industry or a coverage team',
  top_n      INT    DEFAULT 10 COMMENT 'Rows to return, largest contributors first (1-50)'
)
RETURNS TABLE (
  dimension STRING, dimension_id STRING, dimension_value STRING, actual DOUBLE, comparison DOUBLE, delta DOUBLE,
  share_of_total_delta DOUBLE, rank INT, total_actual DOUBLE, total_comparison DOUBLE, total_delta DOUBLE,
  compare_to STRING, period_start DATE, period_end DATE, comparison_start DATE, comparison_end DATE, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this to explain WHY revenue moved: it splits the gap between actual revenue and \
the plan (or the prior period, or the prior year) by product, client, banker, team, segment, industry or region, and ranks the drivers. \
share_of_total_delta is each row''s share of the total gap (0.85 = 85%). Money in CAD. \
Example: variance_drivers(''revenue'', ''2026-Q3'', ''plan'', ''product'', ''Industrials''). If error is not null, fix the inputs.'
RETURN
WITH {cfg_cte()},
{period_ctes("variance_drivers")},
{seg_ctes("variance_drivers")},
args AS (
  SELECT lower(trim(COALESCE(variance_drivers.compare_to, 'plan'))) AS cmp,
         lower(trim(COALESCE(variance_drivers.dimension, 'product'))) AS dim,
         LEAST(GREATEST(COALESCE(variance_drivers.top_n, 10), 1), 50) AS n,
         CASE WHEN lower(trim(COALESCE(variance_drivers.metric, '-'))) <> 'revenue'
                THEN 'Only metric revenue is supported. For RAROC use cross_sell_whitespace or get_kpi.'
              WHEN lower(trim(COALESCE(variance_drivers.compare_to, 'plan'))) NOT IN ('plan', 'prior_period', 'prior_year')
                THEN 'compare_to must be plan, prior_period or prior_year.'
              WHEN lower(trim(COALESCE(variance_drivers.dimension, 'product'))) NOT IN ('product', 'client', 'banker', 'team', 'segment', 'industry', 'region')
                THEN 'dimension must be product, client, banker, team, segment, industry or region.' END AS arg_err
),
win AS (
  SELECT b.*, args.*,
         CASE args.cmp WHEN 'prior_period' THEN prior_start WHEN 'prior_year' THEN add_months(start_date, -12) ELSE start_date END AS cs,
         CASE args.cmp WHEN 'prior_period' THEN prior_end   WHEN 'prior_year' THEN add_months(end_date, -12)   ELSE end_date END AS ce,
         COALESCE(args.arg_err, b.err, seg.seg_err) AS e
  FROM b CROSS JOIN args CROSS JOIN seg
),
keyed AS (  -- the dimension key and label for every client x product
  SELECT c.client_id, pr.product_id,
         CASE w.dim WHEN 'product' THEN pr.product_id WHEN 'client' THEN c.client_id WHEN 'banker' THEN c.coverage_banker_id
                    WHEN 'team' THEN c.team WHEN 'segment' THEN c.segment WHEN 'industry' THEN c.industry WHEN 'region' THEN c.region END AS k,
         CASE w.dim WHEN 'product' THEN pr.product_name WHEN 'client' THEN c.client_name WHEN 'banker' THEN bk.banker_name
                    WHEN 'team' THEN c.team WHEN 'segment' THEN c.segment WHEN 'industry' THEN c.industry WHEN 'region' THEN c.region END AS label
  FROM cl c CROSS JOIN {G}.dim_product pr CROSS JOIN win w
  LEFT JOIN {G}.dim_banker bk ON bk.banker_id = c.coverage_banker_id
),
{REV_RANGE},
act AS (
  SELECT kk.k, MAX(kk.label) AS label, SUM(x.revenue) AS v
  FROM rev x CROSS JOIN win w JOIN keyed kk ON kk.client_id = x.client_id AND kk.product_id = x.product_id
  WHERE x.d BETWEEN w.start_date AND w.end_date GROUP BY kk.k
),
cmp_rev AS (
  SELECT kk.k, MAX(kk.label) AS label, SUM(x.revenue) AS v
  FROM rev x CROSS JOIN win w JOIN keyed kk ON kk.client_id = x.client_id AND kk.product_id = x.product_id
  WHERE w.cmp <> 'plan' AND x.d BETWEEN w.cs AND w.ce GROUP BY kk.k
),
cmp_plan AS (
  SELECT kk.k, MAX(kk.label) AS label, {plan_sum("w.cs", "w.ce")} AS v
  FROM {G}.fact_plan p CROSS JOIN win w JOIN keyed kk ON kk.client_id = p.client_id AND kk.product_id = p.product_id
  WHERE w.cmp = 'plan' AND p.month_start <= w.ce AND last_day(p.month_start) >= w.cs GROUP BY kk.k
),
cmp AS (SELECT * FROM cmp_rev UNION ALL SELECT * FROM cmp_plan),
joined AS (
  SELECT COALESCE(a.k, c.k) AS k, COALESCE(a.label, c.label) AS label, COALESCE(a.v, 0) AS actual, COALESCE(c.v, 0) AS comparison
  FROM act a FULL OUTER JOIN cmp c ON a.k = c.k
),
tot AS (SELECT SUM(actual) AS t_act, SUM(comparison) AS t_cmp, SUM(actual - comparison) AS t_delta FROM joined),
ranked AS (  -- biggest contributors to the total gap first
  SELECT j.*, j.actual - j.comparison AS delta, t.t_act, t.t_cmp, t.t_delta,
         ROW_NUMBER() OVER (ORDER BY (j.actual - j.comparison) * CASE WHEN t.t_delta < 0 THEN 1 ELSE -1 END, j.k) AS rnk
  FROM joined j CROSS JOIN tot t
)
SELECT w.dim, r.k, r.label, ROUND(r.actual, 2), ROUND(r.comparison, 2), ROUND(r.delta, 2),
       ROUND(try_divide(r.delta, r.t_delta), 4), CAST(r.rnk AS INT), ROUND(r.t_act, 2), ROUND(r.t_cmp, 2), ROUND(r.t_delta, 2),
       w.cmp, w.start_date, w.end_date, w.cs, w.ce, CAST(NULL AS STRING)
FROM ranked r CROSS JOIN win w
WHERE w.e IS NULL AND r.rnk <= w.n
UNION ALL
SELECT variance_drivers.dimension, {nulls(10)},
       variance_drivers.compare_to, {nulls(4)}, w.e
FROM win w WHERE w.e IS NOT NULL
""")

# ------------------------------------------------------------------ pipeline_conversion
FUNCS.append(f"""
-- 3. pipeline_conversion ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.pipeline_conversion(
  period  STRING COMMENT 'YYYY-MM, YYYY-Qn, MTD or QTD: deals whose expected close date falls in this period',
  segment STRING DEFAULT 'ALL' COMMENT 'ALL, a segment, an industry or a coverage team',
  detail  STRING DEFAULT 'summary' COMMENT 'summary (one row per outcome), slipped (one row per slipped deal) or deals (one row per deal)'
)
RETURNS TABLE (
  row_type STRING, outcome STRING, deals BIGINT, expected_fee DOUBLE, actual_fee DOUBLE, share_of_deals DOUBLE,
  share_of_expected_fee DOUBLE, cohort_deals BIGINT, cohort_expected_fee DOUBLE, cohort_win_rate DOUBLE,
  deal_id STRING, deal_name STRING, client_id STRING, client_name STRING, industry STRING, product_name STRING,
  banker_name STRING, team STRING, stage_now STRING, status_now STRING, expected_close_date DATE, actual_close_date DATE,
  days_late INT, period_start DATE, period_end DATE, as_of_date DATE, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this to see what happened to the deals that were EXPECTED to close in a period: \
won_on_time (won by the period end), lost (lost by the period end), slipped (still open after the expected close date, or closed after the \
period end; stage_now shows where it is now) and not_yet_due (expected later in a period that is still running). \
Slipped deals explain revenue that moved out of the period. cohort_win_rate = won / (won + lost) among deals that closed in the period. \
Money in CAD. Example: pipeline_conversion(''2026-Q3'', ''Industrials'', ''slipped''). If error is not null, fix the inputs.'
RETURN
WITH {cfg_cte()},
{period_ctes("pipeline_conversion")},
{seg_ctes("pipeline_conversion")},
args AS (
  SELECT lower(trim(COALESCE(pipeline_conversion.detail, 'summary'))) AS detail,
         CASE WHEN lower(trim(COALESCE(pipeline_conversion.detail, 'summary'))) NOT IN ('summary', 'slipped', 'deals')
              THEN 'detail must be summary, slipped or deals.' END AS arg_err
),
w AS (SELECT b.*, args.detail, COALESCE(args.arg_err, b.err, seg.seg_err) AS e FROM b CROSS JOIN args CROSS JOIN seg),
cohort AS (
  SELECT d.*, c.client_name, c.industry, pr.product_name, bk.banker_name, bk.team,
         CASE WHEN d.status = 'Won'  AND d.actual_close_date <= w.nominal_end THEN 'won_on_time'
              WHEN d.status = 'Lost' AND d.actual_close_date <= w.nominal_end THEN 'lost'
              WHEN d.status = 'Open' AND d.expected_close_date > w.as_of THEN 'not_yet_due'
              ELSE 'slipped' END AS outcome,
         CASE WHEN COALESCE(d.actual_close_date, w.as_of) > d.expected_close_date
              THEN datediff(COALESCE(d.actual_close_date, w.as_of), d.expected_close_date) ELSE 0 END AS late
  FROM {G}.fact_pipeline d CROSS JOIN w
  JOIN cl c ON c.client_id = d.client_id
  JOIN {G}.dim_product pr ON pr.product_id = d.product_id
  LEFT JOIN {G}.dim_banker bk ON bk.banker_id = d.banker_id
  WHERE w.e IS NULL AND d.expected_close_date BETWEEN w.start_date AND w.nominal_end
),
tot AS (
  SELECT COUNT(*) AS n, SUM(expected_fee) AS fee,
         try_divide(SUM(CASE WHEN outcome = 'won_on_time' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN outcome IN ('won_on_time', 'lost') THEN 1 ELSE 0 END)) AS wr
  FROM cohort
),
summary AS (
  SELECT 'summary' AS row_type, o.outcome, COUNT(c.deal_id) AS deals, ROUND(COALESCE(SUM(c.expected_fee), 0), 2) AS expected_fee,
         ROUND(COALESCE(SUM(c.actual_fee), 0), 2) AS actual_fee
  FROM (SELECT explode(array('won_on_time', 'lost', 'slipped', 'not_yet_due')) AS outcome) o
  LEFT JOIN cohort c ON c.outcome = o.outcome
  GROUP BY o.outcome
)
SELECT s.row_type, s.outcome, s.deals, s.expected_fee, s.actual_fee,
       ROUND(try_divide(s.deals, t.n), 4), ROUND(try_divide(s.expected_fee, t.fee), 4), t.n, ROUND(t.fee, 2), ROUND(t.wr, 4),
       {nulls(13)},
       w.start_date, w.nominal_end, w.as_of, NULL
FROM summary s CROSS JOIN tot t CROSS JOIN w
WHERE w.e IS NULL AND w.detail = 'summary'
UNION ALL
SELECT 'deal', c.outcome, 1, ROUND(c.expected_fee, 2), ROUND(c.actual_fee, 2), NULL, NULL, t.n, ROUND(t.fee, 2), ROUND(t.wr, 4),
       c.deal_id, c.deal_name, c.client_id, c.client_name, c.industry, c.product_name, c.banker_name, c.team, c.stage, c.status,
       c.expected_close_date, c.actual_close_date, CAST(c.late AS INT), w.start_date, w.nominal_end, w.as_of, NULL
FROM cohort c CROSS JOIN tot t CROSS JOIN w
WHERE w.e IS NULL AND (w.detail = 'deals' OR (w.detail = 'slipped' AND c.outcome = 'slipped'))
UNION ALL
SELECT 'error', {nulls(24)}, w.as_of, w.e
FROM w WHERE w.e IS NOT NULL
""")

# ------------------------------------------------------------------ cross_sell_whitespace
FUNCS.append(f"""
-- 4. cross_sell_whitespace -------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.cross_sell_whitespace(
  segment   STRING DEFAULT 'Mid-Market' COMMENT 'ALL, a segment, an industry or a coverage team',
  max_raroc DOUBLE DEFAULT NULL COMMENT 'Return clients with RAROC below this decimal (0.15 = 15%). NULL = the bank hurdle rate (15%)',
  period    STRING DEFAULT 'LATEST' COMMENT 'End of the trailing 12-month window: LATEST (last complete month), YYYY-MM or YYYY-Qn'
)
RETURNS TABLE (
  client_id STRING, client_name STRING, segment STRING, industry STRING, team STRING, coverage_banker STRING,
  raroc DOUBLE, max_raroc DOUBLE, hurdle_rate DOUBLE, gap_to_hurdle DOUBLE, revenue_ttm DOUBLE, allocated_capital_avg DOUBLE,
  products_held STRING, products_held_count INT, missing_products STRING, window_start DATE, window_end DATE, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this to find cross-sell whitespace: clients whose RAROC over the trailing \
12 complete months is below a threshold (default: the 15% hurdle), with the products they hold and the products they do not. \
RAROC = (revenue - cost - expected loss) x (1 - tax) / average allocated capital, as a decimal. gap_to_hurdle = raroc - hurdle_rate. \
Lowest RAROC first. Example: cross_sell_whitespace(''Mid-Market'', 0.15). If error is not null, fix the inputs.'
RETURN
WITH {cfg_cte()},
{period_ctes("cross_sell_whitespace", allow_latest=True)},
{seg_ctes("cross_sell_whitespace")},
w AS (
  SELECT b.*, seg.seg_err, COALESCE(cross_sell_whitespace.max_raroc, b.hurdle) AS thr,
         add_months(b.ttm_end_month, -11) AS ws, b.ttm_end_month AS we,
         COALESCE(b.err, seg.seg_err,
                  CASE WHEN b.p IN ('MTD', 'QTD') THEN 'Use LATEST, YYYY-MM or YYYY-Qn for this tool.' END,
                  CASE WHEN add_months(b.ttm_end_month, -11) < trunc(b.history_start, 'MM') THEN 'Needs 12 months of history: choose 2025-09 or later.' END) AS e
  FROM b CROSS JOIN seg
),
cap AS (
  SELECT c.client_id, AVG(c.allocated_capital) AS cap, SUM(c.expected_loss) AS el, SUM(c.cost_allocation) AS cost
  FROM {G}.fact_capital c CROSS JOIN w JOIN cl ON cl.client_id = c.client_id
  WHERE c.month_start BETWEEN w.ws AND w.we GROUP BY c.client_id
),
rev AS (
  SELECT f.client_id, SUM(f.revenue) AS rev,
         array_sort(collect_set(CASE WHEN f.revenue > 0 THEN p.product_name END)) AS held
  FROM {G}.fact_revenue f CROSS JOIN w JOIN cl ON cl.client_id = f.client_id JOIN {G}.dim_product p ON p.product_id = f.product_id
  WHERE f.month_start BETWEEN w.ws AND w.we GROUP BY f.client_id
),
allp AS (SELECT array_sort(collect_list(product_name)) AS products FROM {G}.dim_product),
k AS (
  SELECT c.*, cap.cap, COALESCE(rev.rev, 0) AS rev, COALESCE(rev.held, array()) AS held,
         (COALESCE(rev.rev, 0) - cap.cost - cap.el) * (1 - w.tax) / cap.cap AS raroc
  FROM cap JOIN cl c ON c.client_id = cap.client_id LEFT JOIN rev ON rev.client_id = cap.client_id CROSS JOIN w
)
SELECT k.client_id, k.client_name, k.segment, k.industry, k.team, bk.banker_name,
       ROUND(k.raroc, 4), w.thr, w.hurdle, ROUND(k.raroc - w.hurdle, 4), ROUND(k.rev, 2), ROUND(k.cap, 2),
       array_join(k.held, ', '), CAST(size(k.held) AS INT), array_join(array_except(allp.products, k.held), ', '),
       w.ws, last_day(w.we), CAST(NULL AS STRING)
FROM k CROSS JOIN w CROSS JOIN allp LEFT JOIN {G}.dim_banker bk ON bk.banker_id = k.coverage_banker_id
WHERE w.e IS NULL AND k.raroc < w.thr
UNION ALL
SELECT {nulls(17)}, w.e
FROM w WHERE w.e IS NOT NULL
ORDER BY 7 ASC NULLS LAST, 1
""")

# ------------------------------------------------------------------ coverage_capacity
FUNCS.append(f"""
-- 5. coverage_capacity -----------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.coverage_capacity(
  team     STRING DEFAULT 'ALL' COMMENT 'ALL or a coverage team: Coverage East 1, Coverage East 2, Coverage West, Coverage US, Coverage FI & Sponsors',
  quarters INT    DEFAULT 4 COMMENT 'How many complete quarters of win rate to return (1-8), most recent last'
)
RETURNS TABLE (
  team STRING, quarter STRING, bankers BIGINT, clients BIGINT, clients_per_banker DOUBLE, capacity_target INT,
  over_capacity BOOLEAN, revenue_ttm DOUBLE, revenue_per_banker_ttm DOUBLE, meetings_per_client_ttm DOUBLE,
  deals_closed BIGINT, deals_won BIGINT, win_rate DOUBLE, ttm_window_end DATE, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this for coverage capacity and productivity by team: clients per banker \
against the team target (over_capacity = true when above it), trailing 12-month revenue per banker and meetings per client, and the \
win rate (won / (won + lost)) of deals closed in each of the last complete quarters. One row per team and quarter. \
Example: coverage_capacity(''ALL''). If error is not null, fix the inputs.'
RETURN
WITH {cfg_cte()},
args AS (
  SELECT cfg.*, LEAST(GREATEST(COALESCE(coverage_capacity.quarters, 4), 1), 8) AS n,
         CASE WHEN as_of = last_day(as_of) THEN trunc(as_of, 'MM') ELSE add_months(trunc(as_of, 'MM'), -1) END AS ttm_end,
         CASE WHEN as_of = last_day(add_months(trunc(as_of, 'QUARTER'), 2)) THEN trunc(as_of, 'QUARTER')
              ELSE add_months(trunc(as_of, 'QUARTER'), -3) END AS last_q
  FROM cfg
),
teams AS (
  SELECT team, COUNT(*) AS bankers, MAX(capacity_target) AS target FROM {G}.dim_banker
  WHERE lower(trim(COALESCE(coverage_capacity.team, 'ALL'))) IN ('all', lower(team)) GROUP BY team
),
chk AS (
  SELECT CASE WHEN (SELECT COUNT(*) FROM teams) = 0
              THEN concat('Unknown team ''', coverage_capacity.team, '''. Use ALL, Coverage East 1, Coverage East 2, Coverage West, Coverage US or Coverage FI & Sponsors.') END AS e
),
qs AS (
  SELECT explode(sequence(add_months(last_q, -3 * (n - 1)), last_q, interval 3 month)) AS q_start FROM args
),
clients AS (SELECT team, COUNT(*) AS n FROM {G}.dim_client GROUP BY team),
rev AS (
  SELECT c.team, SUM(f.revenue) AS rev FROM {G}.fact_revenue f JOIN {G}.dim_client c USING (client_id) CROSS JOIN args a
  WHERE f.month_start BETWEEN add_months(a.ttm_end, -11) AND a.ttm_end GROUP BY c.team
),
cov AS (
  SELECT c.team, SUM(v.meetings) AS meetings FROM {G}.fact_coverage v JOIN {G}.dim_client c USING (client_id) CROSS JOIN args a
  WHERE v.month_start BETWEEN add_months(a.ttm_end, -11) AND a.ttm_end GROUP BY c.team
),
wr AS (
  SELECT bk.team, trunc(d.actual_close_date, 'QUARTER') AS q_start, COUNT(*) AS closed, SUM(CASE WHEN d.status = 'Won' THEN 1 ELSE 0 END) AS won
  FROM {G}.fact_pipeline d JOIN {G}.dim_banker bk USING (banker_id)
  WHERE d.status IN ('Won', 'Lost') GROUP BY bk.team, trunc(d.actual_close_date, 'QUARTER')
)
SELECT t.team, concat(year(q.q_start), '-Q', quarter(q.q_start)), t.bankers, COALESCE(cl.n, 0),
       ROUND(COALESCE(cl.n, 0) / t.bankers, 2), CAST(t.target AS INT), COALESCE(cl.n, 0) / t.bankers > t.target,
       ROUND(COALESCE(r.rev, 0), 2), ROUND(COALESCE(r.rev, 0) / t.bankers, 2), ROUND(try_divide(cv.meetings, cl.n), 2),
       COALESCE(w.closed, 0), COALESCE(w.won, 0), ROUND(try_divide(w.won, w.closed), 4), last_day(a.ttm_end), CAST(NULL AS STRING)
FROM teams t CROSS JOIN qs q CROSS JOIN args a
LEFT JOIN clients cl ON cl.team = t.team LEFT JOIN rev r ON r.team = t.team LEFT JOIN cov cv ON cv.team = t.team
LEFT JOIN wr w ON w.team = t.team AND w.q_start = q.q_start
UNION ALL
SELECT coverage_capacity.team, {nulls(13)}, chk.e
FROM chk WHERE chk.e IS NOT NULL
ORDER BY 1, 2
""")

# ------------------------------------------------------------------ recon_breaks
FUNCS.append(f"""
-- 6. recon_breaks ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {G}.recon_breaks(
  period STRING DEFAULT 'ALL' COMMENT 'ALL, YYYY-MM, YYYY-Qn, MTD or QTD'
)
RETURNS TABLE (
  status STRING, product_id STRING, product_name STRING, month STRING, control_total DOUBLE, fact_total DOUBLE,
  delta DOUBLE, tolerance DOUBLE, months_checked BIGINT, period_start DATE, period_end DATE, note STRING, error STRING
)
COMMENT 'Northlake Bank (fictional), synthetic data. Use this to check whether booked revenue reconciles to the general ledger: \
GL control total vs the sum of booked revenue, by product and month. Returns one row per break (|delta| above the tolerance, $1,000) \
with status = break, or a single row with status = no_breaks. delta = control_total - fact_total in CAD; a positive delta means \
revenue is missing from the books. Example: recon_breaks(''2026-Q2''). If error is not null, fix the inputs.'
RETURN
WITH {cfg_cte()},
{period_ctes("recon_breaks", allow_all=True)},
w AS (SELECT b.* FROM b),
r AS (
  SELECT rc.* FROM {G}.recon_control rc CROSS JOIN w
  WHERE w.err IS NULL AND rc.month_start BETWEEN trunc(w.start_date, 'MM') AND w.end_date
)
SELECT 'break', r.product_id, r.product_name, date_format(r.month_start, 'yyyy-MM'), r.control_total, r.fact_total, r.delta, r.tolerance,
       (SELECT COUNT(*) FROM r), w.start_date, w.end_date,
       CASE WHEN r.delta > 0 THEN 'GL is higher than booked revenue: a booking may be missing.'
            ELSE 'Booked revenue is higher than the GL: a booking may be duplicated or misposted.' END, CAST(NULL AS STRING)
FROM r CROSS JOIN w WHERE r.is_break
UNION ALL
SELECT 'no_breaks', {nulls(6)}, w.tolerance, (SELECT COUNT(*) FROM r), w.start_date, w.end_date,
       'Every product and month reconciles within the tolerance.', CAST(NULL AS STRING)
FROM w WHERE w.err IS NULL AND NOT EXISTS (SELECT 1 FROM r WHERE r.is_break)
UNION ALL
SELECT 'error', {nulls(11)}, w.err FROM w WHERE w.err IS NOT NULL
""")

HEADER = """-- Northlake Bank (fictional) · governed Unity Catalog tools. Synthetic data only.
-- The agent gets numbers ONLY through these six functions. Each COMMENT is the tool description the LLM reads.
-- Bad input never fails: it returns a row with the error column filled in.
-- Run: python scripts/run_sql.py tools/create_functions.sql
-- Generated by tools/build_functions.py: change that file, not this one, then rebuild.
--
--   get_kpi(metric, period, segment)                                one KPI vs plan and prior period
--   variance_drivers(metric, period, compare_to, dimension, segment, top_n)   what explains a revenue gap
--   pipeline_conversion(period, segment, detail)                    what happened to deals expected to close
--   cross_sell_whitespace(segment, max_raroc, period)               clients below the RAROC hurdle, products missing
--   coverage_capacity(team, quarters)                               clients per banker, revenue per banker, win rate
--   recon_breaks(period)                                            GL vs booked revenue breaks
--
-- Periods: YYYY-MM, YYYY-Qn, MTD, QTD (to gold.config as_of_date). Money in CAD, rates as decimals.
"""
FOOTER = """
-- Check: the six functions and their comments
SELECT routine_name, left(comment, 80) AS comment
FROM ${catalog}.information_schema.routines
WHERE routine_schema = 'gold'
ORDER BY routine_name;
"""
import re  # noqa: E402

body = HEADER + "".join(f.rstrip() + ";\n" for f in FUNCS) + FOOTER
# Spark SQL escapes a quote inside a literal as \' ('' would just join two literals).
Q, BS = "'", "\\"
body = re.sub(r"(?<!')''(?!')", BS + BS + Q, body)               # x''y''  ->  x\'y\'
body = re.sub(r"'''(?=\s*[,)])", BS + BS + Q + Q, body)          # x''',   ->  x\'',   (escaped quote, then close)
body = body.replace(Q * 3, Q + BS + Q)                            # , '''.  ->  , '\'.  (open, then escaped quote)
out = Path(__file__).resolve().parent / "create_functions.sql"
out.parent.mkdir(exist_ok=True)
out.write_text(body)
print(f"{out}: {len(body.splitlines())} lines")
