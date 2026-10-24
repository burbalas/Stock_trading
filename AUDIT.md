# Trading bot review

The original requirement was automatic stock trading using a demonstrated profitable strategy. The repository contains an automated **paper** operator, deterministic heuristic signals, and exploratory backtests. It does not establish a profitable live strategy, and this review does not turn it into one.

## Findings and repairs

| Problem found | Current behavior |
| --- | --- |
| Windows startup failed because exchange timezone data was missing | `tzdata` is installed through requirements; a local `.venv` was created |
| Accepted orders could be recorded as fully filled at estimated prices | Only broker-confirmed quantity and price are journaled; partial executions are preserved |
| Retries and concurrent processes could duplicate submissions | Stable broker IDs, durable intent reservations, atomic state updates, and a submitting-process lock |
| Only protective fills were recovered | All tracked bot orders are synchronized, including partial executions on canceled orders |
| A paper URL override could target a real endpoint | The paper adapter rejects real-money and untrusted endpoints |
| Pending cancellation was treated as complete | Shares remain reserved until cancellation is confirmed and holdings are refreshed |
| Risk sizing ignored execution costs and repeated signals | Sizing includes modeled costs and cash reserves; duplicate/conflicting signals are handled and entry caps retain exits |
| Invalid prices and account values could propagate | Finite numeric inputs, valid bars, supported positions, and complete held-position marks are checked |
| New entries were sized from the previous daily close | Entry quantities use recent bid/ask-validated quotes; stale or excessively wide quotes are rejected |
| Strategy selection ranked test-period results | Candidates are ranked on training results and tested using shared chronological date boundaries |
| Close-detected drawdown exits executed at that same close | Liquidation is queued for a later open; gap risk remains visible |
| Warm-up and incomplete fold coverage distorted statistics | Warm-up is excluded from evaluation and the final fold retains remaining dates |
| Research lacked reproducible metadata | New exports include parameter/data manifests and output hashes; readiness rejects missing or mismatched provenance |
| The session could continue overnight despite `--until-close` | First-open waiting and continuous multi-session operation have explicit separate behavior |
| Unattended operator state was difficult to observe or stop | Atomic heartbeat, `STOP_BUYS`, and `STOP` controls with interruptible waits |

## Strategy evidence

The saved `research_v6_circuit10_2026-08-17.csv` reports **23.1168%** test return versus **41.7035%** benchmark return: **-18.5867 percentage points** of benchmark edge. Its matching walk-forward file reports **-0.9457 percentage points** mean edge and **-23.4504%** worst drawdown. A configured drawdown circuit is not a guarantee that realized losses remain within that threshold.

Some earlier CSVs contain better-looking selected results. They have no archived input candles/provenance manifests proving reproducibility or an untouched holdout, and were generated before simulator corrections. They are not sufficient to identify a validated strategy. Repeatedly changing rules using the same historical test periods creates selection bias.

The default advisor is a rule-based heuristic. No trained machine-learning model, independent forward-performance record, or genuine live execution evidence was found.

## Remaining limits

- Alpaca paper credentials were subsequently configured and read-only account/data checks passed on 2026-09-15. Actual order execution has not been tested during this review; an observed paper session is still required.
- The supported automated portfolio is whole-share, long-only US stocks. Confirmed fractional fills are recorded truthfully; unresolved fractional or short holdings stop order planning.
- Ambiguous submissions remain reserved until broker reconciliation or operator review. A same-day rejected or canceled order is not blindly resubmitted under a new ID.
- Broker protection depends on successful API submission and confirmation; the operator is not a redundant hosted trading service. A machine failure can stop local risk monitoring.
- Daily OHLC simulation cannot reproduce exact intraday price paths or live fills. Splits, dividends, universe survivorship, real spreads, liquidity, and account cash flows need independent validation.
- The evidence report provides a research screen. It does not grant live order authority and cannot prove future profits.

## Verification completed

Local Windows/Python 3.11 verification on 2026-09-14:

