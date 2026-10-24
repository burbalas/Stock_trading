# AI Stock Trading Bot

A stock-strategy research tool and automated Alpaca paper-trading operator, with a separate disabled-by-default live pilot. Local simulations require no credentials; connected paper trading requires paper credentials and `--submit`. Paper commands reject real-money endpoints. The live pilot requires separate credentials, a pinned account and explicit budget configuration; it has not been exercised with real money.

**October 22:** the [corrected profitability evidence review](PROFITABILITY_EVIDENCE_REVIEW_2026-10-22.md) found USD losses at both intended sizes in the fixed 27-session diagnostic. The prepared €2,500 live ceiling stays disabled. See [live pilot preparation](LIVE_PILOT_GUIDE.md) and the [engineering review](REVIEW_2026-10-22.md). Earlier [small-capital results](SMALL_CAPITAL_REVIEW.md) used adjusted-price approximations and do not validate literal whole-share execution.

For prospective profitability tracking, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\Update-ProfitabilityEvidence.ps1` weekly from this folder. It uses read-only market-data requests, freezes the existing strategy at the two intended sizes and preserves prior results. The separate prospective replay begins October 23; it does not place orders or certify real broker execution. Add `-StatusOnly` for a local-only check.

**Open the app:** double-click **Stock Trading (Paper)** on the desktop or [Open-StockTrading.cmd](Open-StockTrading.cmd). The [app guide](APP_GUIDE.md) explains its background operation, start/stop controls, logs, reports and optional sign-in resume. The [October 21 review](REVIEW_2026-10-21.md) records the first verified candidate paper entries and remaining real-money work.

The [September 22 readiness review](LIVE_READINESS.md) is retained as historical context; use the October 21 review for current operation.

**No strategy in this repository has established future profitability.** The saved research CSVs are exploratory results, and the latest v6 candidate underperformed its benchmark. The advisor is a deterministic heuristic, not a trained predictive AI model. See [the audit and remaining limitations](AUDIT.md) and [the current setup and operating guide](OPERATING_GUIDE.md).

The [2026-09-15 profitability review](PROFITABILITY_REVIEW.md) compares seven fixed candidates on archived, adjusted historical data. Slower relative strength improved on the current operator and survived higher costs, but did not beat the initially capital-matched passive benchmarks in the final period. It is the next candidate for forward paper evaluation; submitting profiles remain unchanged.

For normal paper operation use the desktop shortcut. The alternative [Start-PaperCandidate.ps1](Start-PaperCandidate.ps1) launcher is described in the [operating guide](OPERATING_GUIDE.md#advanced-a-candidate-session-from-a-terminal). It previews by default; `-Submit` runs the frozen candidate until market close using Alpaca paper funds.

## What it does

- Runs a paper-trading loop over historical/demo candles.
- Uses a pluggable strategy interface.
- Includes a moving-average strategy as the default.
- Includes momentum, mean-reversion, and ensemble strategies.
- Includes a simple AI-style advisor interface that can later call an LLM or model API.
- Keeps strategies signal-only. Only the portfolio/risk layer can convert signals into orders.
- Ranks many symbols together before creating orders.
- Applies portfolio limits before every order.
- Models slippage and optional commissions in paper backtests.
- Writes live decisions and submitted paper fills to `var/`.
- Blocks duplicate same-symbol/same-side/same-day submissions.
- Blocks adding to already-held positions by default.
- Tracks per-position highs and can exit with a trailing stop.
- Stops opening new buys if the account drawdown stop is active.
- Emits trade, position, and equity summaries.
- Supports CSV candle data or generated demo data.
- Supports Alpaca historical stock data and paper order submission.

## Quick Start

On Windows, create a local environment and install dependencies first (`tzdata` is required for exchange timezones):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests
python -m trading_bot.cli readiness-report
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place of `python` in the commands below.

```powershell
python -m trading_bot.cli demo
```

Run with generated demo data for specific symbols:

```powershell
python -m trading_bot.cli run --symbols AAPL MSFT NVDA --cash 10000 --days 180
```

Run a larger universe with portfolio constraints:

```powershell
python -m trading_bot.cli run --symbols AAPL MSFT NVDA TSLA AMZN GOOGL META --strategy ensemble --max-open-positions 4 --max-daily-orders 3
```

Run from a watchlist file:

```powershell
python -m trading_bot.cli run --symbols-file universe.txt --strategy ensemble
```

Use a researched preset:

```powershell
python -m trading_bot.cli run --symbols-file universe_popular.txt --profile aggressive-research
```

Run against a CSV file:

```powershell
python -m trading_bot.cli run --csv data\sample_prices.csv --symbols AAPL --cash 10000
```

Run a backtest using Alpaca historical data, while still using the local paper broker:

```powershell
python -m trading_bot.cli run --data alpaca --symbols AAPL MSFT --days 180
```

Evaluate the latest Alpaca signal without submitting an order:

```powershell
python -m trading_bot.cli alpaca-once --symbols AAPL MSFT
```

Submit generated orders to Alpaca paper trading:

```powershell
python -m trading_bot.cli alpaca-once --symbols AAPL MSFT --submit
```

Run repeated Alpaca paper checks:

```powershell
python -m trading_bot.cli alpaca-loop --symbols-file universe.txt --cycles 4 --interval-minutes 15
```

Run the operator loop with frequent risk checks and completed-daily-bar signal checks:

```powershell
python -m trading_bot.cli alpaca-session --symbols-file universe_popular.txt --profile paper-operator --submit --risk-interval-minutes 5 --signal-interval-minutes 390 --cycles 12
```

For normal paper operation after the market opens, run until the close instead of counting cycles:

```powershell
python -m trading_bot.cli alpaca-session --symbols-file universe_popular.txt --profile paper-operator --sell-only --submit --risk-interval-minutes 5 --signal-interval-minutes 390 --until-close
```

You can start the session before the market opens and let it wait until the open buffer is over:

```powershell
python -m trading_bot.cli alpaca-session --symbols-file universe_popular.txt --profile paper-operator --submit --risk-interval-minutes 5 --signal-interval-minutes 390 --until-close --wait-for-open
```

`alpaca-session` checks stop loss, trailing stop, account drawdown, reconciliation, and position highs on the risk interval. Strategy signals use completed daily bars, so one signal pass per market session is normally enough; the five-minute risk loop continues to use current broker prices. When submitting, it also creates or raises broker-side protective stops every 15 minutes by default. Transient API failures are retried five times before the session exits. `--until-close --wait-for-open` waits for the first open and then stops at that session's close. Use `--continuous` to wait through nights, weekends, and exchange holidays and resume each session.

Runtime controls: `var/heartbeat.json` records operator status; create `var/STOP_BUYS` to pause entries while retaining exits, or `var/STOP` to stop the session. One submitting process can use a journal directory at a time. Use one canonical journal per paper account.

Record experimental Strategy V2 signals beside the sell-only operator without giving the shadow strategy order authority:

```powershell
python -m trading_bot.cli alpaca-session --symbols-file universe_popular.txt --profile paper-operator --sell-only --shadow-strategy relative-strength --shadow-journal-dir var\shadow --submit --risk-interval-minutes 5 --signal-interval-minutes 390 --until-close --wait-for-open
```

The shadow strategy ranks the universe by volatility-adjusted long-horizon momentum and only buys in a healthy breadth regime. Its decisions are written separately under `var\shadow`; `submit` is forcibly disabled for the shadow pass.

Create broker-side protective stop orders for current paper positions:

```powershell
python -m trading_bot.cli alpaca-protect --submit
```

`alpaca-protect` uses the same active stop levels shown by `status`, submits GTC sell stop orders in Alpaca paper, and skips symbols that already have an open sell order.

Run a tiny execution reliability smoke test in Alpaca paper. This is not a strategy test; it submits a controlled 1-share buy, creates a temporary protective stop, cancels that stop, sells the share, and verifies no test position remains. It refuses symbols you already hold and uses a separate journal folder by default:

```powershell
python -m trading_bot.cli alpaca-exec-smoke --symbol MSFT
python -m trading_bot.cli alpaca-exec-smoke --symbol MSFT --submit
```

Check local setup without placing orders:

```powershell
python -m trading_bot.cli doctor
```

Check the paper account, positions, open orders, and bot state:

```powershell
python -m trading_bot.cli status --symbols-file universe_popular.txt
```

`status` also reconciles the bot's submitted-fill log against Alpaca's current positions. If reconciliation fails, submit commands are blocked until you inspect the paper account state.

The status output includes a position risk section showing current price, tracked high, active stop price, and distance to stop for each held symbol.

Write an end-of-day report. If you run the audit on a weekend or the next day, pass the trading date you are reporting:

```powershell
python -m trading_bot.cli daily-report --report-date 2026-07-24
```

Write a strategy performance report from the paper fills journal plus current open Alpaca positions:

```powershell
python -m trading_bot.cli strategy-report
```

This report separates closed trades from open marked-to-market trades and shows realized P/L, open P/L, win rate, average win/loss, holding time, and trade returns compared with a benchmark such as `SPY`. Use it to decide strategy changes from evidence instead of a few recent screenshots.

Run the whole end-of-day workflow in one command:

```powershell
python -m trading_bot.cli close-audit --submit
```

`close-audit` syncs filled protective stops, creates or raises broker-side protection, writes a health report, writes the daily report, writes the strategy report, and runs the test suite. Use `--report-date YYYY-MM-DD` when auditing a previous market day.

Include a dry signal scan in the same status check:

```powershell
python -m trading_bot.cli status --symbols-file universe_popular.txt --signals
```

Run a dry Alpaca check with the current researched preset:

```powershell
python -m trading_bot.cli alpaca-once --symbols-file universe_popular.txt --profile aggressive-research
```

Run the more usable paper-operator profile, which allows up to two daily orders and uses tighter drawdown limits:

```powershell
python -m trading_bot.cli alpaca-once --symbols-file universe_popular.txt --profile paper-operator
```

The legacy paper-operator currently retains `--stop-loss-pct 0.10` and `--trailing-stop-pct 0.03` for existing positions. Corrected next-open and daily high/low research found the 3% trail too turnover-heavy for Strategy V2, so V2 remains shadow-only and is not promoted for order submission.

The daily order cap is tracked in `var/bot_state.json`, so repeated runs on the same day will not keep adding more buy orders after the cap is reached. Sell orders and stop-loss exits remain available.

By default, the bot also refuses to buy more of a symbol you already hold. This keeps paper tests from accidentally averaging into existing positions. Use `--allow-position-adds` only when you intentionally want to test scaling into positions.

After any exit, the portfolio layer blocks a fresh buy in the same symbol for three calendar days by default. Override this only for research with `--buy-cooldown-days`.

Paper runs track each held symbol's highest seen price in `var/bot_state.json`. If a position later falls more than `--trailing-stop-pct` from that high, the portfolio layer can create a sell order. The default trailing stop is `0.05`, or 5%.

Submit paper orders only while the market is open:

```powershell
python -m trading_bot.cli alpaca-once --symbols-file universe_popular.txt --profile paper-operator --submit
```

By default, submit commands refuse to queue orders while the market is closed. They also refuse fresh buy submissions during the first 10 minutes after market open and during the last 15 minutes before market close. Use `--sell-only` near the open or close to keep exits active without adding new positions. Use `--allow-closed-submit`, `--allow-near-open`, or `--allow-near-close` only if you deliberately want that behavior in paper trading.

You can override profile limits directly:

```powershell
python -m trading_bot.cli alpaca-once --symbols-file universe_popular.txt --profile paper-operator --max-daily-orders 3
```

After the daily buy budget is used, monitor with sell-only submission so exits and stop losses can still happen but new buys are blocked:

```powershell
python -m trading_bot.cli alpaca-loop --symbols-file universe_popular.txt --profile paper-operator --sell-only --submit --cycles 4 --interval-minutes 30
```

For faster exit monitoring without new entries:

```powershell
python -m trading_bot.cli alpaca-session --symbols-file universe_popular.txt --profile paper-operator --sell-only --submit --risk-interval-minutes 5 --signal-interval-minutes 390 --until-close
```

Run research with walk-forward validation:

```powershell
python -m trading_bot.cli research --data alpaca --symbols-file universe_popular.txt --profile aggressive-research --walk-forward --csv-out research_walkforward_results.csv
```

Run a broader strategy/parameter search:

```powershell
python -m trading_bot.cli research --data alpaca --symbols-file universe_popular.txt --profile paper-operator --days 1095 --expanded --walk-forward --folds 6 --csv-out research_expanded.csv --walk-forward-csv-out research_expanded_walkforward.csv
```

Retest only named finalists without rerunning the full grid:

```powershell
python -m trading_bot.cli research --data alpaca --symbols-file universe_popular.txt --expanded --strategy-filter relative-strength-189-100-b60,ensemble-20-50-c65 --walk-forward
```

Backtests use warm-up history without trading it, execute strategy orders at the next session open, and model fixed/trailing stops from daily opens and lows. A drawdown detected at the close queues liquidation for the next available open. Market gaps can exceed the configured drawdown threshold. Research selection uses training scores; chronological test results remain evaluation data. CSV exports now include a manifest describing source data, parameters, limitations, and output hashes.

Compare stop-loss and trailing-stop settings while keeping the entry strategy fixed:

```powershell
python -m trading_bot.cli risk-research --data alpaca --symbols-file universe_popular.txt --profile paper-operator --days 1095 --csv-out risk_research_results.csv
```

Decide whether risk research is strong enough to change live stop settings:

```powershell
python -m trading_bot.cli risk-promotion-report --risk-csv risk_research_results.csv
```

Decide whether research evidence is strong enough to change the live strategy:

```powershell
python -m trading_bot.cli promotion-report --research-csv research_expanded.csv --walk-forward-csv research_expanded_walkforward.csv
```

The promotion report screens exploratory paper candidates using training selection, positive test-period benchmark edge, walk-forward thresholds, drawdown, and score improvement. It does not authorize real-money deployment. `readiness-report` checks matching research provenance and performance, and cannot label synthetic or legacy unverified results as proof of profitability.

Expected CSV columns:

```text
date,symbol,open,high,low,close,volume
```

## Project Layout

```text
trading_bot/
  advisor.py      AI advisor interface and default heuristic advisor
  broker.py       Paper broker and order execution
  cli.py          Command line entry point
  config.py       Bot configuration
  data.py         CSV and demo market data providers
  engine.py       Trading/backtest engine
  journal.py      CSV logs and duplicate-order state
  live.py         Alpaca latest-signal runner
  models.py       Shared dataclasses and enums
  portfolio.py    Portfolio-level signal ranking and order planning
  risk.py         Risk manager
  strategies.py   Signal-only strategies
