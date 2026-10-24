# Forward evidence status — October 22, 2026

Read-only local audit at approximately **13:35 Europe/Vilnius**. No broker call, credentials read, orders or process changes were made by this audit.

The current candidate has **one recorded market date, two opened positions and zero completed round trips**. There is not yet a forward sample capable of establishing profitability. Repeated account observations during one evening do not increase the number of independent market sessions.

## Attributable records

The authoritative paper runtime is the redirected local directory:

`C:\Users\edgar\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\LocalCache\Local\StockTrading\paper`

| Record | Count / detail |
|---|---|
| Current submitting observation cohort | 7 records, all October 21, 2026 |
| First / latest observation timestamps | `2026-10-21T18:53:50.400016+00:00` / `2026-10-21T19:17:05.895242+00:00` |
| First / latest observed equity | $99,440.16 / $99,469.50 |
| New candidate fills | BUY 17 AMD and BUY 24 V on October 21 |
| New candidate exits / closed round trips | 0 / 0 |
| Older imported fills | 26, mode `alpaca-paper-sync`; strategy attribution unavailable |
| Current cohort parameters SHA-256 | `45af716d86ffbb32ec8e25483263fd81c680762703b1fc45124ce2ec4ea523ab` |

The observed equity difference is an intraday mark in a preexisting roughly $100,000 simulated account, not an audited cash-flow-adjusted investment return and not evidence of execution at €2,500 or €5,000. Legacy executions must stay separate from this candidate. Account identity values are intentionally omitted.

The saved heartbeat reported `waiting_for_open` at `2026-10-22T10:35:15.507748+00:00`. That is process activity, not proof of broker connectivity, up-to-date valuations or future execution.

## Replication differences that remain material

The submitting parameter record uses IEX data, 600 requested calendar days, a ten-minute opening buffer, five-minute risk polling and approximately one signal review per regular session. The September study used consolidated SIP, all-adjusted bars and modeled next-open fills. The live preparation additionally introduces bounded IOC entries and euro loss triggers. These are execution/risk variants; their results cannot be spliced into one homogeneous return series.

The current paper account is far larger than the intended funded budget. Whole-share affordability, fees and rejection frequency differ. Preserve this account's journal and continue its cohort as operational evidence; create a separately recorded representative-budget paper replay/account experiment rather than resetting or relabeling its old history.

## Audit input fingerprints

These files may change as the operator runs. Hashes describe the read-only audit snapshot, not a permanent freeze of active runtime storage:

| Source | SHA-256 |
|---|---|
| `account_observations.csv` | `cf46fa156fc7b51499830b86a1dedf388e25e64bb22a3f040794881d2f910013` |
| `fills.csv` | `708fade45ecef323027a7d8f0530d21462e79470a3b881ac883084ad58fb8fd7` |
| `bot_state.json` | `15770b686b9e5e3ef244f734e0ac7a9d0f72ce4ca05881637c0f6755ea0ac98c` |

Next steps and minimum review conditions are fixed in the [evidence plan](PROFITABILITY_EVIDENCE_PLAN_2026-10-22.md) and its [pre-results clarification](PROFITABILITY_EVIDENCE_PLAN_ADDENDUM_2026-10-22.md). Nothing here enables real-money trading or supplies a profitability guarantee.