- **149 unit and regression tests passed**, including broker timeouts, partial fills, cancellation races, concurrent reservations, stale quotes, research selection, and session controls.
- Python compilation and dependency consistency checks passed.
- The demo ran successfully. A separate synthetic research run exported training/test and walk-forward CSVs plus a provenance manifest; readiness correctly rejected synthetic data as profitability evidence.
- The latest legacy v6 evidence was assessed and rejected for missing provenance, negative benchmark edge, and excessive observed drawdown.
- Git whitespace checks passed. A GitHub Actions workflow was added for Windows/Linux and Python 3.11/3.12; those hosted jobs have not been run during this local review.
- Brokerage connectivity, live market-data access, and actual paper order execution remain untested because paper credentials were not configured. No orders were submitted.

### Connected follow-up on 2026-09-15

The account connection, completed daily-bar feed, and dry signal scan passed. The broker reported no positions or open orders. The initial history reconciliation failure came from broker-generated replacement stop IDs; tracing `replaces`/`replaced_by` links recovered their confirmed closing fills and brought the journal into agreement with the account. No journal reset or brokerage changes were required.

This follow-up added replacement-chain recovery tests, preserved newer active protection during history import, and blocked ambiguous partial-fill replacement histories pending execution-ledger reconciliation. The operator preserves partially filled stop orders instead of resizing their cumulative quantities. A recovered same-day sell-before-buy ordering defect in the performance report was also corrected; unmatched sells and unvalued open lots now raise errors instead of disappearing from results. Date-only historical fills use buy-first FIFO within each day and cannot provide exact intraday attribution.

**159 tests passed** after these changes, along with Python compilation and Git whitespace checks. A fresh paper performance report was written to `var/strategy_report_2026-09-15.md`. No orders were placed and no continuous operator was started. Fresh intraday entry quotes and actual paper order execution still require an observed market-open trial.

