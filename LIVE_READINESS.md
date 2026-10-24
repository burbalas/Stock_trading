# Status and real-money readiness — 2026-09-22

**October 22 update:** this is a historical checkpoint. A separate live pilot adapter, EUR risk controls and read-only preflight have now been implemented, while funded submission remains disabled and untested. See [LIVE_PILOT_GUIDE.md](LIVE_PILOT_GUIDE.md) and [SMALL_CAPITAL_REVIEW.md](SMALL_CAPITAL_REVIEW.md) for current scope and blockers.

**Historical checkpoint:** see [the October 21 review](REVIEW_2026-10-21.md) and [desktop app guide](APP_GUIDE.md) for current status. Supervised background paper operation, immediate buy revalidation and the first candidate paper entries have since been implemented and exercised. Real-money accounting and profitability prerequisites remain open.

**Connected paper trading works, but this project is not ready for real-money deployment.** The brokerage adapter still explicitly rejects real-money endpoints. That restriction has not been removed.

## What actually happened

- The repaired candidate ran from about 22:46 to 23:00 EEST on September 15 and stopped normally at market close. The previous launcher was intentionally limited to one session.
- No later operator heartbeat or Python process was found on September 22. The bot was not trading in the background during the intervening week.
- The paper account checked again at approximately 21:34 EEST on September 22 had **$99,440.16 cash/equity, no positions, no open orders, and no unresolved order intents**. Reconciliation passed. This balance is unchanged from the prior check; the operator is still stopped and no stop/drawdown flag is active.
- Today's read-only signal scan passed on completed daily bars. A second, market-open preview passed fresh-quote validation and prepared two whole-share entry plans within the configured daily/capital limits, without submitting them. This is not evidence of executed trades or validated profit.
- The broker reports today's market open at 16:30 EEST and close at 23:00 EEST. The configured entry buffer ends around 16:40 EEST.

There is still no forward-trading sample for the frozen relative-strength candidate. A short session with no orders establishes neither execution reliability under load nor profitable performance. The [historical comparison](PROFITABILITY_REVIEW.md) favored the candidate over the old bot, but its final return still lagged initially capital-matched passive benchmarks.

## Improvements from this review

- `Start-PaperCandidate.ps1 -Submit -Continuous` now waits across market sessions instead of ending after one day. The one-session default remains available. A closed terminal, sleeping computer, crash, or reboot still stops local operation; this is not an installed service.
- `status` now reports the operator heartbeat, whether it is stale/stopped, and drawdown/stop flags. A connected brokerage account is no longer the only visible status.
- Successful session risk and signal checks now append account equity observations to `var/account_observations.csv`, with timestamps, account, preview/submission mode, parameters and their hash. This records future evidence without extra broker calls. These observations are not cash-flow-adjusted investment returns, and missing dates are not fabricated.
- Account drawdown halts cancel the bot's already-pending BUY orders, await confirmed outcomes, synchronize raced fills, and refresh holdings before liquidation. Manual orders and existing sell protection are not canceled by that step. Unknown cancellation outcomes remain blocked.
- Risk exits have bounded retries after authoritative confirmation that an earlier order was rejected, canceled or expired. Original intents and confirmed fills remain recorded; ambiguous active orders cannot create a second order. No more than three attempts per symbol/day are allowed.
- Protective-stop creation can recover from an earlier confirmed zero-fill cancellation/rejection using bounded stable IDs. It permits the initial stop plus up to three recovery attempts, checks returned identity/status/quantity/price, and does not mark a rejected response as active protection. Protection is rechecked immediately after a risk-exit attempt instead of waiting for the periodic stop-sync deadline. Filled, pending, replaced or otherwise ambiguous prior stops remain blocked for reconciliation.

**Verification:** all 230 tests pass on the local Windows/Python 3.11 environment, including the native PowerShell launcher, cancellation/fill races, retry deduplication, protection recovery, stale heartbeats and account-observation separation. Dependency consistency and whitespace checks pass. The connected status and market-open preview passed without orders. Software tests do not establish a profitable strategy.

## Next operation

Run from the project folder in VS Code's PowerShell terminal:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-PaperCandidate.ps1 -Submit -Continuous
```

This operates **Alpaca paper funds only**. Keep the terminal and computer awake. Do not run another submitting bot against the same account. Observe the first entries and protective stops in the Alpaca paper dashboard; if quotes, reconciliation or orders fail, retain the logs rather than resetting account state.

Check status from a second terminal:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.cli status --symbols-file universe_popular.txt
```

The latest console log is under `var/logs`; `var/heartbeat.json` records activity. `Ctrl+C` stops the operator while leaving already-existing broker orders active. See [the operating guide](OPERATING_GUIDE.md) for `STOP_BUYS` and `STOP` controls.

## What must happen before real money

1. **Establish the intended capital and loss limits.** The user's starting allocation and acceptable loss are still unspecified. A simulated $99,440 account does not validate sizing for a small funded account; whole-share minimums can make a 10% entry impossible. Do not infer an acceptable personal loss from the research's provisional 15% drawdown circuit.
2. **Finish execution and accounting support.** Fractional residual fills can still block automated liquidation/protection. A broker corporate-action ledger is needed to reconcile splits and other non-trade share changes with journal quantities and tracked highs. Split-adjusted price history alone does not solve holdings accounting. Sequential market BUY orders also need immediate pre-submission revalidation of cash, positions, executable prices and permitted price drift.
3. **Collect unchanged forward evidence.** Confirm real paper entries, stops, exits, restart recovery and reconciliation. Compare the same dates against passive alternatives after costs and with exposure/drawdown visible. The existing 60-session checkpoint is an operational review, not a guarantee or automatic promotion rule. Avoid retuning every time a loss appears.
4. **Deploy reliably and separately.** Before a funded pilot, use supervised operation on an always-available machine, runtime storage outside a synchronized documents folder, actionable failure alerts and tested restart/reconciliation procedures. Keep paper and live credentials, journals and risk limits separate; never change the existing paper endpoint to real money as a shortcut.
5. **Only then consider a separately reviewed, tightly capped live pilot.** The real-money adapter and its execution controls must be implemented and tested, live-account eligibility confirmed, and the capital cap explicitly set. No software or historical comparison can guarantee future profits.

Paper execution is an approximation: Alpaca documents omissions including market impact, latency slippage, regulatory fees and dividends. Those costs and cash flows matter when comparing a funded account with this study's adjusted-price results. [Alpaca paper-trading documentation](https://docs.alpaca.markets/us/docs/paper-trading).

Alpaca also rejects conflicting buy/sell orders in several cases; resolving pending entries and preserving cancellation evidence is therefore operationally necessary. [Alpaca order protections](https://docs.alpaca.markets/us/docs/user-protection).

No real-money endpoint was enabled, no orders were placed by this review, and no continuous process was started automatically. Existing local changes remain uncommitted.
