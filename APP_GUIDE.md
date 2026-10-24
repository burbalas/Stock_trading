# Use the desktop app

Open the **Stock Trading (Paper)** desktop shortcut, or double-click [Open-StockTrading.cmd](Open-StockTrading.cmd) in this project. VS Code and a terminal are no longer needed to keep the operator running.

The desktop application trades **Alpaca paper funds only**. The separate [live pilot operator](LIVE_PILOT_GUIDE.md) is disabled and has its own credentials, policy, runtime and controls. Opening this desktop app does not start it.

## Controls

- **Start paper trading** checks the saved account and reconciliation, then starts the frozen relative-strength candidate in the background. Repeated clicks cannot start a second submitting operator.
- **Stop bot** persists the stop request and stops the operator gracefully. Existing positions and broker stop orders remain in the account; Stop does not liquidate holdings. An in-flight broker request can delay shutdown.
- **Pause buys** blocks new entries while risk checks and exits continue. **Resume buys** removes this pause; it cannot clear a latched account drawdown halt.
- **Refresh broker** updates the account view and verifies reconciliation. Its timestamp is separate from the worker heartbeat. The stop-coverage message reflects confirmed broker orders at that refresh.
- **Create report** opens a local activity report from recorded observations and fills. Preview data, different parameter sets and legacy trades are identified separately. Equity changes are not audited returns after cash flows and fees.
- **Open logs** shows the current session log, including the underlying errors and rejected entries.
- **Resume on Windows sign-in** is optional and initially off. It creates an app-owned entry in your user's Startup folder. At sign-in it attempts to resume only if you previously left trading enabled; pressing Stop keeps it stopped.

On this computer, paper sign-in resume was enabled during the October 22 review after a Windows restart had interrupted observation. You can turn it off with the checkbox. It starts only the paper manager, never the separate live pilot.

Closing the window leaves the background manager running. An active Windows manager requests prevention of idle system sleep and releases that request on exit; it does not change your power plan or keep the display on. It cannot prevent manual sleep, restart or shutdown, wake an already sleeping computer, or trade without connectivity. Optional sign-in resume is needed after a restart. Check any displayed power warning.

The manager retries transport outages before worker launch, with delays capped at five minutes. Other worker failures permit at most three starts in 15 minutes. Authentication, account and journal inconsistencies require review. A stopped or failed manager is not evidence that broker-side orders have disappeared. Status distinguishes a fresh manager pulse, worker heartbeat and cached broker check.

## Read the status correctly

The window distinguishes the background manager, the worker heartbeat, and the last broker refresh. A recent broker snapshot does not prove that the operator is running. Conversely, cached balances can lag a healthy worker; use Refresh broker for the current account view.

The strategy can remain in cash. Individual entries can also be rejected for stale quotes, wide spreads, share rounding, position limits or daily order limits. New paper entries now use IOC limits capped five basis points above the revalidated ask; they may fill partly or not at all and do not fall back to market BUYs. These checks do not force a trade merely because the market is open. This execution revision starts a new forward observation cohort.

The first two verified candidate paper entries were filled on October 21, 2026: 17 AMD shares and 24 V shares. Both had broker-confirmed stop orders with matching quantities, and the account reconciled. This verifies an execution path, not future profitability.

## Files and recovery

Runtime storage is outside OneDrive under the user's local `StockTrading/paper` directory. Windows Store Python may resolve this inside its package's `LocalCache`; the app displays the actual path and its Open logs button opens it directly. Files include the fill journal, reservations, drawdown latch, heartbeat, account observations, reports and logs.

The original `var` files were retained. `var/.runtime_redirect.json` makes the existing terminal tools use the same canonical journal. Credentials remain in the ignored project `.env` and were not copied to runtime storage. Do not delete the redirect or journal to bypass an error.

Only one submitting paper operator per Windows user is allowed, including terminal launches with different journal paths. An account-ID change is rejected before importing history; account state is no longer silently reset. Follow the recorded error when reconciliation or startup fails.

For terminal diagnostics:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.paper_app status
.\.venv\Scripts\python.exe -m trading_bot.cli status --symbols-file universe_popular.txt
```

Read [the latest engineering review](REVIEW_2026-10-21.md) before considering real money. Fractional/corporate-action accounting, execution-price bounds, personal capital limits and meaningful forward evidence remain unfinished prerequisites.
