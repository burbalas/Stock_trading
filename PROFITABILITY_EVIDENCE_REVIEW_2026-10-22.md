# Profitability evidence, October 22, 2026

**The current strategy has not earned a real-money recommendation.** The corrected post-selection diagnostic lost money in USD at both intended sizes and underperformed its passive references on point estimates. Keep funded trading disabled. This is a short historical diagnostic, not proof that the strategy will always lose, and not an untouched forward test.

The [plan](PROFITABILITY_EVIDENCE_PLAN_2026-10-22.md) and [clarification](PROFITABILITY_EVIDENCE_PLAN_ADDENDUM_2026-10-22.md) were saved before these results. Only the previously selected `relative_strength_189` was evaluated, without changing its parameters or selecting a new strategy. September 15 through October 21 contains **27 equity observations and 26 daily returns**. Earlier bars supply indicator warm-up only. Each account starts flat and uses literal raw prices, whole shares, as-of split signals and dividend accounting.

| Budget label | Costs per side | Candidate USD return | USD P&L | Return after modeled terminal exit |
|---:|---|---:|---:|---:|
| EUR2,500 | 5 bps, no fixed fee | -0.472% | -$13.27 | -0.476% |
| EUR2,500 | 20 bps, no fixed fee | -0.559% | -$15.70 | -0.575% |
| EUR2,500 | 20 bps plus $1 per fill | -0.808% | -$22.70 | -0.860% |
| EUR5,000 | 5 bps, no fixed fee | -0.722% | -$40.58 | -0.735% |
| EUR5,000 | 20 bps, no fixed fee | -0.918% | -$51.56 | -0.968% |
| EUR5,000 | 20 bps plus $1 per fill | -1.221% | -$68.56 | -1.324% |

The zero and $1 fees are assumptions, not verified account charges. The fee sensitivity incurred seven fills/$7 (EUR6.22 at dated references) for the smaller account and seventeen fills/$17 (EUR15.07) for the larger. Exit sensitivity charges the last close for assumed sale costs; it does not assert an actual execution. Remaining holdings and unpaid dividends are assets, not realized profit or immediately available cash. Withholding, personal taxes, executable currency conversion charges and idle-cash interest are excluded.

The budget labels use the common October 21 ECB reference of 1.1234 USD/EUR to set initial USD cash: $2,808.50 and $5,617.00. Those are **not exact EUR2,500/EUR5,000 deposits at the September 15 exchange rate**. Dated EUR opening marks are EUR2,433.92 and EUR4,867.84. Both scenarios are research sizes, not deposit recommendations.

| Base 5 bps comparison | EUR2,500 label USD return | EUR5,000 label USD return | Actual average invested exposure |
|---|---:|---:|---|
| Candidate | -0.472% | -0.722% | 5.40% / 9.77% |
| Whole-share SPY, initial 40% target | +0.538% | +0.538% | 26.34% / 26.34% |
| Same stocks, initial 40% equal-weight target | 0.000% | -0.059% | 0% / 3.10% |
| Zero-interest USD cash | 0.000% | 0.000% | 0% |

Whole-share rounding leaves much more cash than the nominal targets. The smaller stock basket bought nothing, so that row is an uninformative basket comparison. The larger basket could buy only one NFLX and one WMT share. These outcomes must not be rescued with hypothetical fractional shares. The candidate generated 36 and nine affordability rejections respectively; these are repeated decision events, not independent trials.

SPY's higher exposure means the initial-allocation comparison does not isolate stock-selection skill. The separately labeled **ex-post exposure-matched analytical SPY** also had better point estimates: approximately +0.110% and +0.199% versus the candidate's losses. Its allocation is computed using the candidate's entire observed period and scales a whole-share SPY curve toward cash; it is not a deployable preregistered portfolio or a separate investment recommendation. The primary passive references remain whole-share simulations.

In EUR, the base candidate returned +2.230% / +1.973%, while simply retaining USD cash returned **+2.715%**. The dollar's rise explains why positive EUR marks coexist with losses from trading. Currency appreciation is not evidence of a strategy advantage. No executable FX spread is modeled.

The candidate closed three and seven simulated round trips respectively and ended holding one NVDA share at the smaller size; one AMD, two NVDA and one V at the larger. Worst base USD drawdowns were -1.148% and -1.482%. No historical account circuit latched. A daily-bar 15% circuit does not validate the stricter prepared EUR250 total/EUR50 daily/EUR250 order live policy or establish a maximum loss during gaps and outages.

Paired circular block bootstrap diagnostics use 10,000 resamples and fixed base seed 1729, with 5-session and 20-session blocks. Against whole-share SPY, the primary 95% relative-wealth intervals are approximately **[-2.277%, +0.447%]** and **[-2.894%, +0.624%]**. Both cross zero. Twenty-session intervals contain only 1.3 blocks of original history and cannot establish statistically proven underperformance. Every duration/evidence screen fails. Annualized intervals are omitted for this short sample; no likelihood of future profit is inferred.

