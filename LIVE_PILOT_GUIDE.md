# Live pilot preparation — October 22, 2026

The project now contains a separate, explicitly configured Alpaca live adapter and operator. **It has not placed a real-money order, connected to your live account, or established profitable trading.** The desktop shortcut still operates paper funds. Live submission is disabled in the prepared local policy.

## Allocation and loss controls

You specified euros, a maximum allocation of €5,000, a maximum tolerable loss of €1,000, and this Windows computer as the host. The prepared pilot policy is deliberately below those ceilings:

| Setting | Prepared value |
|---|---:|
| Capital ceiling | €2,500 |
| Total loss trigger, from observed account peak | €250 |
| Daily loss trigger, including observed overnight gaps | €50 |
| Maximum initial order | €250, also subject to 10% sizing |
| Open positions / daily BUY attempts | 4 / 2 |
| Entry price allowance | 5 basis points above the freshly revalidated ask |
| Entry order | Whole-share IOC limit; no market fallback |
| Position stop / trailing stop | 10% / 10% |

This is a provisional ceiling for a later supervised pilot, not a claim that €2,500 is optimal or an instruction to deposit it now. No funds were transferred. The half-size ceiling limits the amount exposed while allowing more stocks than a €500–€1,000 account. Many leaders can still be unaffordable. The [small-capital review](SMALL_CAPITAL_REVIEW.md) explains the historical sizing experiment, reused evaluation period and split-adjustment limitations. Its best percentage return must not be used as proof of the best capital allocation.

Loss controls are triggers, not guaranteed maximum losses. Price gaps, outages, delayed fills and FX changes can produce larger realized losses. The existing percentage drawdown circuit also remains active and can stop sooner. Do not deposit additional money to conceal a loss or clear a saved halt to bypass it.

## What is implemented

- Live keys are separate from paper keys. The live SDK uses the official US Trading API endpoint with `paper=False`, without inheriting a paper URL override. Paper commands still reject real-money endpoints.
- The exact live account UUID is pinned in the policy. Account identity, ACTIVE status, USD currency and restriction flags are rechecked before broker writes. This adapter does not support EUR Local Currency Trading or the separate Alpaca Europe APIs; confirm your approved account/API eligibility first.
- A separate local `StockTrading/live` runtime holds live orders, fills, risk baselines, health and heartbeat files. Paper state remains separate. A live submitting lock spans the whole session.
- Entry sizing uses the smaller of the configured capital ceiling and current account equity, available cash/buying power, cash reserve, per-order limit and remaining gross allocation. It does not use margin buying power to exceed available cash.
- Each BUY uses a fresh ask and an IOC limit with conservative tick rounding. Recovered orders and fills must preserve identity, quantity and price cap. An IOC may fill partly or not at all. It is never replaced with an unbounded market BUY.
- Confirmed entries receive a stop before another planned BUY. Pending stop acceptance is checked with bounded retries under stable IDs. Persistently unconfirmed protection latches a halt and immediately attempts the risk-exit path without depending on another historical-data download. Cancellation, raced fills and stable IDs retain their normal safeguards. An unavailable exit remains an explicit error requiring review; the application cannot guarantee execution during broker outages.
- Loss observations persist across restart and cannot rebaseline after completed trades or a deleted risk file. External cash deposits/withdrawals are separated from the euro loss baseline using non-trade activities. Dividends/fees remain account income/cost. Corrections, missing history and unsupported share changes require review.
- Cash-activity/FX-monitoring failures block new buys while known-position USD exits and stop management remain permitted when account/position validation succeeds. Missing, corrupted or inconsistent holdings ledgers still require manual reconciliation. Monitor `live-health.json`; a process heartbeat alone does not prove the euro risk view is current.
- Broker quantities and cumulative executions retain decimal precision. Fractions, short holdings, splits, mergers, symbol changes and external positions are not silently rounded or guessed into agreement. Unsupported holdings stop automation and need broker review.

The EUR view uses a dated, cached ECB daily USD-per-EUR reference, with verified TLS, bounded requests and a four-calendar-day freshness limit for entries. It is an informational mark, not an executable conversion quote or continuous FX hedge. Intraday FX moves, conversion spreads, fees and taxes are not guaranteed by it. A stale cache may be retained solely for estimated existing-position risk management; it cannot authorize new entries. [ECB reference-rate explanation](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html).

