# Profitability review — 2026-09-15

The best next experiment is a frozen paper evaluation of `relative_strength_189`. It improved substantially on the current ensemble in the later evaluation and survived increased trading costs. It did not establish an advantage over passive investing. Do not increase allocation, add leverage, or promote it to real-money trading based on these results.

## What was tested

Seven predefined candidates were compared using the corrected simulator and archived Alpaca SIP daily data. Each of 23 symbols has 2,186 sessions from 2018 through 2026-09-14. The 2018–2019 history supplies indicator warm-up; accounts start separately for development (2020–2022), validation (2023–2024), and final evaluation (2025-01-02 through 2026-09-14). Returns below are cumulative simulated account returns, including idle cash, not annual returns or actual Alpaca returns.

The [plan](PROFITABILITY_PLAN.md) was saved before the new snapshot was evaluated. Selection used development and validation only and was written to [selection_before_final.json](var/profitability_2026-09-15/study/selection_before_final.json) before final results were calculated. The seven candidates are a bounded comparison, not an exhaustive search for an optimal strategy. These calendar periods may have appeared in earlier project research, so this is not a pristine independent holdout.

Signals execute at the next session open. Stops use the existing daily OHLC model. The base execution cost is 5 basis points per side (0.05%); the final segment was repeated at 20 basis points per side (0.20%). No leverage or position additions are allowed. The initial deployment budget is approximately 40%; actual exposure varies. The provisional account drawdown circuit is 15%, latched for the remainder of each period, and execution gaps can exceed it.

## Results

| Candidate | Development return | Validation return | Final return, 5 bps | Final return, 20 bps | Final max drawdown, 5 bps | Final fills, 5 bps |
|---|---:|---:|---:|---:|---:|---:|
| Current ensemble, 3% trail | 18.92% | 9.88% | 5.88% | -8.33% | -5.37% | 573 |
| Ensemble, 10% trail | 15.32% | 22.91% | 9.33% | 6.33% | -10.60% | 189 |
| Current ensemble without advisor | 21.98% | 10.50% | 5.80% | -3.62% | -5.33% | 529 |
| 63-session momentum | 1.08% | 38.13% | 7.01% | 5.23% | -9.10% | 108 |
| **189-session relative strength — frozen selection** | **12.42%** | **36.11%** | **11.09%** | **8.30%** | **-4.26%** | **162** |
| Monthly SPY trend | 4.84% | 17.93% | -3.40% | -3.77% | -10.81% | 7 |
| Monthly ETF selection | 4.80% | 10.81% | 9.74% | 9.23% | -4.73% | 24 |

“Fills” counts both entries and exits, not completed round trips. Relative strength's final CAGR was 6.39% at base costs and 4.81% at stressed costs. Its stressed final drawdown was 4.88%. The momentum candidate breached the development drawdown threshold (15.014%), triggered liquidation on 2022-09-07, and was ineligible under the frozen rule.

The selected strategy was worse than the current bot during development. Its later improvement is encouraging, not universal. Its final traded notional fell from the current bot's 57.82 times starting capital to 17.09 times; stop-triggered exits fell from 259 to 26. Average exposure was similar: 23.10% versus 24.29%. This supports reducing turnover as the most useful direction for this bot.

Wider stops alone improved later return but raised average exposure to 36.04% and almost doubled final drawdown. Removing the advisor gave no consistent benefit. The monthly SPY rule failed the final segment. ETF selection was comparatively inexpensive to trade, but changing to it because it won the final cost-stress column would reuse the final segment for selection.

## Passive alternatives remain competitive

| Final-period benchmark, 5 bps | Return | Max drawdown | Average exposure |
|---|---:|---:|---:|
| SPY, initially 40% invested | 12.67% | -7.69% | 42.86% |
| Same 15 stocks, equal weight, initially 40% invested | 17.65% | -9.68% | 43.99% |
| Same eight ETFs, equal weight, initially 40% invested | 13.78% | -4.35% | 44.14% |
| SPY, fully invested | 31.66% | -18.76% | 99.76% |

Relative strength trailed initially 40%-invested SPY by 1.57 percentage points and the stock basket by 6.56 points, while taking less average exposure and experiencing a smaller drawdown. These benchmarks match initial capital allocation, not realized exposure or risk. They have no account circuit; fully invested SPY lost as much as 33.79% from its peak in development. The ETF selection also lagged its passive basket in final return and drawdown. A more complex bot is not automatically a better investment.

## Best actions now

