# Small-capital feasibility review — 2026-10-22

The frozen strategy behaves differently at the user's intended account size. At €500, the simulated bot never bought a share. At €1,000, it bought only NFLX and WMT. Larger accounts made more of the stock universe accessible, but this reused historical sample does not establish profitable real-money trading or justify allocating the full €5,000 limit.

## Scope and provenance

Only the previously selected `relative_strength_189` and the legacy ensemble were rerun. Strategy, universe, advisor, stops, 15% account circuit, 10% entry cap, four-position limit, two daily buys, no additions and three-day cooldown were unchanged. Starting capital and the prescribed 5/20 basis-point execution-cost scenarios were the only changes. No new strategy was selected or tuned.

The existing archived Alpaca SIP/all-adjusted dataset passed its source hash check. The original saved configurations and strategy parameters matched the current simulation inputs exactly. The evaluated account period is **2025-01-02 through 2026-09-14**, with earlier bars providing indicator warm-up. This final segment had already been examined in the [earlier review](PROFITABILITY_REVIEW.md); it is reused evidence, not a new holdout.

The sizing illustration uses the parent review's 2026-10-21 ECB reference of **1.1234 USD per EUR**: €500/€1,000/€2,500/€5,000 become $561.70/$1,123.40/$2,808.50/$5,617.00. This constant conversion denominates account size. Returns below remain simulated USD account returns; the euro equivalents are reference conversions, with no historical currency return or conversion-fee model. [ECB reference feed](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml).

## Frozen relative-strength results

| Starting allocation | Return, 5 bps | Return, 20 bps | Max drawdown, 5 bps | Average invested, 5 bps | BUY fills | Symbols bought |
|---|---:|---:|---:|---:|---:|---:|
| €500 | 0.00% | 0.00% | 0.00% | 0.00% | 0 | 0 |
| €1,000 | 1.85% | 1.66% | -1.80% | 4.33% | 7 | 2 |
| €2,500 | 9.91% | 8.83% | -2.72% | 14.80% | 43 | 9 |
| €5,000 | 8.48% | 6.43% | -3.22% | 19.19% | 76 | 12 |

Returns are cumulative over approximately 20 months and include idle cash. BUY fills count entries, rather than completed round trips. The €1,000 run bought NFLX and WMT and remained entirely in cash on 69.18% of sampled sessions. The €2,500 and €5,000 runs remained entirely in cash on 40.47% and 38.59% of sessions. None triggered the historical account circuit.

At the fixed reference conversion, the €5,000 run ended approximately €424.02 above starting equity at base costs and €321.41 at stressed costs. Its largest sampled absolute decline from a prior account peak was approximately €180.01/€207.02. These historical observations cannot bound a future loss or guarantee the user's €1,000 loss limit. Price gaps, delayed exits, outages and execution differences remain possible.

The €2,500 run's higher percentage return is a consequence of this particular sequence of whole-share sizing and fills. Selecting account size because it had the best reused-period return would add another historical selection decision. It is not evidence that €2,500 is an optimal allocation.

## Legacy and passive references

| Starting allocation | Legacy return, 5 bps | Legacy return, 20 bps | Whole-share SPY, 40% target | Whole-share stock basket, 40% target |
|---|---:|---:|---:|---:|
| €500 | 0.00% | 0.00% | 0.00% | 0.00% |
| €1,000 | 0.48% | -0.11% | 0.00% | 0.00% |
| €2,500 | 4.86% | -1.26% | 6.52% | 0.00% |
| €5,000 | 6.65% | -1.59% | 9.77% | 8.09% |

The passive references buy at the second evaluation session's open, floor each allocation to whole shares and retain rounding cash. The stock basket allocates its 40% target equally across the same 15 stocks. When an allocated slice cannot buy one share, that slice remains cash. Therefore some passive rows do nothing; their exposures differ materially from both each other and the active bot.

At €5,000, the whole-share SPY reference returned 9.77% with a -5.96% sampled drawdown. The stock-basket reference returned 8.09% with a -2.58% drawdown, buying only AMD, NFLX, NVDA and WMT at entry. The references have no account circuit or active exits. These results provide no clear general advantage for the more complex bot.

## Affordability and the next useful check

The unchanged 10% entry cap allows at most $56.17/$112.34/$280.85/$561.70 per initial entry at the four account sizes, before the IOC price buffer and other constraints. At the archive's last close on **2026-09-14**, the following numbers of universe stocks fit those caps using one share and the configured 5 bps price buffer:

| Allocation | Stocks affordable at that archived close | Symbols |
|---|---:|---|
| €500 | 0 | None |
| €1,000 | 2 | NFLX, WMT |
| €2,500 | 4 | NVDA, AMZN, NFLX, WMT |
| €5,000 | 12 | AAPL, MSFT, NVDA, AMZN, GOOGL, TSLA, AVGO, AMD, NFLX, JPM, V, WMT |

These are archived prices and simple affordability checks. They are not current quotes or strategy BUY signals. Leadership, breadth, confidence, daily limits, cash, slot availability and execution checks can reject otherwise affordable stocks.

The next useful operational experiment is a separate paper account or paper replay sized to the allocation the user would actually fund, with the same frozen rules. Record how often intended entries are rejected because one share exceeds the entry cap. A €500 account is unsuitable for testing this exact whole-share stock strategy under the demonstrated prices and caps; a €1,000 account tests a much narrower strategy than the old $100,000 paper account. A larger trial may improve affordability but still needs a fresh broker quote check and evidence that the additional complexity is worthwhile. The study does not support raising position limits merely to force more trades.

## Important limits and reproduction

All-adjusted historical prices include later split and dividend adjustments. Flooring share quantities against them is an approximation: before a split, the literal share price may have been higher and a historical whole-share entry may not have been affordable. This especially limits precise small-account claims involving NFLX or other split histories. The present-day universe also introduces survivorship and selection bias.

The simulator uses next-open entries and daily-bar stops. The operator uses fresh quotes, delayed regular-hours entries, IOC limits and broker stops. The study omits minimum brokerage fees, exact spreads, tax, market impact, operational failures, corporate-action cash flows and currency-conversion charges. Live absolute daily/total euro loss controls are not part of this historical 15%-drawdown simulation.

Artifacts are stored locally in ignored [var/smallcapital_2026-10-22](var/smallcapital_2026-10-22/results.json): complete results JSON/CSV, each active run's fills and account snapshots, the bounded runner, frozen configuration/source metadata and simulator hashes. No credentials or broker clients were used. Source files stayed unchanged during the run. Data SHA-256: `959b182a8e0d6d2eb3cebdb8187b48079584e463f41594af81ff6253b71b19cc`.

Reproduce with:

```powershell
.\.venv\Scripts\python.exe var\smallcapital_2026-10-22\run_small_capital.py
```
