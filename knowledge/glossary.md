---
doc_id: glossary
doc_type: glossary
title: CIB Business Definitions Glossary
as_of: 2026-11-01
owner: CIB Business Management
---

# CIB Business Definitions Glossary

Northlake Bank (fictional), synthetic. This glossary is the single source of truth for how CIB Business Management defines its performance measures. The governed SQL tools, the Power BI measures and the insights agent all follow these definitions. If a number in a deck does not match a definition here, the definition wins and the deck is corrected.

Conventions used throughout: money is in Canadian dollars (CAD); rates are shown as percentages in commentary and stored as decimals in data (0.15 = 15%); quarters are calendar quarters (Q3 = July to September) unless labelled fiscal; Northlake's fiscal year runs November to October.

## Revenue measures

### Revenue
**Definition:** Revenue booked to CIB clients in the period: fees plus net interest income, after the daily data-quality checks. Recurring products (Lending, Rates & FX Hedging, Transaction Banking) accrue every month; event products (M&A Advisory, Debt Capital Markets, Equity Capital Markets) are booked on the day the deal closes, not before.
**Formula:** Sum of booked revenue for the clients and products in scope, for every day or month in the period.
**Unit:** CAD.
**Owner:** CIB Finance (Product Control).
**Source:** `gold.fact_revenue` (monthly history) and `gold.fact_revenue_daily` (daily feed); tool `get_kpi(metric='revenue')`.

### Revenue vs Plan
**Definition:** How far booked revenue is above or below the approved annual plan (budget) for the same period and scope. A negative variance is a miss. For a period that is still running (month to date or quarter to date), the plan is pro-rated by calendar day so that a partial month is compared with a partial plan.
**Formula:** Variance = Revenue − Plan. Variance % = (Revenue − Plan) ÷ |Plan|.
**Unit:** CAD and %.
**Owner:** CIB Business Management.
**Source:** `gold.fact_plan`; tools `get_kpi` (total) and `variance_drivers` (by product, client, banker, team, segment, industry or region).

### Variance driver
**Definition:** A product, client, banker, team, segment, industry or region ranked by how much of the total revenue gap it explains. Share of variance tells the reader how concentrated the miss is: when one or two drivers explain most of the gap, commentary names them; when the gap is spread thinly, commentary says so.
**Formula:** Driver share = driver variance ÷ total variance (0.85 means the driver explains 85% of the gap).
**Unit:** CAD and %.
**Owner:** CIB Business Management.
**Source:** tool `variance_drivers`.

### Prior period and year over year (YoY)
**Definition:** Prior period is the immediately preceding month or quarter; for a partial period it is the same number of days in the previous month or quarter. Year over year compares with the same period one year earlier.
**Formula:** Change = Revenue this period − Revenue comparison period. Change % = change ÷ |comparison revenue|.
**Unit:** CAD and %.
**Owner:** CIB Business Management.
**Source:** tools `get_kpi` (prior period) and `variance_drivers(compare_to='prior_period' or 'prior_year')`.

### Month to date (MTD), quarter to date (QTD) and as-of date
**Definition:** MTD and QTD run from the first day of the month or quarter to the as-of date. The as-of date is the last business day with data, normally yesterday. Every figure quoted for a running period must state its as-of date.
**Formula:** Period = first day of month (or quarter) to as-of date.
**Unit:** Dates.
**Owner:** CIB Data Office.
**Source:** `gold.config` key `as_of_date`; `gold.v_pbi_meta`.

## Profitability and capital

### RAROC
**Definition:** Risk-adjusted return on capital: what a client or portfolio earns after costs, expected credit losses and tax, for each dollar of capital the bank allocates to it. It is measured over the trailing 12 complete months so that lumpy event fees do not swing it from month to month.
**Formula:** RAROC = (Revenue − Cost allocation − Expected loss) × (1 − Tax rate) ÷ Average allocated capital. Tax rate = 25%. Trailing 12 complete months.
**Unit:** % (stored as a decimal).
**Owner:** CIB Finance with Risk Analytics (capital and expected loss).
**Source:** `gold.fact_capital`, `gold.fact_revenue`; tools `get_kpi(metric='raroc')` and `cross_sell_whitespace`.

### Hurdle rate
**Definition:** The minimum RAROC that Northlake Bank requires a client relationship to earn over time. The CIB hurdle rate is 15%. A relationship below the hurdle is not automatically exited: it is reviewed for pricing, capital usage and cross-sell first.
**Formula:** Gap to hurdle = RAROC − 15%. A negative gap means the client is below the hurdle.
**Unit:** %.
**Owner:** CIB Finance; approved annually by the CIB Executive Committee.
**Source:** `gold.config` key `hurdle_rate`; `config/constants.yaml`.

### Clients below hurdle
**Definition:** Clients whose trailing 12-month RAROC is below the 15% hurdle rate. This list is the starting point for relationship reviews and for cross-sell planning.
**Formula:** Count of clients where RAROC < 15%, trailing 12 complete months.
**Unit:** Count of clients.
**Owner:** CIB Business Management with Coverage.
**Source:** tool `cross_sell_whitespace`.

