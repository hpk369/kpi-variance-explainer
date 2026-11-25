# Evaluation scorecard · 2026-11-25 15:58

Northlake Bank (fictional), synthetic data. Planner `qwen35-122b-a10b`, writer `gpt-oss-120b`, judge `qwen35-122b-a10b`, 15 questions, average 14.3255s per answer.

| Metric | Target | Result |
|---|---|---|
| Number accuracy | 100% | 100% ✅ |
| Number accuracy, stories 1–3 (Gate 7) | 100% | 100% ✅ |
| Tool selection | ≥ 95% | 100% ✅ |
| Groundedness (LLM judge) | ≥ 90% | 86% ❌ |
| Retrieval relevance (LLM judge) | ≥ 85% | 100% ✅ |
| Guardrails | 2/2 | 2/2 ✅ |
| Expected documents retrieved | – | 100% |
| Key names mentioned | – | 100% |

Judge errors: 0 · agent errors: 0 · blocked answers: 0

## Rows

| id | outcome | numbers | tools | grounded | relevance | notes |
|---|---|---|---|---|---|---|
| s1_why_miss | answered | 100% | ✓ | ✓ | 100% |  |
| s1_slipped | answered | 100% | ✓ | ✓ | 100% |  |
| s1_ma_share | answered | 100% | ✓ | ✓ | 100% |  |
| s2_whitespace | answered | 100% | ✓ | ✓ | 100% |  |
| s2_lending_only | answered | 100% | ✓ | ✗ | 100% | Evidence shows Driftwood Life Sciences holds the highest allocated capital ($5.5M) rather than Eastgate Fabrication ($5.2M), and the minimum revenue is approximately $0.65M rather than $0.8M. Unsupported: Largest average allocated capital: $5.2 M – Eastgate Fabrication; The portfolio’s revenue spans |
| s2_avg_raroc | answered | 100% | ✓ | ✓ | 100% |  |
| s3_over_capacity | answered | 100% | ✓ | ✓ | 100% |  |
| s3_win_trend | answered | 100% | ✓ | ✓ | 100% |  |
| s3_team_size | answered | 100% | ✓ | ✓ | 100% |  |
| s4_recon | answered | 100% | ✓ | ✓ | 100% |  |
| d_raroc | answered | – | – | ✓ | 100% |  |
| d_weighted_pipeline | answered | 100% | – | ✓ | 100% |  |
| d_slipped | answered | – | – | ✗ | 100% | The evidence defines the timeframe as a generic 'period', but the answer specifies 'quarter' in the executive paragraph without support. Unsupported: closing after the quarter ends |
| g_real_bank | refused | – | ✓ | – | – |  |
| g_future | answered | – | – | ✓ | – |  |

## What the two groundedness failures were, and the fix

This run (15:58) includes the fix for the 13:14 run's two misses (84 days "after the quarter"; a team at target called "below"): both rows now pass. Two different rows failed, again with right numbers in wrong sentences:
- **s2_lending_only:** the question asked "how many"; the answer also named a "largest allocated capital" client on its own and picked the wrong one (Eastgate Fabrication $5.2M; Driftwood Life Sciences has $5.5M).
- **d_slipped:** the glossary defines a slipped deal by "period"; the answer narrowed it to "quarter".

Fix (after this run): Python now computes the lowest and highest RAROC, revenue and capital with their client names, and the writer may only use those (rule 18: answer what was asked, never pick an extreme itself); definitions keep the glossary's own words (rule 12). The number check proves each figure came from a tool, not that the sentence around it is right; the groundedness judge is what catches that.
