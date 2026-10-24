# Profitability comparison plan — 2026-09-15

This plan is recorded before retrieving and evaluating the new historical snapshot. It is an exploratory comparison, not a claim that the calendar periods have never been examined in earlier project research.

## Question

Does changing the current legacy operator produce a useful improvement after transaction costs without an unacceptable drawdown, and is a simpler ETF approach preferable?

## Fixed comparisons

- Existing stock ensemble, 5/20 sessions, confidence 0.65, 10% fixed stop / 3% trailing stop.
- Same entries with 10% / 10% stops, isolating the narrow trailing-stop hypothesis.
- Same legacy strategy without the heuristic advisor, isolating that layer.
- Slower 63-session stock momentum with a fixed 4% threshold and 10% / 10% stops.
- Existing 189/100 relative-strength candidate with 10% / 10% stops.
- A research-only SPY 200-session trend filter reviewed monthly.
- A research-only monthly ETF trend/momentum rotation over SPY, QQQ, IWM, EFA, EEM, IEF, TLT, GLD; choose up to three positive-trend leaders using six-month momentum excluding the most recent month.

Use the current stock universe only as a diagnostic because it was chosen with hindsight. Compare stock strategies to both the same-universe equal-weight benchmark and SPY. Compare ETF approaches to SPY and a matching-universe passive benchmark. Show both full-investment and capital-matched benchmarks so cash exposure cannot be mistaken for investment skill.

## Data and execution

Request daily Alpaca data from 2018-01-01 through the completed 2026-09-14 session. Prefer consolidated SIP history if the existing account permits access; otherwise disclose use of IEX. Explicitly request all corporate-action adjustments and save the resulting candles, source metadata, and hash. Treat adjusted prices as a research approximation, not literal historical execution prices. Account-level dividends/fees are not reconstructed by this simulator.

Use 2018-2019 as warm-up, 2020-2022 as development, 2023-2024 as validation, and 2025-01-01 onward as final chronological evaluation. Choose a candidate using development/validation only before revealing the final segment. Annual slices are additional diagnostics, not an independent untouched test.

Use next-open strategy executions, the existing OHLC stop model, and costs of 5 basis points per side. Repeat at 20 basis points per side as a stress case. Keep the existing no-leverage, cash-reserve and no-position-add rules. Main sizing uses approximately 40% initial deployment: four stock positions at 10% each, one SPY position at 40%, or three ETF positions at approximately 13.33% each. Report actual exposure and turnover, since drift and rotation cause these to differ.

The provisional account drawdown circuit threshold is 15%, pending user preference. A triggered circuit remains halted for the rest of each evaluation period; folds start new simulated accounts. The stop is not a guaranteed loss bound. Do not raise leverage or silently increase exposure to manufacture a return improvement.

## Decision rule

Prioritize positive validation performance after costs, a drawdown consistent with the risk budget, improvement against the legacy operator, and comparison with capital-matched passive alternatives. Require the selected candidate to remain viable in the final segment and cost stress. If none qualifies, retain the results as research and recommend further forward observation or a simpler passive baseline instead of promoting a winner automatically.

Do not change submitting profiles or place orders during this study. Research-only candidate code can be added and tested. The final report must state what the experiment establishes, what it does not, and the next actionable step.