Alpaca documents that limit orders may remain unfilled and sell stops can execute away from their trigger price. IOC orders avoid leaving an entry resting all day, but do not remove liquidity or outage risk. [Alpaca order documentation](https://docs.alpaca.markets/us/docs/orders-at-alpaca).

## Before any live launch

1. Rotate the paper keys that were found in `.env.example` and appeared in diagnostic output. Put replacement values only in the ignored `.env`. Then Stop the paper bot, close its window, reopen it and Start, so the background processes reload the replacement credentials. The example file now contains placeholders. Never paste API secrets into chat or commit them.
2. Use an approved, dedicated USD Trading API account with no manual positions, competing bots, foreign open orders, short holdings or fractional residuals. First live initialization requires a flat account and an empty live execution journal. Live eligibility and funded connectivity have not been verified on this machine.
   Complete funding and let it settle before initialization. Stop the operator before any later deposit, withdrawal or external account change, then review and reconcile before restarting. Account and activity endpoints are separate snapshots; delayed activity publication can otherwise cause a conservative false halt. These observations are not audited cash-flow-adjusted returns.
3. Exercise the bounded-entry version in paper operation and verify actual entry, partial/terminal outcomes, confirmed stops, exits and restart/reconciliation. The current roughly $100,000 paper account does not validate sizing at €2,500. A dedicated representative paper account/replay is needed; preserve the existing journal rather than resetting it to bypass account errors. The small-capital study is offline reused evidence, not that execution trial.
4. Keep the frozen rules long enough to collect meaningful forward results and compare costs, drawdowns and passive alternatives. There is no automatic promotion to live mode and no proven profit claim. One evening's fills do not establish a trading edge.
5. Monitor this Windows host. The active operator requests prevention of idle system sleep, but cannot prevent manual sleep, restart, shutdown, internet loss or broker outages. The live pilot runs in an explicitly opened terminal; it is not automatically launched by the paper desktop sign-in setting. Broker stops remain at the broker after the local process stops.

## Local setup and read-only checks

Add separate live credentials to the ignored `.env` only:

```dotenv
ALPACA_LIVE_API_KEY=YOUR_LIVE_API_KEY
ALPACA_LIVE_SECRET_KEY=YOUR_LIVE_SECRET_KEY
```

Keep the existing paper settings and keys separate. The live adapter does not treat changing `ALPACA_PAPER` to false as authorization.

From the project folder, retrieve your live account identity locally without placing orders:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.live_trading account
```

Replace `REPLACE_WITH_YOUR_LIVE_ACCOUNT_UUID` in ignored `live-policy.json` with that exact lowercase UUID. Inspect the prepared budget fields. Keep `enabled` false while checking:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-LiveTrading.ps1
```

This checks the live account, reconciliation, completed market-data coverage, feed permissions and entry affordability. Fresh quotes are checked only while the market is open; a closed-market check cannot prove a future quote will be usable. A technical pass does not enable writes or establish profitability. The report is in the separate runtime's `live-check.json`.

The launcher prints the full console-log location in the separate live runtime's `logs` directory. Missing account identity or credentials produces a blocked preflight, not automatic order authority.

## Explicit future live launch

After the prerequisites are satisfied, live operation requires setting `enabled` true in the local policy and supplying its exact account UUID to the launcher:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-LiveTrading.ps1 -Submit -ConfirmAccount YOUR_LIVE_ACCOUNT_UUID
```

This command can place **real-money orders**. It is documented for future use and was not run by this review. Keep the terminal open and supervise the first sessions. The existing paper desktop remains labeled Paper and controls its separate paper worker.

For live controls from another terminal:

```powershell
.\.venv\Scripts\python.exe -m trading_bot.live_trading status
.\.venv\Scripts\python.exe -m trading_bot.live_trading pause-buys
.\.venv\Scripts\python.exe -m trading_bot.live_trading resume-buys
.\.venv\Scripts\python.exe -m trading_bot.live_trading stop
```

Pause preserves exits; Stop ends local monitoring without liquidating positions or canceling broker stops. A stop issued during preflight remains persisted and prevents launch. Resume buys cannot clear a latched loss halt. Read the actual account and errors before intervening; do not delete journals or risk baselines to force a restart.