1. **Freeze relative strength as the next paper candidate.** Preserve the exact settings below and compare future account-level returns with the current bot and passive references over the same dates. Use at least 60 completed market sessions as an initial operational review, continuing if too few positions have closed. That duration is a review checkpoint, not a statistical proof threshold. Record actual spreads, fills, fees, rejected orders, idle cash, and drawdowns. Do not retune after each loss.
2. **Keep the existing capital budget.** Four entries at 10% each do not target 92% investment, even though the cash reserve is 8%. Raising deployment could raise both return and loss; it would not demonstrate better signals. Keep the provisional 15% account circuit until the user's preferred risk budget is established.
3. **Measure execution differences before enabling the candidate.** Research uses SIP/all-adjusted bars and next-open fills. The existing operator defaults to IEX/split-adjusted bars, uses fresh entry quotes, and normally waits ten minutes after the open. Compare signals and realized paper execution against the frozen model before interpreting forward results as a replication. Existing healthy holdings outside the top four remain held; this candidate ranks new entries rather than continually rotating into exactly four leaders.
4. **Keep the simple alternatives in the evaluation.** The results do not justify deploying the monthly SPY prototype or replacing the selected strategy with the final-period ETF winner. If the frozen candidate fails to add value after costs and risk in forward evaluation, simplify the project around a passive benchmark instead of expanding the parameter search or adding an unvalidated AI layer.

The earlier recovered paper report had 13 closed trades and gross realized P/L of -$557.61, including a one-share smoke test. Original strategy attribution is unavailable. That small sample cannot establish that the current strategy or its trailing stop caused those losses.

## Frozen candidate and safe preview

Use the current 15-stock universe. Strategy settings: momentum window 189, skip most recent 5 sessions, trend window 100, volatility window 20, top four new-entry candidates, minimum breadth 60%, and positive median universe momentum. Retain the heuristic advisor and minimum confidence 0.68; confidence is an uncalibrated routing score. Risk settings: 10% fixed and trailing stops, 15% account circuit, four positions, two daily buys, 10% per entry, 25% position cap, 8% cash reserve, no additions, and three-calendar-day exit cooldown.

This existing CLI command previews the candidate without submitting orders. It records decisions in a separate preview folder. Its prices follow the operator's IEX/split-adjusted path, so it is an operational preview rather than an exact reproduction of the study:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.cli alpaca-once --symbols-file universe_popular.txt --profile paper-operator --strategy relative-strength --short-window 20 --long-window 100 --min-confidence 0.68 --trailing-stop-pct 0.10 --days 600 --journal-dir var\relative_strength_preview
```

No candidate was enabled for automatic submission during this review. Existing submitting profiles were preserved, and no orders or continuous operator were started. See [the operating guide](OPERATING_GUIDE.md) for account checks and controls before any paper deployment.

## Engineering completed and reproducibility

Historical provider requests now explicitly default to split adjustment, preventing raw splits from becoming artificial momentum changes. Alpaca documents that omitting `adjustment` defaults to raw; the archived experiment explicitly uses `all`, which also includes dividend and spin-off adjustments. [Alpaca historical bars reference](https://docs.alpaca.markets/us/reference/stockbars).

The live account drawdown circuit now persists its halt across recovery, restart, and threshold changes. Buy reservations are blocked after a breach and remaining positions continue receiving liquidation instructions. A manual account-state review is required before clearing it; do not reset the journal merely to bypass a loss limit. This does not guarantee execution prices or cancel already accepted broker orders by itself.

Added monthly research prototypes, the offline comparison runner, and regression tests for future-data isolation, selection isolation, benchmark costs, provider adjustments, and persistent drawdown behavior. **177 tests passed**, dependency checks passed, and the diff whitespace check passed on Windows/Python 3.11.

Reproduce from the local frozen data:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.profitability_study --csv var\profitability_2026-09-15\adjusted_daily.csv --output var\profitability_2026-09-15\reproduction --max-drawdown .15
```

[Source metadata](var/profitability_2026-09-15/source.json), [full results/configurations/code hashes](var/profitability_2026-09-15/study/results.json), and [CSV results](var/profitability_2026-09-15/study/results.csv) are saved locally under ignored `var`; keep that folder to retain the data. Data SHA-256: `959b182a8e0d6d2eb3cebdb8187b48079584e463f41594af81ff6253b71b19cc`.

Limitations include the present-day stock universe's selection/survivorship bias, hindsight in ETF choices, adjusted OHLC as a total-return approximation rather than literal historical execution prices, daily rather than intraday account drawdowns, zero interest on idle cash, and no exact taxes, market-impact or brokerage-cash-flow model. Changing these assumptions requires a labeled new experiment, not a silent revision of this result.

Published [time-series momentum research](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum) and [Faber's timing-model resources](https://mebfaber.com/timing-model/) motivated simple trend comparisons. Their evidence does not validate this stock universe or implementation; the research-only monthly rules are not exact replications of those papers.
