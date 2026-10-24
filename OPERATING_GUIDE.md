# Set up and operate the paper bot

**Current status (2026-10-21):** use the **Stock Trading (Paper)** desktop shortcut or [Open-StockTrading.cmd](Open-StockTrading.cmd). The app now manages a background paper worker and has verified entries with broker-confirmed stops. See [APP_GUIDE.md](APP_GUIDE.md) and [the latest review](REVIEW_2026-10-21.md). Keep the computer awake; closing the desktop window leaves the manager running. Windows sign-in resume is optional and initially off.

The `status` command includes operator heartbeat age and stop flags. Successful session checks record account equity in the local runtime's `account_observations.csv`; previews and submitting sessions are marked separately. These are observations, not audited returns after deposits, dividends, fees and withdrawals. Existing `var` CLI paths redirect to the canonical runtime outside OneDrive. Direct filesystem writes to the old `var` folder do not follow this redirect: use the app or the control commands below.

The project can research strategies and automatically submit Alpaca paper orders. No strategy has passed a complete independent profitability validation. The desktop app runs the frozen relative-strength candidate. The `paper-operator` CLI profile is a different experimental legacy preset; use the desktop app or candidate launcher for the current candidate.

Read the [profitability review](PROFITABILITY_REVIEW.md) for the candidate's historical comparison and limitations. Research uses archived SIP/all-adjusted bars, while normal operator requests explicitly use split-adjusted history. The desktop app provides the current Start control.

An account drawdown breach now latches in `bot_state.json`: a rebound or restart will not resume buying, and risk cycles continue requesting liquidation. Review the account, outstanding orders, and recorded trigger before any deliberate state reset. Do not clear this halt merely to bypass the drawdown limit.

## Advanced: a candidate session from a terminal

The desktop app is the normal entry point. Use this alternative only after stopping the managed worker; both launch paths use the same journal and submitting lock. The dated preview and storage notes below are historical evidence, not the current account state.

The account and candidate preview passed on 2026-09-15 at approximately 15:37 EEST. Paper cash/equity was $99,440.16, with no positions or open orders and matching reconciliation. The candidate's completed-session signals were risk-off, so remaining in cash was expected. All 15 preview signal directions matched the frozen SIP/all-adjusted model for that session. This preview did not exercise fresh buy quotes or order execution.

From the project folder, start one session using the [candidate launcher](Start-PaperCandidate.ps1):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-PaperCandidate.ps1 -Submit
```

This submits **simulated Alpaca paper orders** when signals and risk checks permit. It uses the existing account journal, checks risk every five minutes, evaluates entries after the ten-minute opening buffer, and exits at market close. Keep this terminal open and the computer awake. Exchange hours can change with holidays and daylight-saving dates; the operator uses the broker's market clock. There may be no trades when the strategy stays risk-off. Add `-Continuous` to this terminal command to wait across sessions. The desktop app starts a supervised continuous worker separately.

Omit `-Submit` for a preview only. Add `-ShowCommand` to inspect the generated command without connecting. Parameters are explicit rather than inherited from the legacy submitting preset. The launcher keeps the operator's IEX/split-adjusted signal path, whose differences from the historical study remain documented in the profitability review.

Every launcher run now prints its console-log path under `var/logs/candidate-*.log` and records Python standard output and errors there. If a command fails, the final message points to that log, which contains the underlying Python error. Successful preview output and an isolated startup-failure traceback were both verified through Windows PowerShell; recording output does not change strategy parameters or order authority.

### Windows state-save error repaired

The 2026-09-15 session stopped at 17:21 EEST after Windows denied replacement of `var/bot_state.json` (`WinError 5`). The project is inside OneDrive; the exact process holding or denying access was not identified. State, confirmed-fill CSV, and heartbeat replacements now retry Windows access/sharing errors for at most 5.55 seconds, keeping the old file intact until replacement succeeds. They do not repeat a trade or erase the destination. Persistent failures still stop the operation, and a secondary heartbeat/cleanup failure no longer hides the original error.

At that September review, all 187 tests passed, including an actual Windows reader-lock test and reservation/fill deduplication during retries. A connected risk preview successfully saved the existing journal after the fix. At approximately 18:40 EEST that day, the account still had $99,440.16 cash/equity, no positions, no open orders, and matching reconciliation. The October desktop update additionally moved authoritative runtime storage outside OneDrive; no account reset or elevated administrator terminal is needed.

## 1. Check the local installation

Run from the project directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m trading_bot.cli doctor
.\.venv\Scripts\python.exe -m trading_bot.cli readiness-report
```