The data review also found and fixed a material collection error. Alpaca's corporate-action interval uses `process_date`, which may follow an ex-date by weeks. Querying only through October 21 omitted SPY, JPM and MA dividends already earned but payable later. A separately archived supplement resolves those records; raw/split/all price factors now reconcile to nine distributions during the diagnostic. Entitlement accrues before ex-date trades, remains a receivable until pay date, and then transfers to cash without creating a second profit. [Alpaca documents the process-date filter and publication-delay limitations](https://docs.alpaca.markets/us/reference/corporateactions-1). [JPM's declaration](https://www.jpmorganchase.com/ir/news/2026/jpmc-declares-increased-common-stock-dividend-9-15), [Mastercard's declaration](https://investor.mastercard.com/investor-news/investor-news-details/2026/Mastercard-Board-of-Directors-Announces-Quarterly-Dividend-d3ed32e33/default.aspx), and [State Street distributions](https://www.ssga.com/us/en/individual/resources/documents/etf-dividend-distributions) corroborate the affected payments.

Historical split quantities, cost bases, highs and pending orders adjust only on effective dates. Unsupported actions touching any merger/name-change participant now block evaluation. Missing cash/split ex-dates cannot be replaced with payment/process dates. Factor checks use a fixed $0.03 rounding tolerance; they cannot independently exclude smaller omitted distributions, common provider omissions or historical publication delays. The universe and symbol mapping still contain hindsight/survivorship bias, and the exchange calendar has not been independently certified. Next-open SIP fills do not reproduce the operator's delayed IEX signals, bounded IOC orders, latency, partial fills or protection execution.

Final artifacts are in `var/evidence_2026-10-22/study_v3/`; raw/split/all CSVs, actions, supplement and ECB references are in `var/evidence_2026-10-22/data/`. Each result saves source, code, configuration and plan hashes, full fills, marks, cash flows, rejections, costs and uncertainty diagnostics. Versions 1 and 2 remain archived. The final rerun includes stricter malformed-action guards and extra cost fields; the numerical returns did not change. Earlier all-adjusted small-capital results remain approximate and cannot be treated as literal historical whole-share execution.

At **17:57 UTC / 20:57 Europe/Vilnius on October 22**, a separate read-only Alpaca check found the paper manager and worker running, a healthy heartbeat, sign-in resume enabled and sleep prevention active. Positions were 28 AAPL, 17 AMD and 24 V, each with a managed broker stop; broker and journal quantities reconciled. Equity was approximately $98,874 in the preexisting roughly $100,000 paper account. That is an unaudited account mark, not candidate investment performance. Journals contained 43 observations across two dates and two parameter fingerprints, three new candidate opening trades and zero candidate exits. Twenty-six older imported fills remain unrelated; the October 22 AAPL fill is the twenty-seventh synchronized record and belongs to the current operation. Do not attribute all imported fills to the candidate or combine the two configurations into an unchanged forward cohort.

Prospective evidence is registered separately from **October 23**, with zero eligible completed sessions at this review. The tracker freezes source/configuration hashes, preserves hash-addressed JSON/CSV revisions and rejects revisions to committed history. A provider revision or source change needs a documented new trial; it must not silently improve old evidence. This is representative-size **daily-bar replay**, not verified broker execution at those sizes. No order or funding action exists in its update path.

From the project folder in the VS Code PowerShell terminal, run this weekly or the day after completed market sessions:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Update-ProfitabilityEvidence.ps1
```

It downloads market data with the existing paper data credentials only, appends eligible prospective replay evidence and writes `var/forward_evidence_2026-10-23/progress.md`. Before the first eligible completed session it reports zero data without contacting an API. For a local-only status check, add `-StatusOnly`. This command is not an installed background schedule. Keep the existing paper app running on the awake, connected computer for separate operational observations; do not reset its journal or launch a second worker.

The first operational review requires **at least 60 completed market sessions AND 20 closed round trips**, unchanged rules and no unresolved execution/accounting failures. A separate descriptive statistical duration screen requires 252 paired return observations. These are review checkpoints, not profitability proofs or automatic funding triggers. Representative-budget broker execution and account-specific costs still need verification before a funded pilot. There is no reason to add live keys or deposit money based on this test. If the candidate remains uncompetitive, evaluate a simpler alternative under a new plan and future clock instead of selecting a winner from this already examined interval.

Validation: the complete suite ran 523 tests, with 522 passing and one Windows symlink-permission skip. Dependency checks, source compilation and whitespace checks passed. Independent reconciliation verified all 24 saved cash ledgers and all 31 artifact hashes. Final results SHA-256: `f7bc20ea90a003f7f7dc225051d84303ac25a468fa9c41a034060063093830a3`. Frozen prospective registration SHA-256: `31d868f07fdf85e78b0a00521a4709a5c27a53aee2b097c71a6d5664f2c9b960`. Both launcher modes were exercised before the first eligible session; they correctly reported zero forward data, and the update wrote the progress report. A synthetic first-session fixture also confirms that all six cohorts work before any returns exist. No funded API or real-money order was used.