tests/
  test_bot.py     Smoke tests for the main components
```

## Alpaca Setup

Install the official Alpaca SDK:

```powershell
pip install -r requirements.txt
```

Set your paper trading credentials as environment variables:

```powershell
$env:ALPACA_API_KEY="your_key_id"
$env:ALPACA_SECRET_KEY="your_secret_key"
$env:ALPACA_PAPER="true"
$env:ALPACA_BASE_URL="https://paper-api.alpaca.markets/v2"
```

Or create a local `.env` file using `.env.example` as the template. `.env` and `.env.local` are ignored by Git.

Keep credentials out of source control. If a key is pasted into a chat, commit, screenshot, or issue, rotate it in Alpaca before trading.

## Extension Points

- Add a live data provider by implementing `MarketDataProvider`.
- Add a broker integration by implementing the same methods as `PaperBroker`.
- Add a new strategy by subclassing `Strategy`.
- Replace `HeuristicAdvisor` with an API-backed advisor.
- Keep new strategies signal-only. They should return `Signal`, never submit orders.
- Keep order sizing, cash management, position limits, stop losses, and Alpaca submission in `portfolio.py`, `risk.py`, `live.py`, and broker adapters.

The supplied broker adapter only supports Alpaca paper trading. Order acknowledgments are not fills: journals track confirmed cumulative executions, including partial fills, and reconcile them after restart. Whole-share long positions are supported for automated order planning; fractional or short holdings require resolution before trading resumes.
