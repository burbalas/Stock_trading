# Evidence plan clarification before new results

Recorded **2026-10-22 10:35:54 UTC / 13:35:54 Europe/Vilnius**, before evaluation of the newly requested post-selection data. This supplements, without rewriting, [the frozen plan](PROFITABILITY_EVIDENCE_PLAN_2026-10-22.md), whose SHA-256 is `da05e9144b7f3308798afa2bfb93fed10e05323d7cc8c4bc4f0a0eb5649c3ebc`.

- Set bootstrap random seed to **1729**, resamples to **10,000**, primary moving-block length to **5 sessions**, and supplementary fixed sensitivity to **20 sessions**. Report too-short samples rather than silently manufacturing blocks or treating correlated marks as independent. These intervals are conditional diagnostics; a few weeks cannot support reliable long-horizon inference.
- The first forward operational review requires at least **60 complete market sessions and 20 closed round trips**, unchanged parameters, representative sizing and adequate execution records. This is a minimum checkpoint, not a promise of adequate statistical power or a live-trading trigger.
- A statistical runner may additionally require **252 complete paired daily observations** before presenting its descriptive screen as having sufficient duration. That threshold is likewise a heuristic minimum, not proof, a calibrated significance guarantee, or automatic authorization to fund. The operational and statistical duration screens answer different questions; either can remain insufficient after the other is met.

Archive this addendum's hash with the original plan and the output manifest. Do not modify thresholds based on the observed return.