Alpaca documents that paper trading is a simulation and omits factors such as market impact and latency slippage in its [paper-trading documentation](https://docs.alpaca.markets/docs/paper-trading). Its [automated-trading risk disclosure](https://files.alpaca.markets/disclosures/library/RisksAutoTrading.pdf) also explains why attractive backtests can fail in live markets and why stops do not guarantee loss limits.

See [OPERATING_GUIDE.md](OPERATING_GUIDE.md) for installation, paper connection, operational controls, and the evidence workflow.

### Profitability follow-up on 2026-09-15

The [new comparison](PROFITABILITY_REVIEW.md) archives adjusted SIP data, freezes a seven-candidate comparison, records selection before final evaluation, and stresses execution costs. Relative strength improved on the current ensemble in the final period (11.09% versus 5.88%; 8.30% versus -8.33% with higher costs) but trailed initially capital-matched passive references. It remains a forward paper candidate, not a proven profitable system. No submitting profile was changed.

Historical provider requests now explicitly default to split adjustments. Live drawdown checks persist a latched halt across restart and recovery; journal buy reservations respect the halt while risk exits continue. Research-only monthly trend candidates and the offline comparison runner have regression coverage. **177 tests passed** with consistent dependencies and a clean whitespace check. No orders or continuous operator were started during this follow-up. Full source metadata, configurations, code hashes, results and the review plan are linked from the profitability report.

### Windows storage follow-up on 2026-09-15

The user's paper session recorded `WinError 5` replacing `bot_state.json` and stopped at 17:21 EEST. Atomic state, fill-ledger and heartbeat replacements now retry only Windows errors 5/32/33, using the same temporary file under the existing lock. Retries are bounded to ten attempts/5.55 seconds of waiting, preserve the old destination, and do not repeat brokerage actions. Cleanup and failure-heartbeat errors retain the primary exception.

**187 tests passed**, including a real Windows sharing-denial/release test, persistent-failure preservation, immediate propagation of unrelated I/O errors, and order/fill deduplication. A connected risk preview then wrote the production journal successfully without submitting orders. At 18:40 EEST the account remained flat with $99,440.16 cash/equity and no unresolved orders or reconciliation differences. No state reset or automatic restart was performed. Existing historical result code hashes remain the provenance of their original runs; these storage-only changes are subsequent code revisions.

### Operational and real-money readiness review on 2026-09-22

The last actual candidate session completed normally at market close on September 15 and no later session was found. Fresh account checks passed with unchanged $99,440.16 cash/equity and no positions/orders; a market-open preview validated fresh entry quotes and sizing without submitting. See [LIVE_READINESS.md](LIVE_READINESS.md) for the current evidence and explicit remaining real-money blockers.

The launcher now supports explicit continuous paper operation, status displays heartbeat freshness and stop flags, and session cycles record account equity observations with mode/account/parameter provenance. Drawdown halts cancel owned pending entries and synchronize raced fills before liquidation. Confirmed terminal risk exits can retry up to three times with distinct, persisted IDs; ambiguous orders remain blocked. Protective stops can recover from confirmed unfilled terminal attempts, and protection is rechecked immediately after attempted risk exits.

**230 tests passed**, including Windows launcher integration and execution-race/deduplication regressions. Dependency and whitespace checks passed. No real-money access was enabled and no orders or continuous process were started by this review. Fractional/corporate-action accounting, immediate buy revalidation, supervised deployment, capital limits and forward strategy evidence remain prerequisites to a funded pilot.

### Desktop and supervised paper operation on 2026-10-21

The account was still flat at the start of this review, with no forward candidate sessions since September 15. A desktop application, desktop shortcut, hidden background manager, bounded recovery, persistent stop controls, optional sign-in resume and offline activity reports now make local paper operation accessible without a terminal. Runtime state was copied outside OneDrive under locks with hash verification; a canonical redirect preserves the existing CLI ledger. Account changes are rejected before history import, and a user-wide lock prevents submitting operators in different journal directories.

Broker requests now have bounded connect/read waits and atomic account refreshes. Every BUY is revalidated immediately before submission against the current account, quote, cash, drawdown and position limits; quantities cannot increase above the original plan. Invalid quotes reject their individual entries while preserving other candidates and exits. The frozen strategy and quote-quality limits were retained.

The managed paper operator is running independently of the launching terminal. It filled 17 AMD and 24 V shares using simulated funds. At 22:13 EEST, fresh broker checks confirmed matching active protective stops for both positions, successful reconciliation and a healthy worker heartbeat. Startup on Windows sign-in remains off. Closing the desktop window leaves the operator running; the computer must stay awake and connected.

**324 tests ran successfully, with one Windows symlink-privilege skip**. Desktop smoke, dependency consistency and whitespace checks passed. A final control review also fixed stop/pause changes during slow broker requests: each BUY rechecks the canonical flags immediately before reservation, with six new regression tests. The managed worker was deliberately rotated to load that fix. See [the October 21 review](REVIEW_2026-10-21.md) and [app guide](APP_GUIDE.md). Real-money access remains disabled. Fractional/corporate-action accounting, execution-price limits, user capital/loss limits and meaningful forward evidence remain unfinished before any funded pilot; these paper fills do not establish profitability.

### Guarded live-pilot preparation on 2026-10-22

Implemented a separate disabled live operator with account/API isolation, pinned UUID and account restrictions, read-only preflight, EUR capital/absolute-loss controls, bounded IOC entries, immediate confirmed protection, stable emergency risk exits and decimal execution diagnostics. Unsupported holdings/corporate actions and uncertain ledger histories require review. The user's outer limits are €5,000 capital and €1,000 loss; the prepared disabled pilot is capped at €2,500 with tighter €250 total / €50 daily triggers. It has no live account identity or credentials. No real account was contacted, no funds transferred and no live orders submitted.

The frozen small-account comparison shows affordability materially changes the strategy at €500–€5,000; it remains reused evidence rather than a proven edge or optimal funding recommendation. See [SMALL_CAPITAL_REVIEW.md](SMALL_CAPITAL_REVIEW.md), [LIVE_PILOT_GUIDE.md](LIVE_PILOT_GUIDE.md) and [the engineering review](REVIEW_2026-10-22.md) for exact scope, commands, validation and remaining prerequisites.

Windows logs explain the prior interruption as a restart with startup disabled. Paper operation now has transport recovery, manager pulses, idle-sleep inhibition and sign-in resume enabled on this computer. At approximately 13:20 EEST both manager and worker were active, waiting for market open; fresh paper checks reconciled 17 AMD and 24 V shares with confirmed stop coverage. Exposed paper credentials found in the example file were removed from that file and need rotation in Alpaca. The replacement example contains placeholders; credentials stay in ignored local environment files.

Final verification: **435 tests run — 434 passed, one Windows symlink-privilege skip**. Dependency, source-compilation, desktop smoke and whitespace checks passed. The default live launcher safely blocked at the placeholder identity and wrote its diagnostic log without contacting a real account. Funded connectivity/eligibility, representative-budget observed execution and meaningful forward evidence remain unverified. Source changes are local and uncommitted.