### Cross-sell whitespace
**Definition:** For a client below the hurdle, the products it does not yet hold with Northlake. A lending-only client is the classic case: the loan uses a lot of capital, and fee products such as Transaction Banking, Rates & FX Hedging or capital-markets mandates are what lift returns above the hurdle.
**Formula:** Missing products = all six CIB products − products with revenue in the trailing 12 months.
**Unit:** List of products per client.
**Owner:** Coverage, with product partners.
**Source:** tool `cross_sell_whitespace`.

### Products per client
**Definition:** The average number of CIB products a client actively uses, counting a product only if it earned revenue in the trailing 12 complete months. It measures depth of relationship.
**Formula:** Average over clients (with at least one product) of the count of products with revenue > 0, trailing 12 complete months.
**Unit:** Products per client.
**Owner:** CIB Business Management.
**Source:** tool `get_kpi(metric='products_per_client')`.

## Coverage and pipeline

### Coverage capacity
**Definition:** How many client relationships each coverage banker carries compared with the team's target. A team above its target is over capacity: bankers have less time per client, which usually shows up first in a falling win rate. Team targets are: Coverage East 1: 8, Coverage East 2: 10, Coverage West: 8, Coverage US: 8, Coverage FI & Sponsors: 12 clients per banker.
**Formula:** Clients per banker = clients owned by the team ÷ bankers in the team. Over capacity when clients per banker > target.
**Unit:** Clients per banker.
**Owner:** Coverage COO office.
**Source:** `gold.dim_banker`, `gold.dim_client`; tool `coverage_capacity`.

### Revenue per banker
**Definition:** Trailing 12-month revenue from a team's clients divided by the number of bankers in the team. A productivity measure; read it together with coverage capacity, because a stretched team can show high revenue per banker while its pipeline weakens.
**Formula:** Revenue per banker = team revenue, trailing 12 complete months ÷ bankers in the team.
**Unit:** CAD per banker.
**Owner:** Coverage COO office.
**Source:** tool `coverage_capacity`.

### Win rate
**Definition:** The share of decided deals that Northlake won. Only deals that closed in the period count; open deals are excluded until they are won or lost.
**Formula:** Win rate = deals won ÷ (deals won + deals lost), for deals closed in the period.
**Unit:** %.
**Owner:** Coverage COO office.
**Source:** `gold.fact_pipeline`; tools `get_kpi(metric='win_rate')`, `coverage_capacity` (by quarter) and `pipeline_conversion`.

### Weighted pipeline
**Definition:** The probability-weighted value of open deals at a point in time: each open deal's expected fee multiplied by the probability for the stage it has reached. Stage probabilities are Origination 10%, Pitch 25%, Mandate 60% and Execution 85%. Won and lost deals leave the pipeline.
**Formula:** Weighted pipeline = Σ (expected fee × stage probability) over deals open at the period end.
**Unit:** CAD.
**Owner:** Coverage COO office.
**Source:** `gold.fact_pipeline`; tool `get_kpi(metric='weighted_pipeline')`.

### Slipped deal
**Definition:** A deal that was expected to close in a period but did not: it was still open after its expected close date, or it closed after the period ended. How we treat a slipped deal: its fee is not counted as revenue in the original period, because event fees are booked only at close; the deal stays in the weighted pipeline at its current stage; the revenue shortfall it causes is reported as a timing variance, not as a lost deal; and commentary names the deal, its fee and the new expected close date. A slipped deal becomes a lost deal only when the client cancels the mandate or awards it elsewhere.
**Formula:** Slipped = expected close date in the period AND (still open after the expected close date OR actual close date after the period end).
**Unit:** Count of deals and CAD of expected fees.
**Owner:** Coverage, with CIB Business Management.
**Source:** tool `pipeline_conversion(detail='slipped')`.

## Controls

### Reconciliation break
**Definition:** A product and month where booked revenue does not agree with the general ledger (GL) control total by more than the $1,000 tolerance. A positive difference (GL higher than booked revenue) usually means a booking is missing; a negative one usually means a booking is duplicated or misposted. Breaks are handled under the Data-Quality and Reconciliation Break SOP.
**Formula:** Delta = GL control total − booked revenue. Break when |Delta| > $1,000.
**Unit:** CAD.
**Owner:** CIB Finance (Product Control).
**Source:** `gold.recon_control`; tool `recon_breaks`.

### Data-quality failure
**Definition:** A source row that fails a data-quality rule (missing key, unknown client, banker, product or deal, duplicate record, negative fee or revenue, probability outside 0 to 1, close date before open date). Failed rows are quarantined and kept out of reported figures until fixed at source.
**Formula:** Failure rate = rows failed ÷ rows checked, per table and rule.
**Unit:** Rows and %.
**Owner:** CIB Data Office.
**Source:** `gold.dq_summary`, `silver.dq_quarantine`.