If `.venv` does not exist, create it with `python -m venv .venv`, then install using `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.

`readiness-report` currently reports `insufficient_evidence`. The detailed output is `var/readiness_report.json`. A passing unit test establishes software behavior under the tested conditions; it does not establish a profitable strategy or successful brokerage connectivity.

## 2. Connect a dedicated Alpaca paper account

Copy `.env.example` to `.env` if you do not already have a local configuration. Enter your **paper** API key and secret in that local file and keep `ALPACA_PAPER=true`. Do not paste credentials into chat. `.env` is ignored by Git.

Use a dedicated paper account with an account balance representative of your planned capital. `--cash` controls simulations; connected order sizing uses actual broker account equity and cash. Manual holdings, different bots, short positions, and fractional positions can cause reconciliation or whole-share checks to block submissions.

Verify the connection without submitting. Run the signal command during the US stock-market session because new entry plans require fresh quotes, including in dry mode:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.cli status --symbols-file universe_popular.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-PaperCandidate.ps1
```

Resolve any missing data, pending orders, or reconciliation error before submitting. Do not erase journal files to silence an error. An uncertain network response may represent an order that the broker already accepted; the reservation deliberately prevents blind resubmission.

Historical protective orders can have broker-generated replacement IDs. The bot follows broker replacement links to recover their executions. An ambiguous history involving fills before replacement requires reconciliation from the broker's execution ledger; the bot stops rather than guessing whether those fills are inherited by the replacement.

## 3. Observe managed paper operation

Open the desktop app and check its manager state, recent worker heartbeat and broker refresh. **Refresh broker** verifies reconciliation, current holdings and confirmed stop coverage. The current candidate uses a 15% account drawdown circuit threshold and 10% fixed/trailing stops. These are experimental settings, not guaranteed loss limits. Decide your capital and acceptable loss before considering real money.

The operator checks protection after entry submissions and again during the risk loop; protection is effective only after the broker accepts it. Opening the desktop window alone does not start an operator. Closing it leaves an already running manager active.

Compare confirmed quantities and prices with the Alpaca paper dashboard. The desktop app displays its canonical runtime location; direct file reads under the original `var` folder can show retained historical data instead of current runtime state.

Entry sizing uses a recent valid ask quote. Missing quotes, quotes older than two minutes, crossed quotes, and spreads over 2% reject the affected entry. Market-order fill prices can still move after sizing; percentage limits are estimates rather than guaranteed execution-price caps.

The computer must remain awake and connected. The manager provides bounded worker recovery; optional Windows sign-in resume is initially off. This is a local process rather than a hosted deployment.

Use one journal directory and one submitting process for each account. A different journal directory is not a way to run a second competing operator.

## 4. Pause and stop

Pause new buys while risk exits continue:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.paper_app pause-buys
```

Resume new buys by removing that file:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.paper_app resume-buys
```

Stop the session:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.paper_app stop
```

The session checks its canonical stop flag between operations and during waits. Each new BUY also rechecks Stop and Pause buys after broker/quote requests and immediately before reservation. It cannot interrupt an API request already in progress. Ctrl+C also stops a terminal session. Existing broker-side protective orders remain active; stopping does not liquidate positions. Press **Start paper trading** to intentionally restart the managed operator. The runtime's `heartbeat.json` records `running`, `healthy`, `waiting_for_open`, `failed`, or `stopped`; check its timestamp because a terminated process cannot update it.

## 5. Rebuild strategy evidence

Start with historical OHLCV data saved locally, including enough warm-up and multiple market regimes. CSV columns are `date,symbol,open,high,low,close,volume`. Verify corporate-action adjustments and historical universe membership. A present-day popular-stock watchlist introduces selection bias.

```powershell
.\.venv\Scripts\python.exe -m trading_bot.cli research --data csv --csv data\prices.csv --symbols-file universe_popular.txt --profile paper-operator --expanded --walk-forward --folds 6 --csv-out var\research.csv --walk-forward-csv-out var\walkforward.csv
.\.venv\Scripts\python.exe -m trading_bot.cli readiness-report --research-csv var\research.csv --walk-forward-csv var\walkforward.csv
```

For connected historical data, use `--data alpaca --days 1095` in place of the CSV options. Source data should be archived separately; the generated manifest records a hash and parameters, but does not contain the candles. `--data demo` is useful only for software smoke testing and is rejected as profitability evidence.

Freeze the strategy and risk rules before evaluating genuinely unseen dates. Include realistic spreads, slippage, fees, and a relevant benchmark; stress the costs and compare drawdowns. Comparing many variants against the same test period consumes that test period for selection. A `research_candidate` assessment only identifies a candidate for further paper study. Preserve paper fills and evaluate performance over a meaningful sample before considering a separately reviewed real-money integration.

Useful paper reports:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.cli daily-report
.\.venv\Scripts\python.exe -m trading_bot.cli strategy-report
```

The fill-based strategy report is a gross trade attribution report, not a fully audited account return after every brokerage fee, cash flow, dividend, or corporate action.
