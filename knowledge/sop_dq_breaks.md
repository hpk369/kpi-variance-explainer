---
doc_id: sop_dq_breaks
doc_type: sop
title: "SOP: Data-Quality (DQ) and Reconciliation Breaks"
as_of: 2026-11-01
owner: CIB Finance (Product Control) and CIB Data Office
---

# SOP: Data-Quality (DQ) and Reconciliation Breaks

Northlake Bank (fictional), synthetic. Standard operating procedure for handling reconciliation breaks and data-quality failures in CIB performance data. Applies to every figure used in management reporting, the Power BI dashboard and the insights agent.

## Scope and triggers
This SOP starts when the daily pipeline flags either of the following:
- **Reconciliation break:** a product and month in `gold.recon_control` with `is_break = true`, meaning booked revenue differs from the general ledger (GL) control total by more than the $1,000 tolerance.
- **Data-quality failure:** a table and rule in `gold.dq_summary` with `rows_failed > 0`. Failed rows are held in `silver.dq_quarantine` and kept out of reported figures.

The pipeline's checks task reports an expected, already-logged break as a warning and any new break or failure as a failure.

## Severity
- **Severity 1:** a reconciliation break of $1M or more, or any break open at a second month-end.
- **Severity 2:** a reconciliation break between $100,000 and $1M, or a data-quality failure that changes a reported revenue figure.
- **Severity 3:** a reconciliation break under $100,000, or a data-quality failure that does not change revenue (for example a missing banker on a coverage record).

## Triage steps
1. **Log the break** within one business day in the break register: product, month, GL control total, booked revenue, delta, severity, owner. For data-quality failures, log table, rule and rows failed.
2. **Read the sign of the delta.** Delta = GL control total − booked revenue. A positive delta means the GL is higher: a booking is probably missing. A negative delta means booked revenue is higher: a booking is probably duplicated or posted to the wrong product.
3. **Find the source.** Product Control compares the GL journal lines with the booking-system extract for that product and month and identifies the missing, duplicated or misposted batch. The Data Office traces a data-quality failure back to the source-system record.
4. **Correct at source.** Fixes are made in the booking system or the source extract, never by editing gold tables. The next pipeline run picks up the correction; the break closes when the delta is back within tolerance.
5. **Confirm closure.** The owner attaches evidence (journal reference, corrected extract) to the register entry. The register entry is closed only after a clean pipeline run.

## Owners
- Reconciliation breaks: CIB Finance, Product Control analyst for the product.
- Data-quality failures: CIB Data Office, data steward for the source system.
- Commentary disclosure: CIB Business Management.

## Escalation
- Severity 1: Head of CIB Finance within two business days. A Severity 1 break still open after two further month-ends goes on the CFO office watch list.
- Severity 2: Product Control manager within five business days.
- Severity 3: handled in the weekly data-quality review.
- Any reconciliation or DQ break that cannot be explained within ten business days is escalated one level, whatever its size.

## Communication
- Management commentary must disclose every open reconciliation break over $250,000: product, month, amount, direction and expected resolution date.
- Figures affected by an open break are labelled in the dashboard and in agent answers ("Transaction Banking revenue for the month is understated until the correction is posted").
- Data-quality failures are summarised in commentary only when they change a reported figure.
- Never restate a prior month's reported revenue informally; restatements follow the month-end adjustment process.

## Data-quality rules in the pipeline
- Keys are not null (client, banker, product, deal, month).
- Foreign keys exist (client, banker, product and deal must be known).
- No duplicate natural keys: the latest record is kept, the others are quarantined.
- Fees and revenue are not negative.
- Deal probability is between 0 and 1.
- Close date is on or after open date.
